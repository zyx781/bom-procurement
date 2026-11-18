"""采购规则判断与 SKU 决策（从 taobao_runner.py 抽出的纯函数簇）。

为什么单独一个模块
------------------
这一簇函数回答两个问题，且**不碰浏览器、不碰网络、不读写文件**：

    1. 这个候选能不能买？（型号/封装/材质/形态/功率/接口方向… 见 `_candidate_veto`）
    2. 这家店的 SKU 该选哪一档？（见 `suggest_sku` / `_sku_veto` / `_score_sku_value`）

它们是「买错料」防线的主体，也是改规则时最常动的地方。以前和 6000 行的调度器
混在一起，改一条容差规则要在一整个执行器里翻。现在边界固定：

    rules.py          纯判断：输入标题/参数，输出判定与理由（本文件）
    taobao_runner.py  调度、会话、购物车、持久化

约束（改动时请守住）：本模块不 import taobao_runner，不产生副作用；
所有函数对异常输入返回「判不了/不买」，绝不抛。
"""

from __future__ import annotations

import os
import re

import engine


def _log(msg: str) -> None:
    """规则层唯一的输出出口：一行、flush（跑批时父进程读子进程 stdout）。"""
    print(msg, flush=True)


TOLERANCE_DEMOTE_RATIO = float(os.environ.get("TAOBAO_TOL_DEMOTE_RATIO", "3"))


TOLERANCE_PRECISION_PCT = float(os.environ.get("TAOBAO_TOL_PRECISION", "0.5"))


TOLERANCE_PRECISION_VETO = os.environ.get("TAOBAO_TOL_PRECISION_VETO", "1") not in ("0", "false", "False")


TOLERANCE_STRICT_PCT = float(os.environ.get("TAOBAO_TOL_STRICT", "1"))


_VALUE_PATTERNS = {
    "capacitance": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*([pnuµm]?)\s*f(?![a-z])", re.I),
    "inductance": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*([pnuµm]?)\s*h(?![a-z])", re.I),
    "voltage": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*v(?![a-z])", re.I),
    "current": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*a(?![a-z])", re.I),
    "frequency": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*(g|m|k)?hz", re.I),
    "pins": re.compile(r"(?<![0-9.])(\d+)\s*p(?![a-z])", re.I),
    "pitch": re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*mm", re.I),
}


_FREQ_SCALE = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}


_PKG_PREFIX = re.compile(r"^\s*(0201|0402|0603|0805|1206|1210|1812|2512)\s*")


_RISK_WORDS = ("散新", "拆机", "翻新", "打磨", "改字", "白字", "洗板", "二手", "返修")


_UNBUYABLE = ("询价", "联系客服", "拍下改价", "议价", "备注", "留言型号")


_detect_pack = engine.detect_pack


_SPEC_PREFIXES = ("封装", "阻值", "容值", "感值", "完整型号", "针数", "功率")


def _spec_hit_labels(cand: dict) -> set:
    return {
        str(x)
        for x in (cand.get("hits") or [])
        if str(x).startswith(_SPEC_PREFIXES)
    }


def _spec_kinds(cand: dict) -> set:
    """候选命中的规格类别（阻值/容值/型号…），比全文比对不受写法差异影响。"""
    out = set()
    for label in _spec_hit_labels(cand):
        for pref in _SPEC_PREFIXES:
            if label.startswith(pref):
                out.add(pref)
                break
    return out


_RES_WIRE_WORDS = (
    "绕线", "线绕", "wirewound", "水泥电阻", "水泥", "铝壳", "制动电阻", "刹车电阻", "大功率电阻",
)


_RES_CARBON_WORDS = ("碳膜", "carbon film")


def _lcsc_attrs(line: dict) -> dict:
    info = (line.get("row") or {}).get("lcsc_info") or {}
    attrs = info.get("attrs") if isinstance(info, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def _procurement_basis(line: dict, res: dict | None = None) -> dict:
    """这行为什么买这一件：立创编号 → 型号 → 标题里有没有命中型号。

    买完要能追溯。只有「按编号查到」的行才有这条证据链 —— 同型号反查只是
    参考（BOM 里的型号本身可能就是复制错的），不能当采购依据。
    """
    lc = line.get("lcsc") or {}
    if not engine._lcsc_by_code(lc):
        return {}
    model = str(lc.get("model") or "").strip()
    if not model:
        return {}
    cand = (res or {}).get("candidate") or {}
    title = str(cand.get("title") or "")
    hit = bool(title) and engine._norm_text(model) in engine._norm_text(title)
    return {
        "code": lc.get("code") or "",
        "model": model,
        "package": lc.get("package") or "",
        "hit": hit,
        "note": (
            "标题含立创编号对应型号，是同一颗料"
            if hit
            else "标题未出现立创型号，已按编号规格逐项核对（同规格替代）"
        ),
    }


_CAP_FILM_WORDS = (
    "CBB", "启动电容", "启动电容器", "薄膜", "涤纶", "安规电容", "校正电容",
    "X2电容", "MKP", "MKT", "CBB60", "CBB61", "CBB65",
)


_CAP_ELEC_WORDS = ("电解", "铝电解", "钽电容", "固态电容")


_CAP_CERAMIC_WORDS = ("瓷片", "陶瓷", "独石", "MLCC", "C0G", "X7R", "Y5V")


def _bom_cap_family(line: dict) -> str:
    """BOM 这颗电容是哪一族：钽 / 铝电解 / 瓷片（含其他无极性薄膜）。

    只看数值会出大事：95µF 搜出来的是 CBB65 空调压缩机**启动电容**
    （450V、体积像火柴盒），和 BOM 里那颗 10×12mm 的铝电解不是一个东西，
    容值一样也装不进去 —— 极性、纹波、体积全不一样。
    钽电容同样有极性，但和铝电解不是同一种料，必须单独认出来。
    """
    attrs = _lcsc_attrs(line)
    lc = line.get("lcsc") or {}
    blob = " ".join(
        str(line.get(k) or "") for k in ("mpn", "desc", "params_text")
    )
    lc_blob = " ".join(
        [
            str(lc.get("catalog") or ""),
            " ".join(str(x) for x in (lc.get("catalog_path") or [])),
            str(lc.get("desc") or ""),
            str(attrs.get("介质材料") or ""),
        ]
    )
    # 钽要排在「有极性」前面：钽也有极性，先判极性会把它归成铝电解。
    if "钽" in blob or "钽" in lc_blob or "tantalum" in lc_blob.lower():
        return "钽"
    if "有极性" in str(attrs.get("极性类型") or ""):
        return "电解"
    if str(attrs.get("温度系数") or "") or str(attrs.get("介质材料") or ""):
        return "瓷片"
    # 封装名最可靠：立创/Altium 的圆柱体是 CAP-TH_BD<直径>，方形本体是 CAP-TH_L…W…
    pkg = str((line.get("row") or {}).get("package") or "")
    if re.search(r"BD\s*\d", pkg, re.I):
        return "电解"
    if re.search(r"CAP-TH_L", pkg, re.I):
        return "瓷片"
    if any(w in blob for w in _CAP_ELEC_WORDS):
        return "电解"
    if any(w in blob for w in _CAP_CERAMIC_WORDS):
        return "瓷片"
    return ""


def _bom_res_type(line: dict) -> str:
    """BOM 要求的电阻类型（立创 attrs 优先，其次料号/描述）。空 = 没要求，不拦。"""
    t = str(_lcsc_attrs(line).get("电阻类型") or "").strip()
    if t:
        return t
    blob = " ".join(
        str(line.get(k) or "") for k in ("mpn", "desc", "params_text")
    )
    if "金属膜" in blob or "metal film" in blob.lower():
        return "金属膜电阻"
    if "碳膜" in blob or "carbon film" in blob.lower():
        return "碳膜电阻"
    return ""


def _parse_watts(text: str) -> float | None:
    """标题/SKU 里的功率：`1/4W`、`1/2W`、`2W`、`1W/2W/3W` 取最大值。"""
    best = None
    for m in re.finditer(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*(?:/\s*(\d+))?\s*W(?![A-Za-z])", str(text or "")):
        try:
            if m.group(2):
                val = float(m.group(1)) / float(m.group(2))
            else:
                val = float(m.group(1))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if best is None or val > best:
            best = val
    return best


def _parse_volts(text: str) -> float | None:
    """标题里的耐压：`50V`、`3KV`、`3000V` 取最大值。`Vf/Vr` 这类参数名不算。"""
    best = None
    for m in re.finditer(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*(KV|kV|V|伏)(?![A-Za-z])", str(text or "")):
        try:
            val = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if m.group(2).lower() == "kv":
            val *= 1000
        if best is None or val > best:
            best = val
    return best


def _parse_pitch_mm(text: str) -> float | None:
    """脚间距：`P=5.08mm`、`脚间距2.54mm`、`间距 5mm`。"""
    m = re.search(
        r"(?:P\s*=\s*|脚间距\s*[:：]?\s*|引脚间距\s*[:：]?\s*|间距\s*[:：]?\s*)(\d+(?:\.\d+)?)\s*mm",
        str(text or ""),
        re.I,
    )
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None


_E24 = (
    1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
    3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
)


def _is_standard_value(value) -> bool:
    """归一化到 1–10 后是否落在 E24 档位上（容差 1%）。

    用来识别「BOM 名称列写了个世界上不存在的阻值」：5kΩ 不在 E24（应为 5.1k），
    470Ω / 2.2kΩ / 100nF 都在。判不了（空/非数/非正）时一律返回 True，不误伤。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True
    if v <= 0:
        return True
    while v >= 10.0:
        v /= 10.0
    while v < 1.0:
        v *= 10.0
    return any(abs(v - e) / e <= 0.01 for e in _E24)


def _review_reason(line: dict) -> str:
    """BOM 自身矛盾、且名称列的值不是标准档 → 必须人工确认，不自动买。

    实测这份 BOM：470Ω / 2.2kΩ / 5kΩ 三行的 Manufacturer Part 全是
    MF1/4W-1KΩ±1%T52（复制没改）。按名称列买 470Ω、2.2kΩ 是对的；
    但 5kΩ 不是 E24 档位（标准是 5.1k），很可能名称本身也敲错了 ——
    这种行自动买下去，买到的就是个「BOM 里不存在的值」。
    """
    note = str(line.get("value_conflict") or "").strip()
    if not note:
        return ""
    # 冲突来自「立创编号」时一律人工：编号是物料的唯一标识，它和名称列对不上，
    # 说明这份 BOM 自己前后矛盾（要么名称写错、要么编号复制错），
    # 按哪一边买都可能买错，这种行不能自动下单。
    lc = line.get("lcsc") or {}
    if engine._lcsc_by_code(lc) and str(lc.get("model") or "").strip():
        return (
            f"BOM 名称列写 {str(line.get('desc') or '—')}，但立创编号 "
            f"{lc.get('code') or '—'} 对应 {lc.get('model')}，两者冲突；"
            f"编号是唯一标识，不替你猜——请核对原理图后改 BOM"
        )
    p = line.get("params") or {}
    # 同型号反查冲突：BOM 里同一个型号被用在两个差一个数量级的值上
    # （实测 #4 写 10nF、#7 写 100nF，型号都是 CC1H103ZA1ED3F4D1100），
    # 一个型号不可能有两种容值，八成是复制错型号。这种行别按名称硬买 ——
    # 失败提示会变成「SKU 里容值 1.0401mF 对不上」，把人引到卖家写错的单位上。
    lc_params = lc.get("params") or {}
    for key, kind in (("resistance_ohm", "阻值"), ("capacitance_f", "容值"), ("inductance_h", "感值")):
        mine = p.get(key)
        theirs = lc_params.get(key)
        if not mine or not theirs:
            continue
        try:
            ratio = max(float(mine), float(theirs)) / min(float(mine), float(theirs))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        # 只在「两个值都是标准档位」时下这个判断：BOM 写 5kΩ（不在 E24）时，
        # 更该提示的是「这个值本身可疑」，交给下面的 E24 分支。
        if ratio >= 5 and _is_standard_value(mine) and _is_standard_value(theirs):
            return (
                f"BOM 名称列写 {str(line.get('desc') or '—')}，但同一型号 "
                f"{lc.get('model') or ''} 在立创是 {engine._fmt_param_value(key, theirs)}，"
                f"一个{kind}差 {ratio:.0f} 倍；同型号不可能有两种值，"
                f"八成是复制错型号，请核对原理图后改 BOM"
            )
    for key, kind in (("resistance_ohm", "阻值"), ("capacitance_f", "容值"), ("inductance_h", "感值")):
        v = p.get(key)
        if v is None:
            continue
        if not _is_standard_value(v):
            shown = engine._fmt_param_value(key, v)
            return (
                f"BOM 名称列与料号冲突，且{kind} {shown} 不在 E24 标准档位，"
                f"请核对原理图后再决定（宁可先不买）"
            )
        return ""  # 值本身是标准档，按名称列买可以，冲突已在风险里标注
    return ""


_MOUNT_THT = re.compile(r"直插|插件|通孔|\bTHT\b|\bDIP\b|\bTO-?220\b|\bTO-?247\b", re.I)


_MOUNT_SMD = re.compile(
    r"贴片|表面贴装|\bSMD\b|卧贴|立贴|\bSOP-?\d|\bSOIC-?\d|\bQFN-?\d|\bDFN-?\d|"
    r"\bLQFP-?\d|\bTQFP-?\d|\bSOT-?\d|\bTO-?252\b|\bTO-?263\b",
    re.I,
)


_DIODE_THT = re.compile(r"\bDO-\d", re.I)


_DIODE_SMD = re.compile(r"\bSOD-\d|\bSMA\b|\bSMB\b|\bSMC\b", re.I)


def _bom_mount_style(line: dict) -> str | None:
    """BOM 要的是插件还是贴片。只看封装列/原始封装名，不猜名称列。"""
    pkg = str(line.get("package") or "")
    raw = str((line.get("row") or {}).get("package") or "")
    blob = f"{pkg} {raw}"
    if pkg == "插件" or re.search(
        r"(?:^|[_\-\s])TH(?:$|[_\-\s])|CONN-TH|CAP-TH|IND-TH|SW-TH|HDR-TH|LED-TH", blob, re.I
    ):
        return "tht"
    if pkg == "SMD" or re.search(r"\bSMD\b", raw, re.I):
        return "smd"
    if _pkg_code(pkg) or re.search(
        r"(?:^|[^0-9])(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?![0-9])", pkg
    ):
        return "smd"
    # 芯片/功率封装名本身就说明贴片/插件：SOIC/QFN/LQFP/SOT/TO-252 是贴片，
    # DIP/TO-220 是插件。同型号买错封装（SOIC-8 买成 DIP-8）焊盘完全对不上。
    if re.search(
        r"SOIC|SOP-?\d|TSSOP|MSOP|SSOP|[VPLT]?QFN|DFN|LQFP|TQFP|QFP-?\d|SOT-?\d|"
        r"WLCSP|LGA|BGA|PDFN|PDFNWB|TDSON|LFPAK|DPAK|TO-?252|TO-?263",
        blob,
        re.I,
    ):
        return "smd"
    if re.search(r"DIP-?\d|TO-?220|TO-?247", blob, re.I):
        return "tht"
    # 二极管封装名：DO-35/DO-41 是轴向插件，SOD-123/SMA 是贴片
    if str(line.get("subtype") or "") == "diode":
        if _DIODE_THT.search(pkg):
            return "tht"
        if _DIODE_SMD.search(pkg):
            return "smd"
    return None


def _text_mount_style(line: dict, text: str) -> str | None:
    """候选标题/SKU 明确写的是插件还是贴片。两种都写＝多规格链接，返回 None。"""
    t = str(text or "")
    tht = bool(_MOUNT_THT.search(t))
    smd = bool(_MOUNT_SMD.search(t))
    if str(line.get("subtype") or "") == "diode":
        tht = tht or bool(_DIODE_THT.search(t))
        smd = smd or bool(_DIODE_SMD.search(t))
    if tht and smd:
        return None
    if tht:
        return "tht"
    if smd:
        return "smd"
    return None


def _mount_conflict(line: dict, text: str) -> str:
    want = _bom_mount_style(line)
    got = _text_mount_style(line, text)
    if want and got and want != got:
        label = {"tht": "插件/直插", "smd": "贴片"}
        return f"封装形态不符：BOM 是{label[want]}，候选是{label[got]}（焊盘/引脚不通用）"
    return ""


_LED_COLORS = ("冰蓝", "暖白", "红", "绿", "蓝", "黄", "白", "橙", "紫", "粉")


def _led_color(text: str) -> str | None:
    """LED 发光颜色/类型。`白发红` 是红色，别被前面的「白」骗了。"""
    t = str(text or "")
    if re.search(r"全彩|七彩|RGB|三色", t, re.I):
        return "RGB"
    if re.search(r"双色|双色灯", t):
        return "双色"
    m = re.search(r"白发(冰蓝|暖白|红|绿|蓝|黄|白|橙|紫|粉)", t)
    if m:
        return m.group(1)
    for c in _LED_COLORS:
        if c in t:
            return c
    return None


def _led_conflict(line: dict, text: str) -> str:
    if str(line.get("subtype") or "") != "led":
        return ""
    want = _led_color(_line_blob(line))
    got = _led_color(text)
    if not want or not got or want == got:
        return ""
    return f"LED 颜色不符：BOM 要{want}，候选是{got}（不同颜色是不同料）"


_CRYSTAL_ACTIVE = re.compile(r"有源|振荡器|oscillator|\bosc\b|vcxo|tcxo", re.I)


_CRYSTAL_PASSIVE = re.compile(r"无源|晶体|谐振器|crystal|xtal", re.I)


def _crystal_kind(text: str) -> str | None:
    t = str(text or "")
    if _CRYSTAL_ACTIVE.search(t):
        return "active"
    if _CRYSTAL_PASSIVE.search(t):
        return "passive"
    return None


def _crystal_pins(text: str) -> int | None:
    t = str(text or "")
    m = re.search(r"(?<!\d)([1-8])\s*[pP](?![A-Za-z0-9])", t)
    if m:
        return int(m.group(1))
    m = re.search(r"([1-8])\s*(?:脚|pin)", t, re.I)
    if m:
        return int(m.group(1))
    for word, n in (("两脚", 2), ("二脚", 2), ("三脚", 3), ("四脚", 4)):
        if word in t:
            return n
    return None


def _crystal_conflict(line: dict, text: str) -> str:
    """无源晶体 ≠ 有源晶振；4P 封装 ≠ 2P 两脚。频率对了也不是同一种器件。"""
    if str(line.get("subtype") or "") != "crystal":
        return ""
    want_kind = _crystal_kind(_line_blob(line))
    got_kind = _crystal_kind(text)
    if want_kind and got_kind and want_kind != got_kind:
        return (
            "晶振类型不符：BOM 是"
            + ("有源晶振（要供电）" if want_kind == "active" else "无源晶体（两端/四端无源）")
            + "，候选是"
            + ("有源晶振" if got_kind == "active" else "无源晶体")
        )
    want_pins = _crystal_pins(
        f"{line.get('package') or ''} {(line.get('row') or {}).get('package') or ''}"
    )
    got_pins = _crystal_pins(text)
    if want_pins and got_pins and want_pins != got_pins:
        return f"晶振脚数不符：BOM 是 {want_pins}P，候选是 {got_pins}P（焊盘对不上）"
    return ""


_INDUCTOR_CM = re.compile(r"共模|common\s*mode", re.I)


def _inductor_conflict(line: dict, text: str) -> str:
    """单绕组电感 ≠ 共模电感（双绕组四脚）；另外线径/额定电流不能往下换。"""
    if str(line.get("subtype") or "") != "inductor":
        return ""
    blob = _line_blob(line)
    want_cm = bool(_INDUCTOR_CM.search(blob))
    got_cm = bool(_INDUCTOR_CM.search(text))
    if got_cm and not want_cm:
        return "候选是共模电感（双绕组四脚），BOM 是单绕组电感，脚位/接法都不对"
    if want_cm and not got_cm:
        return "BOM 要共模电感（双绕组），候选没有共模结构，不能当单绕组电感买"

    # 线径：Altium 的 IND-TH_…-D0.5 就是 0.5mm 漆包线，淘宝写作「0.5线」。
    # 0.3 线顶 0.5 线，感值一样但载流能力差一截（实测 3mH 行买成 0.3 线）。
    def _wire(t: str):
        t = str(t or "")
        m = re.search(r"(?<![A-Za-z0-9])D\s*(\d+(?:\.\d+)?)(?![\d.])", t, re.I)
        if m:
            return float(m.group(1))
        # 淘宝写法是「0.3线」「0.5mm线」，数字在「线」前面；「线径0.5」才是数字在后。
        # 顺序不能反：旧写法 `线\s*(\d+)` 会把「0.3线 2A」里的 2A 读成 2mm 线径。
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*线(?!径)", t, re.I)
        if m:
            return float(m.group(1))
        m = re.search(r"线径\s*[:：]?\s*(\d+(?:\.\d+)?)", t)
        return float(m.group(1)) if m else None

    want_wire = _wire(blob)
    got_wire = _wire(text)
    if want_wire and got_wire and got_wire < want_wire * 0.9:
        return f"线径不符：BOM 是 {want_wire:g}mm 线，候选 {got_wire:g}mm 线（载流不够）"

    # 额定电流：BOM 明确写了 A 数才比；候选没写不拦（标题常常只写线径）。
    want_a = engine._parse_current(blob)
    got_as = engine.title_amps(text)
    if want_a and got_as and max(got_as) < want_a * 0.9:
        return f"额定电流不符：BOM 要 ≥{want_a:g}A，候选只有 {max(got_as):g}A"
    return ""


_BOARD_LIKE = re.compile(
    r"转接板|转接座|烧录座|测试座|测试架|核心板|开发板|评估板|最小系统|模块板|空板|底板|扩展板",
    re.I,
)


def _board_conflict(line: dict, text: str) -> str:
    """芯片行不能买成核心板/转接板/烧录座 —— 标题里带完整型号也照样是另一件东西。"""
    if str(line.get("category") or "") not in ("ic", "power"):
        return ""
    if _BOARD_LIKE.search(_line_blob(line)):
        return ""
    m = _BOARD_LIKE.search(str(text or ""))
    if m:
        return f"候选是「{m.group(0)}」类成品板/治具，BOM 要的是芯片本体"
    return ""


_CONN_RIGHT = re.compile(r"弯针|弯脚|弯排|卧式|卧贴|90\s*度|right\s*angle", re.I)


_CONN_STRAIGHT = re.compile(r"直针|直脚|立式|立贴|180\s*度", re.I)


def _conn_orient(text: str) -> str | None:
    t = str(text or "")
    right = bool(_CONN_RIGHT.search(t))
    straight = bool(_CONN_STRAIGHT.search(t))
    if right and straight:
        return None
    if right:
        return "right"
    if straight:
        return "straight"
    return None


def _conn_gender(text: str) -> str | None:
    """公头/母座。XT30/XT60 的 -M/-F 后缀与 JST 的 B4B-XH（公针座）都算证据。"""
    t = str(text or "")
    m = re.search(r"\b(?:XT|MR|MT)\d{2}[A-Z0-9]*[-_ ]([MF])\b", t, re.I)
    if m:
        return "male" if m.group(1).upper() == "M" else "female"
    if re.search(r"\bB\d+B-(?:XH|PH|ZH|SH|EH|GH)\b", t, re.I):
        return "male"
    if re.search(r"公头|公座|公针|插头|排针|\bmale\b", t, re.I):
        return "male"
    # 「插座」不算证据：JST 直针座、排针座卖家普遍把公针座也叫「插座」，
    # 拿它判 female 会把「XH2.54 直针 插座」这种正确候选误杀。只认显式母端字样。
    if re.search(r"母头|母座|母针|母端|排母|\bfemale\b", t, re.I):
        return "female"
    return None


def _conn_conflict(line: dict, text: str) -> str:
    if str(line.get("category") or "") != "connector":
        return ""
    blob = _line_blob(line)
    attrs = _lcsc_attrs(line)
    want_o = _conn_orient(blob) or _conn_orient(str(attrs.get("连接器类型") or ""))
    got_o = _conn_orient(text)
    if want_o and got_o and want_o != got_o:
        return (
            "连接器朝向不符：BOM 是"
            + ("弯针/卧贴" if want_o == "right" else "直针/立贴")
            + "，候选是"
            + ("弯针/卧贴" if got_o == "right" else "直针/立贴")
        )
    want_g = _conn_gender(blob)
    got_g = _conn_gender(text)
    if want_g and got_g and want_g != got_g:
        return (
            "连接器公母不符：BOM 是"
            + ("公头/公座" if want_g == "male" else "母座/母头")
            + "，候选是"
            + ("公头/公座" if got_g == "male" else "母座/母头")
        )
    return ""


def _model_family(token: str):
    """拆同族型号：SS34 / NE555P / 1N5819W / 2CL2FL。"""
    t = str(token or "").upper()
    m = re.match(r"^([A-Z]{1,4})(\d{2,4})([A-Z]{0,3})$", t)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(\d[A-Z])(\d{2,4})([A-Z]{0,3})$", t)
    if m:
        return m.group(1), m.group(2)
    return None


def _title_model_conflict(line: dict, title: str) -> str:
    """二极管/LED/磁珠/保险丝：标题只有同族别的号 → 不能买。

    IC/功率器件有 H02（必须出现完整型号）兜底；被动件以前只看封装和数值，
    SS34 的位子买成 SS26、1N5819W 买成 1N5817 都能混过去。
    多型号链接（标题同时列了 BOM 型号）不算冲突，具体由 SKU 选。
    """
    if str(line.get("category") or "") != "passive":
        return ""
    if str(line.get("subtype") or "") not in ("diode", "led", "bead", "fuse"):
        return ""
    mpn = str(line.get("mpn") or "").strip().upper()
    fam = _model_family(mpn)
    if not fam or len(mpn) < 4:
        return ""
    nt = engine._norm_text(title)
    if engine._norm_text(mpn) in nt:
        return ""
    for tok in re.findall(r"[A-Z]{1,4}\d{2,4}[A-Z]{0,3}|\d[A-Z]\d{2,4}[A-Z]{0,3}", str(title).upper()):
        tf = _model_family(tok)
        if not tf or tf[0] != fam[0] or tf[1] == fam[1]:
            continue
        if tok in mpn or mpn in tok:
            continue
        return f"标题里只有同系列 {tok}，没有 BOM 型号 {mpn}（同系列不同号＝另一种料）"
    return ""


def _under_power(line: dict, text: str) -> str:
    """候选明确写了更低的功率 → 拦。没写不拦（标题常常只写封装）。"""
    want = (line.get("params") or {}).get("power_w")
    if not want:
        return ""
    got = _parse_watts(text)
    if got is not None and got < float(want) * 0.9:
        return f"功率不符：BOM 要 {float(want):g}W，候选只有 {got:g}W（会过热）"
    return ""


_CONN_TITLE_RE = re.compile(
    r"排针|排母|端子|接插件|连接器|牛角|简牛|航空插|FPC\s*座|IDC\s*座|排线", re.I
)


_PASSIVE_TITLE_RE = re.compile(r"贴片电阻|贴片电容|二极管|三极管|电感|晶振|磁珠")


def _category_conflict(line: dict, text: str) -> str:
    """整件东西的品类就不对：BOM 要 100uF 电容，搜出来是 PZ254 排针。

    2026-11-18 真机 #24：100uF 行匹配到「PZ254V-11-03P 排针」，以前要等开页、
    到 SKU 层「核实不到容值」才拒 —— 白跑一趟；万一那家 SKU 里恰好写着 100uF，
    就会把排针当电容加进购物车。值闸门永远发现不了「这不是同一类元件」。
    """
    cat = str(line.get("category") or "")
    sub = str(line.get("subtype") or "")
    t = str(text or "")
    if not t:
        return ""
    if cat == "passive" or sub in ("resistor", "capacitor", "inductor", "diode", "led", "crystal"):
        if _CONN_TITLE_RE.search(t) and not _PASSIVE_TITLE_RE.search(t):
            return "品类不符：BOM 要被动件，候选是连接器/排针类"
    elif cat == "connector":
        m = _PASSIVE_TITLE_RE.search(t)
        if m:
            return f"品类不符：BOM 要连接器，候选是{m.group(0)}"
    return ""


def _material_conflict(line: dict, text: str) -> str:
    """材料/结构对不上时返回一句原因；空字符串表示可以继续。"""
    t = str(text or "")
    if not t:
        return ""
    p = line.get("params") or {}

    # 形态先判：贴片/插件、LED 颜色、晶振类型、电感结构、芯片本体/成品板、连接器朝向。
    # 这些和阻值/容值无关，值闸门永远发现不了，但装上去就是错的。
    _why = _category_conflict(line, t)
    if _why:
        return _why
    _why = _mount_conflict(line, t)
    if _why:
        return _why
    for _check in (_led_conflict, _crystal_conflict, _inductor_conflict, _board_conflict, _conn_conflict):
        _why = _check(line, t)
        if _why:
            return _why

    # —— 电阻：类型必须一致（金属膜 ≠ 绕线 ≠ 碳膜）——
    bom_type = _bom_res_type(line)
    if bom_type:
        if "金属膜" in bom_type or "氧化膜" in bom_type:
            for w in _RES_WIRE_WORDS:
                if w in t:
                    return f"候选是「{w}」电阻，BOM 要的是{bom_type}（绕线有感、体积大，不能顶替）"
            for w in _RES_CARBON_WORDS:
                if w in t:
                    return f"候选是碳膜电阻，BOM 要的是{bom_type}（精度/温漂差一个档）"
        if "绕线" in bom_type:
            # 反过来：BOM 明确要绕线，金属膜小功率也不能顶（功率不够）
            pw = p.get("power_w")
            got = _parse_watts(t)
            if pw and got and got + 1e-9 < float(pw) * 0.9:
                return f"候选 {got:g}W，低于 BOM 要的 {float(pw):g}W 绕线功率"

    # —— 电容族系：电解 ≠ 瓷片 ≠ 薄膜/启动电容 ——
    # 数值对得上也可能完全不是一个元件：95µF 的 CBB65 空调启动电容（450V）
    # 和 10×12mm 铝电解容值相同，但极性、体积、纹波电流全都不一样。
    fam = _bom_cap_family(line)
    if fam == "钽":
        # 钽电容有极性、低 ESR、体积小，和铝电解不是同一种料。反过来
        # （BOM 铝电解、候选钽）算升级，可以不拦；但 BOM 明确要钽时，
        # 买成铝电解/瓷片/薄膜就是实打实的错料。
        for w in ("铝电解", "电解", "瓷片", "陶瓷", "薄膜", "CBB", "固态"):
            if w in t and "钽" not in t:
                return f"候选是「{w}」类，BOM 要的是钽电容（低 ESR、有极性，不能互顶）"
    elif fam == "电解":
        for w in _CAP_FILM_WORDS + _CAP_CERAMIC_WORDS:
            if w in t:
                return f"候选是「{w}」类（无极性/薄膜），BOM 要的是铝电解（有极性、体积相近才装得下）"
    elif fam == "瓷片":
        for w in _CAP_ELEC_WORDS + _CAP_FILM_WORDS:
            if w in t:
                return f"候选是「{w}」类，BOM 要的是瓷片/陶瓷电容（脚距与体积不同）"

    # —— 功率：可以放大，但不能离谱（1/4W 换成 2W 还能焊，换 5W 就装不下）——
    pw = p.get("power_w")
    if pw:
        got = _parse_watts(t)
        if got and got > max(0.5, float(pw) * 8):
            return f"候选 {got:g}W，BOM 只要 {float(pw):g}W，体积大一个量级，装不进原焊盘"

    # —— 电容耐压：50V 的瓷片买成 3KV，脚距和体积都不是一回事 ——
    v = p.get("voltage_v")
    if v:
        got_v = _parse_volts(t)
        if got_v and got_v > max(100.0, float(v) * 10):
            return f"候选耐压 {got_v:g}V，BOM 只要 {float(v):g}V，高压规格体积/脚距完全不同"

    # —— 脚距：立创核准了 2.54，就不能买 5.08 ——
    # 只在 BOM 值没有名称/料号冲突时生效：冲突行的立创参数来自那颗错料号，不能当依据。
    if not str(line.get("value_conflict") or "").strip():
        _pitch_txt = str(_lcsc_attrs(line).get("脚间距") or "")
        # 立创属性值就是「2.54mm」这种裸值，不带「P=」前缀，不能拿标题的正则硬套
        _pm = re.search(r"(\d+(?:\.\d+)?)\s*mm", _pitch_txt)
        try:
            bom_pitch = float(_pm.group(1)) if _pm else None
        except (TypeError, ValueError):
            bom_pitch = None
        got_pitch = _parse_pitch_mm(t)
        if bom_pitch and got_pitch and abs(got_pitch - bom_pitch) > 0.3:
            return f"候选脚间距 {got_pitch:g}mm，BOM 是 {bom_pitch:g}mm，焊盘对不上"
    return ""


def _required_spec_kinds(line: dict) -> set:
    """候选标题里**必须**能核实的非数值规格（型号 / 针数）。

    阻值/容值不在这里：淘宝电阻电容普遍是「一个链接挂十几个值」，标题本来
    就不写单一值，硬要求标题命中会把整类商品拒掉。数值交给 SKU 选中值核实：
    有 SKU 的链接按选项比对，单规格商品才回头看标题（见 _value_kinds_missing）。
    """
    p = line.get("params") or {}
    need = set()
    if str(line.get("category") or "") in ("ic", "power") and line.get("mpn"):
        need.add("完整型号")
    if p.get("pins"):
        need.add("针数")
    return need


def _sku_model_conflict(line: dict, text: str) -> str:
    """SKU 选中值里出现了同族不同号的料号 → 返回那个料号。

    实测 SS34 的商品链接，SKU 选项里既有「丝印SS34」也有「SR260贴片SMA丝印SS26」，
    选中 SS26 就是 60V 2A 顶替 40V 3A —— 同系列降规格，和「买错」一样严重。
    标题里 SS34 在列（SS12/14/34/54），所以只看标题永远查不出来。

    只判同族（前缀字母相同、序号不同），避免把 50V / 100nF 这类规格值当料号。
    料号互相包含也算相同（NE555 ⊂ NE555P），否则会把同一个型号判成冲突。
    """
    mpn = str(line.get("mpn") or "").strip().upper()
    m = re.match(r"^([A-Z]{1,4})(\d{2,4})([A-Z]{0,3})$", mpn)
    if not m:
        return ""
    fam = m.group(1)
    for tok in re.findall(r"[A-Z]{1,4}\d{2,4}[A-Z]{0,3}", str(text or "").upper()):
        tm = re.match(r"^([A-Z]{1,4})(\d{2,4})([A-Z]{0,3})$", tok)
        if not tm or tm.group(1) != fam:
            continue
        if tok == mpn or tok in mpn or mpn in tok:
            continue
        return tok
    return ""


def _title_value_kinds(line: dict, title: str) -> set:
    """标题里能否直接读到 BOM 要的数值（单规格商品只能靠这个核实）。

    不用候选自带的 hits：演练/续跑/旧缓存里未必有，直接解析标题更稳。
    """
    p = line.get("params") or {}
    t = str(title or "")
    out = set()
    try:
        if p.get("resistance_ohm") is not None:
            # 淘宝 SKU 值常是「2K2」「1/4W 470R(100只)」这种写法。两个解析器都要试：
            # engine 那个读「2K2」会取到 2Ω，_sku_resistance 才读得出 2.2kΩ ——
            # 只在前者返回 None 时兜底是不够的（它会返回一个错的值）。
            _cands = (engine._parse_resistance(t), _sku_resistance(t))
            for _r in _cands:
                if _r is not None and engine._value_match(
                    float(_r), float(p["resistance_ohm"]), 0.005
                ):
                    out.add("阻值")
                    break
        if p.get("capacitance_f") is not None:
            c = engine._parse_capacitance(t)
            if c is not None and engine._value_match(float(c), float(p["capacitance_f"]), 0.02):
                out.add("容值")
        if p.get("inductance_h") is not None:
            # 系列标题列多档：只要其中一档是 BOM 要的就算读到，不能只看第一个。
            for ind in engine._parse_inductances(t):
                if engine._value_match(float(ind), float(p["inductance_h"]), 0.02):
                    out.add("感值")
                    break
    except Exception:  # noqa: BLE001
        return out
    return out


def _required_value_kinds(line: dict) -> set:
    """这一行必须核实到的数值类别（阻值/容值/感值）。"""
    p = line.get("params") or {}
    out = set()
    if p.get("resistance_ohm") is not None:
        out.add("阻值")
    if p.get("capacitance_f") is not None:
        out.add("容值")
    if p.get("inductance_h") is not None:
        out.add("感值")
    return out


def _title_with_spec(line: dict, item: dict) -> str:
    """标题第一行里没有型号、但商品卡的规格行里有 —— 补进标题再评分。

    淘宝搜索结果把型号印在标题下面的规格行（「4.2E104-BT40UART」），
    title 只取第一行会丢掉型号，H02「标题未出现完整型号」直接把整行判死
    （#107 E104-BT40 就是这么连续 4 条搜索词全灭的）。
    """
    title = (item.get("title") or "").strip()
    mpn = (line.get("mpn") or "").strip()
    if not mpn or not title:
        return title
    if _alnum(mpn) and _alnum(mpn) in _alnum(title):
        return title
    spec = item.get("spec_text") or ""
    if _alnum(mpn) and _alnum(mpn) in _alnum(spec):
        return f"{title} {mpn}"
    return title


def _strip_pkg(text: str) -> str:
    return _PKG_PREFIX.sub("", str(text or ""))


def _sku_resistance(text: str, allow_bare: bool = False):
    t = _strip_pkg(text).replace(" ", "")
    if re.search(r"[pnuµμ]f", t, re.I):
        return None
    # 功率不是阻值。卖家常把功率写在前面（「1/4W 470R」「绕线1W 2.2R」），
    # 而下面的规则都是 ^ 锚定的 —— 不先剔掉，1/4W 里的「1」会被当成 1Ω，
    # 真正的 470R 永远读不到，整组 SKU 全被判成「与 BOM 冲突」。
    t = re.sub(r"\d+\s*/\s*\d+\s*[wW]", "", t)
    t = re.sub(r"\d+(?:\.\d+)?\s*[wW](?![a-zA-Z])", "", t)
    # 容差/精度常写在值前面：「0402 （精度1%）33K 10000个」「±5% 4K7」。
    # 那个 1 会被 ^[^0-9Rr]+ 的剥离当成值的开头，整组选项就读不出阻值 ——
    # 实测 #4（200kΩ）在某家店里全组 33K/36K/39K… 一个都没读出来，
    # 结果按「封装+精度」25 分挑了个 33K，直到值闸门才发现不对。
    t = re.sub(r"[±+]?\s*\d+(?:\.\d+)?\s*%", "", t)
    t = re.sub(r"[（(]?\s*精度", "", t)
    # 前面的中文/字母（金属膜、绕线、色环）也不该挡住数值
    t = re.sub(r"^[^0-9Rr]+", "", t)
    if not t:
        return None
    # 4K7 / 1K2：夹点写法，代表 4.7k / 1.2k
    m = re.match(r"^(\d+)([kKmM])(\d)(?!\d)", t)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * {"k": 1e3, "K": 1e3, "M": 1e6}[m.group(2)]
    # R24 = 0.24Ω
    m = re.match(r"^R(\d{1,2})(?!\d)", t)
    if m:
        return float("0." + m.group(1))
    # 1R2 = 1.2Ω
    m = re.match(r"^(\d+)R(\d)(?!\d)", t)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # 0R / 2.2R / 100R / 24.9R：R 当 Ω 用，这是卖家最常见的写法，
    # 以前只认 Ω，导致「0603 2.2R 0.1% (20个)」这种值解析成 None ——
    # 阻值没参与打分，2.2R 靠封装 +15 就混过了 0Ω/33Ω/100Ω 这些行。
    m = re.match(r"^(\d+(?:\.\d+)?)R(?![a-zA-Z])", t)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)(Ω|欧)?", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    has_ohm = bool(m.group(3))
    rest = t[m.end():]
    if unit:
        mult = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}[unit]
        return val * mult
    if not has_ohm:
        if not allow_bare:
            return None
        if rest and not re.match(r"^(±?\d+(?:\.\d+)?%)", rest):
            return None
    return val


def _sku_capacitance(text: str):
    t = _strip_pkg(text).replace(" ", "")
    # 容值不一定在开头：「25V220UF 6*8」「0402 100NF」都要能取到
    m = re.search(r"(?<![0-9.])(\d+(?:\.\d+)?)(p|n|u|µ|μ|m)[fF](?![a-z])", t, re.I)
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}[m.group(2).lower()]


def _close(a, b, rel=0.005):
    if b == 0:
        return abs(a) < 1e-12
    return abs(a - b) / abs(b) <= rel


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _sku_inductance(text: str):
    t = _strip_pkg(text).replace(" ", "")
    m = re.search(r"(?<![0-9.])(\d+(?:\.\d+)?)(n|u|µ|μ|m)[hH](?![a-z])", t, re.I)
    if not m:
        return None
    return float(m.group(1)) * {"n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}[m.group(2).lower()]


_PACK_ONLY = re.compile(r"^[^\dA-Za-z]{0,6}\s*\d+\s*(?:个|只|片|支|条|颗|件|pcs)\s*(?:推荐|热卖|包邮)?$", re.I)


def _is_pack_only(text: str) -> bool:
    """True for SKU values that only differ by package quantity: 「10个推荐」「100个」."""
    return bool(_PACK_ONLY.match(str(text or "").strip()))


def _pack_in_text(text: str) -> int | None:
    """'15UH(印字150) 10个' -> 10 (pieces per package)."""
    n, _ = _detect_pack(text, explicit_only=False, allow_generic=True)
    return n


def _sku_tolerance(text: str):
    m = re.search(r"(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*%", str(text or ""))
    return float(m.group(1)) if m else None


def _sku_freq(text: str):
    """'50MHz' → 5e7（Hz）。晶振只认频率：8MHz 和 50MHz 是两颗完全不同的料。"""
    m = _VALUE_PATTERNS["frequency"].search(str(text or ""))
    if not m:
        return None
    return float(m.group(1)) * _FREQ_SCALE.get(str(m.group(2) or "").lower(), 1.0)


def _fmt_freq(hz: float) -> str:
    if hz >= 1e9:
        return f"{hz / 1e9:g}GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:g}MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:g}kHz"
    return f"{hz:g}Hz"


def _want_freq(line: dict):
    """BOM 要的频率（Hz）。params.frequency 是 '50MHz' 这类字符串，解析成数。"""
    f = _sku_freq(str((line.get("params") or {}).get("frequency") or ""))
    if f is None:
        f = _sku_freq(str(line.get("desc") or ""))
    return f


def _sku_has_spec(line: dict, text: str) -> bool:
    """这段 SKU 文本里有没有能和 BOM 参数对上号的东西。

    用来认出「非规格组」：像「50个 / 100个 / 500个」这种只在卖包装数量的组，
    以前会被拿去做电压/容值匹配（页面默认「50个」→ 判成电压不符 → 整行卡死）。
    """
    p = line.get("params") or {}
    t = str(text or "")
    if not t:
        return False
    if any(w in t for w in _UNBUYABLE):
        return True
    if "resistance_ohm" in p and _sku_resistance(t) is not None:
        return True
    if "capacitance_f" in p and _sku_capacitance(t) is not None:
        return True
    if "inductance_h" in p and _sku_inductance(t) is not None:
        return True
    if "frequency" in p and _sku_freq(t) is not None:
        return True
    if "tolerance_pct" in p and _sku_tolerance(t) is not None:
        return True
    if "voltage_v" in p and re.search(r"\d+(?:\.\d+)?\s*[vV]\b", t):
        return True
    if "power_w" in p and re.search(r"\d+(?:\.\d+)?\s*[wW]\b", t):
        return True
    if "pins" in p and re.search(r"(?<![\d.])\d{1,2}\s*(?:p|pin|针)(?![0-9a-zA-Z])", t, re.I):
        return True
    if "current_a" in p and engine.title_amps(t):
        return True
    if "pitch_mm" in p and (
        re.search(r"\d+(?:\.\d+)?\s*mm", t, re.I)
        or re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)\d\.\d{1,2}", t, re.I)
    ):
        return True
    mpn_key = _alnum(line.get("mpn"))
    if len(mpn_key) >= 5 and mpn_key in _alnum(t):
        return True
    full_pkg = line.get("package") or ""
    if full_pkg:
        if engine.connector_series(full_pkg, line.get("desc"), line.get("mpn")):
            variants = engine.connector_pkg_variants(full_pkg)
        else:
            variants = engine.pkg_variants(full_pkg.split(" ")[0])
        nt = t.lower()
        if any(v.lower() in nt for v in variants):
            return True
    return False


def _tol_verdict(want: float, got: float) -> tuple[float, str]:
    """容差判定：返回 (分数增减, 给人工看的备注)。

    以前只有「≤要求 +15 / ≤20% +10 / 否则 −40」三档，于是 BOM 要 0.1%、
    实买 ±1% 也照样 +10 分被加购（#70）。这里改成按**倍数**分档：
    宽容差只降权不否决，备注里写清宽了多少倍，报告里能直接看出来。
    """
    if got <= want + 1e-9:
        return 15.0, ""
    ratio = got / max(want, 1e-9)
    if ratio <= 1.5:
        return 8.0, ""
    if ratio <= TOLERANCE_DEMOTE_RATIO + 1e-9:
        return 0.0, f"容差 {got:g}% 略宽于要求 {want:g}%"
    if ratio <= 10:
        return -40.0, f"容差 {got:g}% 宽于要求 {want:g}%（{ratio:.0f}×）"
    return -70.0, f"容差 {got:g}% 远宽于要求 {want:g}%（{ratio:.0f}×）"


def _score_sku_value(line: dict, label: str, text: str) -> float:
    p = line.get("params") or {}
    lab = label or ""
    score = 0.0
    if any(w in str(text) for w in _RISK_WORDS):
        score -= 80
    if any(w in str(text) for w in _UNBUYABLE):
        score -= 150

    # 型号出现在 SKU 文本里是最强信号（连接器/芯片/功率管几乎只按型号区分）
    mpn_key = _alnum(line.get("mpn"))
    if len(mpn_key) >= 5 and mpn_key in _alnum(text):
        score += 60

    if "resistance_ohm" in p:
        r = _sku_resistance(text, allow_bare=("阻" in lab))
        if r is not None:
            score += 50 if _close(r, p["resistance_ohm"]) else -100
    if "capacitance_f" in p:
        c = _sku_capacitance(text)
        if c is not None:
            score += 50 if _close(c, p["capacitance_f"], 0.02) else -100
    if "inductance_h" in p:
        ind = _sku_inductance(text)
        if ind is not None:
            score += 50 if _close(ind, p["inductance_h"], 0.05) else -100
    if "frequency" in p:
        # 晶振：值对上 +50，读出别的频率直接 −100（能不能用的否决在 _sku_veto）
        want_f = _want_freq(line)
        f = _sku_freq(text)
        if f is not None and want_f is not None:
            score += 50 if _close(f, want_f, 0.001) else -100
    if "tolerance_pct" in p:
        tol = _sku_tolerance(text)
        if tol is not None:
            delta, _note = _tol_verdict(float(p["tolerance_pct"]), tol)
            score += delta
        else:
            # 没写容差 ≠ 容差合格。不否决（详情页常把精度放在标题里），也不扣分：
            # 这个组往往根本不是容差组（功率瓦数 / 包装数量），扣 5 会把
            # 只差一档的合法选项压成「无法确认」—— 470Ω 的功率组就这么被连坐的。
            pass
    if "voltage_v" in p:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[vV]\b", str(text))
        if m:
            score += 15 if float(m.group(1)) >= p["voltage_v"] - 1e-9 else -30
    if "power_w" in p:
        # 「1/4W」「1/8W」是卖家的常用写法，直接拿 \d+W 匹配会漏掉，导致 0.25W 的
        # 位子被 1W/2W 的整包选项顶掉（或者反过来被判成无法确认）。
        pw = None
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*[wW](?![a-zA-Z])", str(text))
        if m and float(m.group(2)):
            pw = float(m.group(1)) / float(m.group(2))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*[wW](?![a-zA-Z])", str(text))
            if m:
                pw = float(m.group(1))
        if pw is not None:
            score += 10 if pw >= p["power_w"] * 0.999 else -20
    if "current_a" in p:
        # 保险丝：额定电流必须同一个值。页面默认常常是 0.25A，8A 的位子买错就是整批返工。
        amps = engine.title_amps(text)
        if amps:
            ok = any(abs(a - p["current_a"]) <= max(0.005, p["current_a"] * 0.01) for a in amps)
            score += 50 if ok else -100
    full_pkg = line.get("package") or ""
    # 连接器系列：XH / 排针 / 端子台。库里叫 XH2.50mm-4P，标题写 XH2.54-4P，
    # 直接比字符串必然不中，要换成系列写法再比。
    conn_series = _conn_kind(line)
    pkg = full_pkg.split(" ")[0]
    if conn_series:
        variants = engine.connector_pkg_variants(full_pkg)
    else:
        variants = engine.pkg_variants(pkg) if pkg else []
    if variants:
        nt = str(text).lower()
        if any(v.lower() in nt for v in variants):
            score += 15
        else:
            others = [v for v in ("0201", "0402", "0603", "0805", "1206", "1210", "2512") if v not in variants and v in nt]
            if others:
                score -= 40
    if "channel" in p:
        if re.search(rf"{p['channel']}\s*(?:沟道|channel|mos)", str(text), re.I):
            score += 15
        elif re.search(r"[NP]\s*(?:沟道|channel|mos)", str(text), re.I):
            score -= 40
    if "pins" in p:
        n = _sku_pins(text)
        if n is not None:
            want = int(p["pins"])
            if n == want:
                score += 15
            elif conn_series in ("排针", "排母") and n > want:
                # 排针整条卖（40P 一条自己剪），针数多于需求可用
                score += 5
            else:
                score -= 40
    # 单排 / 双排 是不同料：SKU 里常见 “1*2p / 2*2p” 写法
    raw_params = str((line.get("row") or {}).get("params") or "")
    params_text = raw_params + " " + str(line.get("params_text") or "") + " " + str(line.get("desc") or "")
    rows = re.search(r"([12])\s*[*xX×]\s*(\d+)", str(text))
    if rows:
        if "单排" in params_text and rows.group(1) == "2":
            score -= 45
        if "双排" in params_text and rows.group(1) == "1":
            score -= 45
        if "单排" in params_text and rows.group(1) == "1":
            score += 15
        if "双排" in params_text and rows.group(1) == "2":
            score += 15
    if "pitch_mm" in p:
        txt = str(text)
        m = re.search(r"(\d+(?:\.\d+)?)\s*mm", txt, re.I)
        if not m:
            # XH2.54 / PH2.0 这种把间距写在系列名后面的写法
            m = re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)(\d\.\d{1,2})", txt, re.I)
        # 「单排排针2.54 2P」这种不带 mm 也不带系列前缀的写法，再兜一层
        pitch_val = float(m.group(1)) if m else engine._parse_pitch_loose(txt)
        if pitch_val is not None:
            # 2.50 与 2.54 是同一个 XH 系列，必须视为同档
            score += 15 if engine.pitch_match(pitch_val, p["pitch_mm"]) else -40
    return score


_FORM_HARNESS_WORDS = ("带线", "线束", "渡锡线", "母头带线", "端子线", "杜邦线", "连接线")


def _rows_of(text: str):
    """SKU/标题里的排数：1*40P / 2*2p / 三排 / 单排。认不出返回 None。"""
    t = str(text or "")
    m = re.search(r"(?<![\d.])([1-4])\s*[*xX×]\s*(\d+)", t)
    if m:
        return int(m.group(1))
    for word, n in (("单排", 1), ("双排", 2), ("三排", 3), ("四排", 4)):
        if word in t:
            return n
    return None


def _line_blob(line: dict) -> str:
    """BOM 这一行的全部文本（含原始行）。

    engine 会把「排针 2.54mm 2P 单排」归一成 package=「排针」、把「SMD 保险丝」
    归一成「SMD」，所以判断形态/品类时必须把原始行列进来，否则「单排」「保险丝」
    这些关键信息在归一化时就丢了。
    """
    row = line.get("row") or {}
    parts = [
        line.get("package"), line.get("desc"), line.get("params_text"), line.get("mpn"), line.get("category"),
        row.get("package"), row.get("desc"), row.get("params"), row.get("mpn"),
    ]
    return " ".join(str(x or "") for x in parts)


def _line_rows(line: dict):
    """这一行的排数（1 单排 / 2 双排）。立创属性是权威证据。

    只扫 BOM 文本是不够的：`PZ254-2-20-Z-8.5` 这种料号看不出单排双排，
    封装名又常被归一成「插件」，于是一行「40P 排针」看着像单排 ——
    购物车里就多了条 1*40P 的单排条，装不到 2×20 的焊盘上。
    立创属性里明写「排数=双排 / 插针结构=2x20P」，用它兜住。
    """
    blob = _line_blob(line)
    attrs = ((line.get("lcsc") or {}).get("attrs") or {})
    if isinstance(attrs, dict):
        n = engine._parse_rows(
            " ".join(str(attrs.get(k) or "") for k in ("排数", "插针结构", "Pin Structure"))
        )
        if n:
            return n
    for word, n in (("单排", 1), ("双排", 2), ("三排", 3), ("四排", 4)):
        if word in blob:
            return n
    return None


def _conn_kind(line: dict) -> str | None:
    # 原始封装名（HDR-TH_40P…）和立创品类都算证据：BOM 的 package 列常被归一成
    # 「插件」，光看它连「是不是排针」都判不出来，剪断放宽/母头互斥就全失效。
    kind = engine.connector_series(
        line.get("package") or "", line.get("desc"), line.get("mpn"),
        (line.get("row") or {}).get("package"),
    )
    if kind:
        return kind
    cat = " ".join(
        [str((line.get("lcsc") or {}).get("catalog") or "")]
        + [str(x) for x in ((line.get("lcsc") or {}).get("catalog_path") or [])]
    ).lower()
    if "female" in cat:
        return "排母"
    if "header" in cat or "male pins" in cat:
        return "排针"
    if "terminal block" in cat:
        return "端子台"
    return ""


def _pkg_code(text: str) -> str:
    """从一段文本里读出唯一的封装代号（0402/0603/2010…）。

    只在「这段文字里只出现一个封装」时返回：一个链接常同时挂 0402/0603/0805，
    那时封装本身不构成判据（由别的闸门管），乱猜会把正常选项全否掉。
    """
    got = set(
        re.findall(r"(?<!\d)(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)", str(text or ""))
    )
    return got.pop() if len(got) == 1 else ""


def _sku_veto(line: dict, label: str, text: str) -> str | None:
    """硬属性不一致 → 这个 SKU 值直接出局，不允许靠"总分高"混过。

    以前冲突只扣分（耐压 −30、针数 −40、容值 −100），而一项命中能加 +50，
    于是一个 6.3V 的 47µF 电容（要 16V）能打出 +35 分被自动加购。
    这里把"能不能用"和"值不值"分开：先判能不能用。
    """
    p = line.get("params") or {}
    t = str(text or "")
    if not t:
        return None
    if any(w in t for w in _UNBUYABLE):
        return "要询价/改价，不能直接下单"
    # 形态/结构类：值对了也可能不是同一种器件。放在封装/数值前面判，
    # 报错更直白（「LED 颜色不符」比「容值对不上」更接近真实原因）。
    for _check in (
        _mount_conflict,
        _led_conflict,
        _crystal_conflict,
        _inductor_conflict,
        _board_conflict,
        _conn_conflict,
    ):
        _why = _check(line, t)
        if _why:
            return _why
    _why_pn = _title_model_conflict(line, t)
    if _why_pn:
        return _why_pn
    _why_pw = _under_power(line, t)
    if _why_pw:
        return _why_pw
    # 封装：BOM 要 0402，SKU 写 2010 —— 焊盘从 1.0×0.5mm 变成 5.0×2.5mm，装不上。
    # 标题闸门看不出这个：「贴片电阻 100R 1% 5% 0402/0603/…/2010」标题里 0402 在，
    # 真机上却选中了「2010贴片电阻 100R ±5%（10只）」。
    _bom_pkg = _pkg_code(str(line.get("package") or ""))
    if _bom_pkg:
        _got_pkg = _pkg_code(t)
        if _got_pkg and _got_pkg != _bom_pkg:
            return f"封装 {_got_pkg} ≠ 要求 {_bom_pkg}"
    if "resistance_ohm" in p:
        r = _sku_resistance(t, allow_bare=True)
        if r is not None and not _close(r, p["resistance_ohm"]):
            return f"阻值 {_fmt_val(r)}Ω ≠ 要求 {_fmt_val(p['resistance_ohm'])}Ω"
    if "capacitance_f" in p:
        c = _sku_capacitance(t)
        if c is not None and not _close(c, p["capacitance_f"], 0.02):
            return f"容值 {_fmt_farad(c)}F ≠ 要求 {_fmt_farad(p['capacitance_f'])}F"
    if "inductance_h" in p:
        ind = _sku_inductance(t)
        if ind is not None and not _close(ind, p["inductance_h"], 0.05):
            return f"感值 {_fmt_farad(ind)}H ≠ 要求 {_fmt_farad(p['inductance_h'])}H"
    if "frequency" in p:
        # 晶振：8MHz 的位子焊 50MHz 不起振。频率是「单位相同值不同就判不同」的
        # 硬冲突（_CONFLICT_UNITS 里有它），SKU 层也要一条不漏地拦。
        want_f = _want_freq(line)
        if want_f is not None:
            f = _sku_freq(t)
            if f is not None and not _close(f, want_f, 0.001):
                return f"频率 {_fmt_freq(f)} ≠ 要求 {_fmt_freq(want_f)}"
    # 精密档容差：BOM 要 0.1%/0.05%，选项是 ±1% —— 这是另一个等级的料，
    # 装上分压/采样就偏，宁可 unresolved 也不买（#70）。普通档（1%/5%）只降权。
    # 「没标注」的情况不能在这里拦：容值和容差经常是两个独立 SKU 组
    # （容值组只写 24.9pF），这里一刀切会把容值组全 veto 掉 —— 由
    # suggest_sku 在所有组扫完后统一判（见 _precision 检查）。
    if "tolerance_pct" in p and TOLERANCE_PRECISION_VETO:
        want_tol = float(p["tolerance_pct"])
        got_tol = _sku_tolerance(t)
        if (
            got_tol is not None
            # 0.1%/0.5% 是精密档，1% 也是常见精密档：明确写 5%/10% 就是降等级。
            # 只拦「明确标宽」的选项，没标精度的交给标题级打分，避免整行被判死。
            and want_tol <= TOLERANCE_STRICT_PCT + 1e-9
            and got_tol > want_tol * TOLERANCE_DEMOTE_RATIO + 1e-9
        ):
            return f"容差 {got_tol:g}% 宽于要求 {want_tol:g}%（降规格，不能用）"
    # 介质（P0-4）：要 C0G 买了 X7R 是另一个等级的料 —— SKU 值里明确写了
    # 别的介质就拦（#8 的错买：1nF 50V C0G 5% 买成 1nF_±10%_50V_X7R）。
    # 没写介质不算冲突，由 suggest_sku 挂「未标注」备注让人工留意。
    _dv = _dielectric_verdict(line, t)
    if _dv:
        return _dv
    if "voltage_v" in p:
        volts = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", t)]
        if volts and max(volts) < p["voltage_v"] - 1e-9:
            return f"耐压 {max(volts):g}V < 要求 {p['voltage_v']:g}V"
    if "current_a" in p:
        amps = engine.title_amps(t)
        if amps and not any(abs(a - p["current_a"]) <= max(0.005, p["current_a"] * 0.01) for a in amps):
            return f"电流 {'/'.join(f'{a:g}' for a in amps)}A ≠ 要求 {p['current_a']:g}A"
    if "pins" in p:
        want = int(p["pins"])
        n = _sku_pins(t)
        if n is not None and n != want:
            kind = _conn_kind(line)
            want_rows = _line_rows(line)
            sku_rows = _rows_of(t)
            # 排针整条卖，剪断即可 —— 但只能沿针位方向剪：双排 2*40P 能剪成 2*20P，
            # 单排 1*40P 却剪不出双排。以前这里写死 `rows == 1` 才放行，等于默认
            # 所有排针都是单排，双排的整条货反而被拒。
            if sku_rows and want_rows:
                same_rows = sku_rows == want_rows
            else:
                same_rows = want_rows is None and sku_rows in (None, 1)
            strip_ok = kind in ("排针", "排母") and n > want and same_rows
            if not strip_ok:
                return f"针数 {n} ≠ 要求 {want}"
    # 单排/双排/三排：2*2p 是双排 4 针，顶不了"2P 单排"
    want_rows = _line_rows(line)
    got_rows = _rows_of(t)
    if want_rows and got_rows and got_rows != want_rows:
        return f"排数 {got_rows} ≠ 要求 {want_rows}"
    if "pitch_mm" in p:
        pv = _sku_pitch(t)
        if pv is not None and not engine.pitch_match(pv, p["pitch_mm"]):
            return f"间距 {pv:g}mm ≠ 要求 {p['pitch_mm']:g}mm"
    if "channel" in p:
        ch = str(p["channel"]).upper()
        if re.search(r"[NP]", ch):
            if ch == "N" and re.search(r"[Pp]\s*(?:沟道|channel|mos)", t):
                return "沟道不符（要 N 沟道）"
            if ch == "P" and re.search(r"[Nn]\s*(?:沟道|channel|mos)", t):
                return "沟道不符（要 P 沟道）"
    # 连接器形态：BOM 要板端针座，SKU 却是带线/母头 —— 焊不上板
    blob = _line_blob(line)
    if _conn_kind(line) or "排针" in blob or "端子台" in blob:
        if not any(w in blob for w in ("带线", "线束", "线材", "端子线")):
            if any(w in t for w in _FORM_HARNESS_WORDS):
                return "形态不符：要板端针座，这是带线/母头"
    # 尺寸/耐压：电解电容、电感的实体尺寸只有 SKU 选项里才写得清
    # （标题「10/16/25/35V」多档，实际买哪档看 SKU 文本）
    _sv = _size_verdict(line, t)
    if _sv:
        return _sv
    # 连接器系列：标题层面看不出来的（标题只写「2.54mm 4P」），SKU 里往往写着型号
    want_fam = _conn_family(blob)
    got_fam = _conn_family(t)
    if want_fam and got_fam and want_fam != got_fam:
        return f"连接器系列不符：BOM 是 {want_fam}，SKU 是 {got_fam}"
    return None


def _sku_pins(text: str):
    txt = str(text or "")
    # 行列写法（2*20P / 1*40P）先认：双排 2*20P 是 40 针，直接抓 `(\d+)P`
    # 只会读到 20，于是「2*20P」被当成 20 针 40P 的位子 → 扣分输给 2*40P，
    # 真机上就这么选了整条 80P 的双排（能剪，但多花一倍钱）。
    m = re.search(r"(?<![\d.])([1-4])\s*[*xX×]\s*(\d{1,3})\s*[pP]?(?![\d.])", txt)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"(\d+)\s*(?:p\b|pin|针)", txt, re.I) \
        or re.search(r"(?<![\d.])(\d{1,2})\s*[pP](?=[A-Z]|[-_(\s]|$|[\u4e00-\u9fff])", txt)
    if not m:
        m = re.search(r"(?<![\d.])(\d{1,2})\s*[aA][wW]?(?=[-_)\s]|$|[\u4e00-\u9fff])", txt)
    return int(m.group(1)) if m else None


def _sku_pitch(text: str):
    txt = str(text or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", txt, re.I) \
        or re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)(\d\.\d{1,2})", txt, re.I)
    if m:
        return float(m.group(1))
    return engine._parse_pitch_loose(txt)


def _fmt_val(v):
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    if v >= 1 or v == 0:
        return f"{v:g}"
    return f"{v:g}"


def _fmt_farad(v):
    for unit, scale in (("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= scale * 0.999:
            return f"{v / scale:g}{unit}"
    return f"{v:g}"


_DIELECTRIC_C1 = ("C0G", "NP0", "COG", "NPO")


_DIELECTRIC_C2 = ("X7R", "X5R", "X6S", "X7S", "X8R", "Y5V", "Y5U", "Z5U", "X7T")


def _dielectric_of(text: str) -> str | None:
    """从标题/SKU 文本里认介质。NP0/COG/NPO 归一成 C0G。

    Class II 的 X 系列允许嵌在 MPN 里认（CC0603**KRX7R**9BB103 的 X7R 就在
    料号中间，是真实介质）；C0G/NP0 必须有边界，防误配。
    """
    t = str(text or "").upper()
    for code in _DIELECTRIC_C1:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", t):
            return "C0G"
    for code in _DIELECTRIC_C2:
        if code in t:
            return code
    return None


def _dielectric_verdict(line: dict, text: str) -> str | None:
    """BOM 要求介质、候选明确写了另一种介质 → 拦。没写不算（很多标题不写），只降权提示。"""
    want = (line.get("params") or {}).get("dielectric")
    if not want:
        return None
    got = _dielectric_of(text)
    if got is None:
        return None
    if (want == "C0G") != (got == "C0G"):
        return f"介质不符：要 {want}，这是 {got}（温漂差一个量级，不能互顶）"
    return None


_CONN_FAMILY_PATTERNS = (
    # USB 系列放最前面：TYPE-C 的封装名是 USB-SMD_KH-TYPE-C-2P，按针数/间距
    # 打分的话和 VH3.96 二针座一模一样（实测 100 分），只有认家族才拦得住。
    ("TYPE-C", (r"type\s*-?\s*c(?![a-z0-9])", r"usb\s*-?\s*c(?![a-z0-9])", r"usb\s*3\.[12]")),
    ("MICRO-USB", (r"micro\s*-?\s*usb",)),
    ("MINI-USB", (r"mini\s*-?\s*usb",)),
    ("USB-A", (r"usb\s*-?\s*a(?![a-z0-9])",)),
    ("XH", (r"\bxh\b", r"-xh-", r"xh\s*-?\s*2\.54", r"jst[_\- ]?xh", r"b\d+b-xh")),
    ("PH", (r"phc2\.0", r"-ph-", r"jst[_\- ]?ph\b")),
    ("ZH", (r"zh1\.5", r"-zh-", r"jst[_\- ]?zh\b")),
    ("SH", (r"sh1\.0", r"-sh-", r"jst[_\- ]?sh\b")),
    ("EH", (r"eh2\.5", r"-eh-", r"jst[_\- ]?eh\b")),
    ("GH", (r"gh1\.25", r"-gh-", r"jst[_\- ]?gh\b")),
    ("KF2510", (r"kf\s*-?\s*2510",)),
    ("KF128/2EDG", (r"kf\s*-?\s*128", r"\b2edg", r"2edgv[ck]")),
    ("VH", (r"vh3\.96", r"-vh-")),
    ("MX1.25", (r"mx\s*-?\s*1\.25", r"1\.25\s*mm\s*卧贴")),
)


def _conn_family(text: str) -> str:
    """从标题/封装/型号里认出连接器家族（XH / PH / KF2510 / KF128-2EDG …）。"""
    t = str(text or "").lower()
    for name, pats in _CONN_FAMILY_PATTERNS:
        for pat in pats:
            if re.search(pat, t):
                return name
    return ""


def _size_pairs(text: str) -> list[tuple[float, float]]:
    """抠「长×宽」尺寸：13*20mm / 13.5x12.6 / L7.1-W6.6 / BD6.3。"""
    t = str(text or "")
    out: list[tuple[float, float]] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[x*×]\s*(\d+(?:\.\d+)?)", t, re.I):
        out.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"L\s*(\d+(?:\.\d+)?)\s*[-_ ]\s*W\s*(\d+(?:\.\d+)?)", t, re.I):
        out.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"BD\s*(\d+(?:\.\d+)?)", t, re.I):
        d = float(m.group(1))
        out.append((d, d))
    return out


def _volts(text: str) -> float | None:
    """文本里第一个耐压值（25V / 80v），没有就 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", str(text or ""))
    return float(m.group(1)) if m else None


def _size_verdict(line: dict, title: str) -> str | None:
    """电解电容 / 电感的实体尺寸、耐压对不上 → 否决。

    2026-11-10 真机踩到：BOM 要 25V 220µF 6×8mm，买成 80V 220µF 13×20mm；
    15µH 要 7.1×6.6 焊盘，买成 13.5×12.6mm 15A —— 容值/感值全对，但**装不上去**。
    只看电气参数的匹配永远发现不了这种错。
    """
    blob = _line_blob(line)
    is_elec = ("电解" in blob) or ("CAP-TH_BD" in blob.upper())
    is_ind = ("电感" in blob) or re.search(r"IND-|_L\d", blob, re.I) is not None
    if not (is_elec or is_ind):
        return None
    req_sizes = _size_pairs(blob)
    cand_sizes = _size_pairs(title)
    if req_sizes and cand_sizes:
        # BOM 只有 BD10.0（圆柱直径、没给长宽）时只能比直径：
        # 候选写「10x17」的 17 是高度，按最大边比会把对的电容判成装不上。
        _bd = re.search(r"BD\s*(\d+(?:\.\d+)?)", blob, re.I)
        _has_lw = re.search(
            r"\d+(?:\.\d+)?\s*[x*×]\s*\d|L\s*\d+(?:\.\d+)?\s*[-_ ]\s*W\s*\d", blob, re.I
        )
        kind = "电解电容" if is_elec else "电感"
        if _bd and not _has_lw:
            req_d = float(_bd.group(1))
            cand_d = min(min(a, b) for a, b in cand_sizes)
            if cand_d > req_d * 1.25:
                return (
                    f"{kind}体积不符：BOM 直径 {req_d:g}mm，候选 {cand_d:g}mm"
                    "（大概率装不进焊盘）"
                )
        else:
            req_max = max(max(a, b) for a, b in req_sizes)
            cand_max = max(max(a, b) for a, b in cand_sizes)
            if cand_max > req_max * 1.35:
                return (
                    f"{kind}体积不符：BOM 最大边 {req_max:g}mm，候选 {cand_max:g}mm"
                    "（大概率装不进焊盘）"
                )
    if is_elec:
        req_v = _volts(line.get("mpn") or "") or _volts((line.get("row") or {}).get("mpn") or "")
        # 标题里列了「10/16/25/35V」这种多档电压的，第一个数字不代表实际买到的那档，
        # 不能按它判耐压（否则好行被误杀）。这种交给 SKU 层用实际选中值判。
        cand_vs = {v for v in re.findall(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", str(title or ""))}
        if req_v and len(cand_vs) == 1:
            cand_v = float(next(iter(cand_vs)))
            if cand_v < req_v - 1e-9:
                return f"电解电容耐压不够：BOM 要 {req_v:g}V，候选只有 {cand_v:g}V"
    return None


def _candidate_veto(line: dict, title: str) -> str | None:
    """候选标题层面的"料不对"判断（在打开商品之前就拦掉，省时间也防买错）。"""
    t = str(title or "")
    if not t:
        return "标题为空"
    _dv = _dielectric_verdict(line, t)
    if _dv:
        return _dv
    # 品类不符：5kΩ 这类冷门阻值搜出来整页都是压敏电阻，SKU 里也确有数值选项，
    # 光看数值能混过去（真机把压敏电阻当 5kΩ 电阻买回来了）。
    _sub = str(line.get("subtype") or "")
    if _sub == "resistor" and re.search(r"压敏|热敏|湿敏|光敏|电位器|可调电阻|敏感电阻", t):
        return "品类不符：要固定电阻，候选是压敏/热敏/可调电阻"
    if _sub == "capacitor" and re.search(r"压敏电阻|热敏电阻|电阻器", t) and "电容" not in t:
        return "品类不符：要电容，候选是电阻"
    # 形态/材料/同族不同号：贴片当插件、有源当无源、共模当单绕组、SS26 顶 SS34…
    # 以前这些只在打开商品后才查（甚至根本不查），错货会先被评分排到前面。
    _mat = _material_conflict(line, t)
    if _mat:
        return _mat
    _pn = _title_model_conflict(line, t)
    if _pn:
        return _pn
    # 尺寸/耐压（电解、电感）：容值对了也可能装不上，见 _size_verdict 注释
    _sv = _size_verdict(line, t)
    if _sv:
        return _sv
    # 连接器系列：KF2510 与 XH 同为 2.54 但互不兼容，只对针数/间距会被它骗过去
    blob = _line_blob(line)
    want_fam = _conn_family(blob)
    got_fam = _conn_family(t)
    if want_fam and got_fam and want_fam != got_fam:
        return f"连接器系列不符：BOM 是 {want_fam}，候选是 {got_fam}"
    if any(w in blob for w in ("保险丝", "熔断")):
        if not any(w in t for w in ("保险丝", "熔断", "Fuse", "fuse", "FUSE")):
            return "不是保险丝（标题没有保险丝/熔断字样）"
    if _conn_kind(line) or "排针" in blob or "端子台" in blob:
        if not any(w in blob for w in ("带线", "线束", "线材", "端子线")):
            if any(w in t for w in _FORM_HARNESS_WORDS):
                return "形态不符：要板端针座，这是带线/母头"
    return None


def _title_has_mpn(line: dict, title: str) -> bool:
    """标题里有没有这个料号。只用来**排序优先**，不做否决（国产替代很常见）。"""
    mpn = _alnum(line.get("mpn"))
    if len(mpn) < 6:
        return True
    return mpn in _alnum(title)


def _tie_pick(line: dict, options: list):
    """同分选项里挑最贴合 BOM 的：电压正好等于要求 > 大于要求 > 没有电压；再比包装小。"""
    p = line.get("params") or {}

    def key(pair):
        _score, v = pair
        text = str(v.get("text", ""))
        rank = []
        if "voltage_v" in p:
            m = re.search(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", text)
            if m:
                rank.append(0 if abs(float(m.group(1)) - p["voltage_v"]) < 1e-9 else 1)
            else:
                rank.append(2)
        if "tolerance_pct" in p:
            # 同分时优选「容差更紧或等于要求」；没写容差的排最后
            got = _sku_tolerance(text)
            if got is None:
                rank.append(2)
            elif got <= float(p["tolerance_pct"]) + 1e-9:
                rank.append(0)
            else:
                rank.append(1)
        rank.append(_pack_in_text(text) or 1)
        return tuple(rank)

    return min(options, key=key)[1]


def condense_sku_groups(groups: list, max_values: int = 60, max_groups: int = 8) -> list:
    """把页面抓到的 SKU 组压成能落盘/能回放的轻量副本。

    只留判定真正用到的三样：组名、值文本、是否禁用。一个「颜色分类」组
    常有上百个值，原样抄进结果 JSON 会让进度文件暴涨；而回放要复现的恰恰是
    「这一档 SKU 里根本没有 5kΩ」——那只需要值文本就够了。字段名与页面结构
    保持一致，回放时可以直接喂给 `suggest_sku` / `_material_conflict`。
    """
    out = []
    for g in (groups or [])[:max_groups]:
        if not isinstance(g, dict):
            continue
        vals = []
        for v in (g.get("values") or [])[:max_values]:
            if not isinstance(v, dict):
                continue
            text = str(v.get("text") or "").strip()
            if not text:
                continue
            vals.append(
                {
                    "text": text,
                    "disabled": bool(v.get("disabled")),
                    "selected": bool(v.get("selected")),
                }
            )
        label = str(g.get("label") or "").strip()
        if label or vals:
            out.append({"label": label, "values": vals})
    return out


def _fmt_need_values(line: dict) -> str:
    """把 BOM 要的阻值/容值/感值写成人话，用于「这家店没有这一档」的提示。"""
    p = line.get("params") or {}
    out = []
    for key, kind in (
        ("resistance_ohm", "阻值"),
        ("capacitance_f", "容值"),
        ("inductance_h", "感值"),
        ("frequency", "频率"),
    ):
        if p.get(key) is None:
            continue
        try:
            out.append(f"{kind} {engine._fmt_param_value(key, p[key])}")
        except Exception:  # noqa: BLE001
            continue
    return "、".join(out)


def _reads_value(line: dict, text: str) -> bool:
    """这段文字里有没有读出「BOM 关心的那类数值」（对不对不管）。

    用来区分两种完全不同的商品：
      · 「在卖阻值，但没有 BOM 要的那一档」→ 该拒，理由写「这家没有 75kΩ」；
      · 「整页不写阻值」（值在型号里/藏在默认项）→ 不该在这一层判死。
    """
    p = line.get("params") or {}
    t = str(text or "")
    if not t:
        return False
    try:
        if p.get("resistance_ohm") is not None and _sku_resistance(t, allow_bare=True) is not None:
            return True
        if p.get("capacitance_f") is not None and _sku_capacitance(t) is not None:
            return True
        if p.get("inductance_h") is not None and _sku_inductance(t) is not None:
            return True
        if p.get("frequency") is not None and _sku_freq(t) is not None:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def suggest_sku(line: dict, groups: list, title: str = "") -> tuple[list, list]:
    suggestions, unresolved = [], []
    # P0-3：精密档（要求 ≤0.5%）最终选中的 SKU 组合里必须能读到容差，
    # 否则不自动买。放在组循环外统一判 —— 容值/容差经常是独立 SKU 组，
    # 单组里判「未标注」会把容值组全误杀；「任一选项标注了就放行」也不行，
    # 混合组里 ±1% 被 veto 后，未标注的「24.9K」会成唯一选项被买走（#70 形态）。
    _p = line.get("params") or {}
    _precision = (
        TOLERANCE_PRECISION_VETO
        and "tolerance_pct" in _p
        and float(_p["tolerance_pct"]) <= TOLERANCE_PRECISION_PCT + 1e-9
    )
    for group in groups or []:
        label = (group.get("label") or "").strip()
        values = group.get("values") or []
        enabled = [v for v in values if not v.get("disabled") and str(v.get("text") or "").strip()]
        if not values:
            continue
        if not enabled:
            unresolved.append({"label": label, "reason": "该组没有可选项（可能缺货）", "values": []})
            continue

        # 硬属性冲突的值直接出局：能不能用，和值不值，是两件事。
        keeps, vetoed = [], []
        for v in enabled:
            why = _sku_veto(line, label, v.get("text", ""))
            if why:
                vetoed.append((v, why))
            else:
                keeps.append(v)
        if not keeps:
            # 只报「第一个被否的选项」会误导：一组里 100PF 是值不对、330PF 是介质不对，
            # 人看到「容值 100pF ≠ 要求 330pF」会以为这家有 330pF 只是排版问题。
            # 按「问题类型」去重后列出来，才看得出这家到底缺什么。
            _kinds: list = []
            for _v, _why in vetoed:
                _k = re.split(r"[ ：]", _why, maxsplit=1)[0].strip()[:10]
                if _k and _k not in _kinds:
                    _kinds.append(_k)
            reason = "所有选项都与 BOM 冲突"
            if _kinds:
                reason += "（" + "；".join(_kinds[:3]) + "）"
            unresolved.append(
                {
                    "label": label,
                    "reason": reason,
                    "values": [v.get("text") for v in enabled[:20]],
                }
            )
            continue
        enabled_ok = keeps

        scored = sorted(
            [(_score_sku_value(line, label, v.get("text", "")), v) for v in enabled_ok],
            key=lambda x: -x[0],
        )
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else -999
        selected = [v for v in enabled_ok if v.get("selected")]

        pick, auto, note = None, False, ""
        _any_spec = any(_sku_has_spec(line, v.get("text", "")) for v in enabled_ok)
        _sel_score = (
            _score_sku_value(line, label, selected[0].get("text", "")) if selected else -999
        )
        # 页面默认值只在「和最优选项差不多好」时保留。以前只要它得正分就保留 ——
        # 实测 BOM 要 1%，选项里 1% 和 5% 都有，页面默认 5%（得 25 分，正分），
        # 于是自动买了 ±5% 的货，把 80 分的 1% 档跳过去：这不是"遵循买家默认"，
        # 是自动降精度。差 30 分以上就当默认值不对，按 BOM 挑。
        if selected and _sel_score >= 10 and best_score - _sel_score < 30:
            pick, auto = selected[0], True
            note = "保留页面已选值"
        elif not _any_spec:
            # 整组没有一个值能对上 BOM 参数（纯包装数量 / 颜色 / 产地）：
            # 它不是规格维度，保留页面已选或取最小包装，不能因为「认不出来」把整行卡死。
            pick = selected[0] if selected else min(
                enabled_ok, key=lambda v: (_pack_in_text(v.get("text", "")) or 1)
            )
            auto = True
            note = "该组不区分电气规格，保留默认"
        elif best_score >= 10 and (len(scored) == 1 or best_score - second >= 5):
            pick, auto = best, True
            note = "按 BOM 参数匹配"
        elif len(enabled_ok) == 1 and best_score > -50:
            # 唯一选项（已过 veto）也要分数不为负才自动加购。
            pick, auto = enabled_ok[0], True
            note = "该组唯一可选"
        elif best_score >= 10 and len(scored) > 1:
            # 分数打平：以前这里是 min(包装数)，包装数解析不出来就退化成
            # 「取选项列表第一个」—— 电压组 [6.3,10,16,25,50]V 里 10V 以上
            # 全是 +15 平手，结果挑中了排第一的 6.3V（−30 分那个）。
            # 现在同分里按「电压正好等于要求 → 包装最小」挑，绝不按列表顺序。
            ties = [x for x in scored if x[0] >= best_score - 0.5]
            pick = _tie_pick(line, ties)
            auto = True
            note = "同型号多个选项，取最贴合 BOM 的"
        elif len(enabled_ok) > 1 and all(_is_pack_only(v.get("text", "")) for v in enabled_ok):
            # 这一组只是在卖包装数量（10个 / 100个），没有规格差异 —— 取最小包装，
            # 数量按 BOM 折算，不放大采购量。
            pick = min(enabled_ok, key=lambda v: (_pack_in_text(v.get("text", "")) or 1))
            auto = True
            note = "该组只区分包装数量，取最小包装"
        elif best_score <= -50:
            _reasonable = "所有选项都与 BOM 冲突"
            _need = _fmt_need_values(line)
            if _need and not _title_value_kinds(line, " ".join(v.get("text", "") for v in enabled_ok)):
                # 整组都是数值、却没有一个是 BOM 要的那一档 —— 这是「这家店不卖这个值」，
                # 不是「BOM 有问题」。报「与 BOM 冲突」会把人引去改 BOM。
                _sample = "、".join(str(v.get("text") or "")[:14] for v in enabled_ok[:5])
                _reasonable = f"该店没有 BOM 要的 {_need}（只有 {_sample} 等），换下一家"
                _log(f"[sku] {_reasonable}")
            unresolved.append(
                {
                    "label": label,
                    "reason": _reasonable,
                    "values": [v.get("text") for v in enabled[:20]],
                }
            )
            continue
        else:
            # 页面默认值可能和 BOM 不符，不能「先加了再说」——交给人工选
            hint = f"页面默认「{selected[0].get('text')}」无法确认符合 BOM" if selected else "无法唯一匹配"
            unresolved.append(
                {
                    "label": label,
                    "reason": hint + "，请在页面里手动选择",
                    "values": [v.get("text") for v in enabled[:30]],
                }
            )
            continue

        # 备注里写清容差放宽了多少 —— 报告/日志要能直接看出「这个买宽了」。
        # 只在**选中的值自己写了容差**时加，否则「容值/电压」这类组也会被
        # 挂上「容差未标注」，纯属噪音。
        if "tolerance_pct" in (line.get("params") or {}):
            _got_tol = _sku_tolerance(pick.get("text", ""))
            if _got_tol is not None:
                _d, _n = _tol_verdict(float(line["params"]["tolerance_pct"]), _got_tol)
                if _n:
                    note = f"{note}；{_n}" if note else _n
        # P0-4：BOM 要求介质但选中值没写 —— 不拦（很多商家不写），但必须留痕
        if (line.get("params") or {}).get("dielectric") and not _dielectric_of(pick.get("text", "")):
            note = f"{note}；介质未标注，请人工核对是否 {line['params']['dielectric']}" if note \
                else "介质未标注，请人工核对"

        suggestions.append(
            {
                "label": label,
                "value": pick.get("text", ""),
                "auto": auto,
                "score": round(best_score, 1),
                "note": note,
            }
        )
    # P0-3 收口：精密档但最终选中的组合读不出容差 → 不自动买，交人工/换店。
    if _precision and suggestions:
        _combined = " ".join(s.get("value", "") for s in suggestions)
        if _sku_tolerance(_combined) is None:
            unresolved.append(
                {
                    "label": "容差",
                    "reason": (
                        f"精密档（要求 {_p['tolerance_pct']:g}%）：SKU 组合里读不到容差标注，"
                        "无法确认精度，不予自动购买"
                    ),
                    "values": [],
                }
            )
            return [], unresolved
    # 最后一道：把标题和选中的值拼起来，BOM 要的那个值到底读不读得到。
    # 前面每组各管各的，很容易出现「封装组选了 0402、包装组选了 100只」，
    # 却没有一组真正携带阻值 —— 真机上 #5（75kΩ）就这样选出了一家只卖
    # 0.15R/0.68R 的店，靠 `_add_line_to_cart` 里的事后闸门才拦住。
    # 这里提前判掉，退回的失败原因才写得准（「这家没有 75kΩ」而不是「核实不到阻值」）。
    _need_kinds = _required_value_kinds(line)
    if _need_kinds and suggestions and not unresolved:
        _texts = [str(title or "")] + [
            str(v.get("text") or "") for g in (groups or []) for v in (g.get("values") or [])
        ]
        # 只有当这件商品**自己提到过这类值**时才有资格判「没有这一档」：
        # 有些链接整页不写阻值（值只在标题的型号里，或靠默认项），那种情况不该
        # 在这里判死，交给执行层的事后闸门（它拿标题一起核）。这条只负责
        # 「明明在卖阻值，却没有 BOM 要的那个」——真机上 75kΩ 选中只卖 0.15R 的店。
        if any(_reads_value(line, t) for t in _texts):
            # 逐段核实，不能拼成一整串：系列链接的标题常是「0402 30K 33K 36K 39K…」，
            # 两个解析器都只认第一个数值（30K），拼起来判就等于「标题里永远没有 BOM 的值」。
            # 实测 #4（200kΩ）那家店，标题列了 8 个档位、SKU 里明确写着 33K，
            # 页级闸门仍报「这家没有这一档」—— 换店是对的，理由却是错的。
            _proved: set = set()
            for _t in [str(title or "")] + [str(s.get("value") or "") for s in suggestions]:
                _proved |= _title_value_kinds(line, _t)
            _miss = _need_kinds - _proved
            if _miss:
                unresolved.append(
                    {
                        "label": "整页",
                        "reason": (
                            f"标题和选中的 SKU 都读不到 BOM 要的 {_fmt_need_values(line)}，"
                            "这家没有这一档，换下一家"
                        ),
                        "values": [],
                    }
                )
                return [], unresolved
    return suggestions, unresolved


def groups_without_selection(groups: list) -> list:
    missing = []
    for g in groups or []:
        label = (g.get("label") or "").strip()
        values = g.get("values") or []
        if not label or not values:
            continue
        if not any(v.get("selected") for v in values):
            missing.append({"label": label, "values": [v.get("text") for v in values if not v.get("disabled")][:20]})
    return missing

