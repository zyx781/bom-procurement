"""BOM 参数解析：封装归一化、阻容感/电压/电流/引脚等参数抽取、目录归类。

从 engine.py 拆出来的纯字符串处理层：只依赖 `re`，不碰浏览器、网络、文件。
engine.py 仍把这里的名字 import 回去，所以 `engine.parse_params` / `engine.classify`
这些老用法一行都不用改。
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# 规范化 / 参数识别
# --------------------------------------------------------------------------

UNITS = "Ω欧姆欧"

_RE_RES = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])?\s*(?:Ω|欧姆|欧|ohms?|ohm)", re.I)
_RE_RES_4K7 = re.compile(r"(\d+)[kK](\d)(?![A-Za-z0-9])")
_RE_RES_K = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])\s*(?:电阻|resistor)")
# 单位字母要大写小写都收：淘宝标题/SKU 里「10NF」「100UF」「4U7H」随处可见，
# 只认小写会让一行本来能买对的料判成「核实不到容值」（实测 #4 的 50V 瓷片就这么被否掉）。
# 但 M/m 单独处理：大写 M 在元件标注里是「兆」，当毫法读会差 9 个数量级，
# 宁可不认，也不能认错。
_RE_CAP = re.compile(
    r"(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|P|N|U|m)\s*[fF](?![A-Za-z])"
)
# 电感单位同样要收大写：淘宝 SKU 常写「3MH」表示 3mH，不是 3 兆亨。
# 电感没有「兆亨」这种常用档，M 在这里按毫处理；与电容的 MF 不同（电容大写 M 仍不认）。
_RE_IND = re.compile(
    r"(\d+(?:\.\d+)?)\s*([uµμnUNmM])\s*[hH](?![A-Za-z])"
)
# 单位后面紧跟中文在淘宝标题里是常态（「95UF单盒精装」「1/4W金属膜」「50V贴片」）。
# 用 \b 收尾会判成「没有词边界」，整段规格读不出来 —— 中文是 Unicode 词字符，
# F「F」和「单」之间不存在边界。改成「后面不是英文字母」才符合真实文本。
_RE_POWER_FRAC = re.compile(r"(\d+)\s*/\s*(\d+)\s*[wW](?![A-Za-z])")
# 前面不能紧贴字母/数字：`1N5819W` 里的 `5819W` 是料号后缀，不是 5819 瓦。
# 这条以前把肖特基二极管读成「5819W」，摘要和功率闸门都跟着错。
_RE_POWER = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[wW](?![A-Za-z])")
_RE_VOLT = re.compile(r"(\d+(?:\.\d+)?)\s*[vV](?:dc|DC)?(?![A-Za-z])")
_RE_TOL = re.compile(r"(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*%")
# 电流：必须自成一个 token。「AO3400A」里的 3400 不是电流 —— 前面紧挨字母的要排除。
_RE_CURR = re.compile(r"(?<![A-Za-z0-9._])(\d+(?:\.\d+)?)\s*(m?[aA])(?![A-Za-z0-9])")
_RE_RDS = re.compile(r"(?:rds\s*\(?\s*on\s*\)?|导通电阻)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(mΩ|m\s*ohm|毫欧)", re.I)
_RE_RDS_LOOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(mΩ|毫欧)")
_RE_TYPE = re.compile(r"([NP])\s*(?:沟道|channel|mos)", re.I)
_RE_PITCH = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.I)
_RE_PINS = re.compile(r"(\d+)\s*(?:pin|针|p\b)", re.I)
# 1x02 / 2x20：Altium 封装名的「排数 x 每排针数」，针数取后一段
_RE_PIN_PAIRS = re.compile(r"(?<![\d.])([1-9])\s*[xX×]\s*(\d{1,3})(?![\d.])")

# 电阻的“省略单位”写法（Altium/立创 BOM 最常见）：33R / 120R / 0R / 4R7 / 100k / 2.2k / 1M
# R 当小数点（4R7=4.7Ω），或直接当单位（33R=33Ω）；k/M 省略 Ω。
_RE_RES_R_UNIT = re.compile(r"(?<![0-9A-Za-z])(\d+(?:\.\d+)?)\s*[Rr](?![0-9A-Za-z])")
_RE_RES_R_DOT = re.compile(r"(?<![0-9A-Za-z])(\d+)[Rr](\d+)(?![0-9A-Za-z])")
_RE_RES_KM = re.compile(r"(?<![0-9A-Za-z])(\d+(?:\.\d+)?)\s*([kKmM])(?![0-9A-Za-zΩ])")

_METRIC = {"0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225", "1812": "4532", "2512": "6332"}
_IMPERIAL = {v: k for k, v in _METRIC.items()}
_IMPERIAL["0603"] = "0201"  # 公制 0603 = 英制 0201，会有歧义，标注时以用户写法为准

_RE_PKG_NAMED = re.compile(
    r"(SOT-?23(?:-?\d)?|SOT-?223|SOT-?323|SOT-?363|SOT-?563|SOT-?89|SC-?70|SOD-?882"
    r"|SOD-?123|SOD-?323|SOD-?523"
    r"|TO-?220F?|TO-?252|TO-?263|TO-?247|TDSON-?\d+|WSON-?\d+|LFPAK\d*|DPAK|POWERPAK"
    r"|(?:L|T)?QFP-?\d+|[VP]?QFN-?\d+|[VP]?DFN-?\d+"
    r"|TSSOP-?\d+|MSOP-?\d+|SSOP-?\d+|(?:E|T|L|V)?(?:SOP|SOIC)-?\d+|DIP-?\d+"
    r"|WLCSP-?\d+|LGA-?\d+|BGA-?\d+"
    r"|SMA|SMB|SMC)(?![0-9A-Za-z])",
    re.I,
)

# 英制/公制芯片尺寸代码；允许前面带字母（Altium 的 C0402 / R0603 / LED0603），
# 只要左右不紧邻其它数字就不算误命中（避免把 5430、8701 之类的料号当封装）。
_RE_PKG_CHIP = re.compile(
    r"(?<![0-9])(0201|0402|0603|0805|1206|1210|1812|2512|1005|1608|2012|3216|3225|4532|6332)(?![0-9])"
)

# 只在封装字段里出现、用来判定贴片/插件的词
_RE_PKG_TH = re.compile(r"(?:^|[_\-/ ])TH(?:[_\-/ ]|$)|CONN-TH|CAP-TH|DIP-?TH", re.I)
_RE_PKG_SMD = re.compile(r"SMD", re.I)


def _altium_package(raw: str) -> str:
    """Altium/KiCad 库名兜底。

    这类封装名（MainParts:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical）不是标准封装代码，
    但仍然带够信息：认出常见家族就给一个能读、能搜、能当门槛的名字，而不是留空。
    """
    t = str(raw or "").strip()
    if not t:
        return ""
    low = t.lower()
    pitch = re.search(r"[_\-]P\s*(\d+(?:\.\d+)?)\s*mm", t, re.I)
    pitch_txt = f"{pitch.group(1)}mm" if pitch else ""
    pairs = re.search(r"(?<!\d)(\d+)\s*[xX]\s*(\d+)(?!\d)", t)
    pins = int(pairs.group(2)) if pairs else None

    if re.search(r"jst|xh[-_ ]?b\d|_xh[-_ ]|xh\d", low):
        tail = f"-{pins}P" if pins else ""
        return f"XH{pitch_txt or '2.54mm'}{tail}"
    if re.search(r"pinheader|pin_header|header_\d|排针|pinsocket|pin_socket|排母", low):
        name = "排母" if re.search(r"pinsocket|pin_socket|排母", low) else "排针"
        bits = [name, pitch_txt, f"{pins}P" if pins else ""]
        # 1x02 / 2x20：排数是不同料，必须写进封装名，否则 H01 分不出单排双排
        if pairs:
            bits.append("双排" if int(pairs.group(1)) == 2 else "单排")
        return " ".join(b for b in bits if b)
    if re.search(r"terminalblock|terminal_block|端子", low):
        return f"端子台 {pitch_txt}".strip()
    if re.search(r"testpoint", low):
        return "测试点"
    if re.search(r"fuse|保险丝|自恢复", low):
        return "SMD 保险丝"
    if re.search(r"tango|tang_?nano|gowin|envelope", low):
        return "模块"
    if re.search(r"radial|直插|插件|dip", low):
        return "插件"
    if re.search(r"coilcraft|xfl\d{4}|xal\d{4}|inductor|电感", low):
        return "SMD 电感"
    if re.search(r"abm\d|osc|xtal|crystal|晶振", low):
        return "SMD 晶振"
    if re.search(r"tango|e104|module|模块|开发板", low):
        return "模块"
    # 芯片厂的封装代号（TI_DDA0008B / Texas_DSG0008A）：至少认出是贴片，别留空
    if re.search(r"exactland|provisional|verified|land_?pattern", low):
        return "SMD"
    return ""


def norm_package(text: str) -> str:
    """识别并规范化封装；同时给出公制换算。

    兼容 Altium/KiCad 的写法：C0402、R0603、LED0603-RD、CONN-TH_...、VQFN-24_...、
    ESOP-8_...、TDSON-8_...、SOD-123_L2.7-... 等（此前因 \\b 词边界全部识别失败）。
    """
    if not text:
        return ""
    raw = str(text)
    if any(w in raw for w in ("插件", "直插", "通孔")):
        return "插件"
    # 测试点/焊盘不是标准封装，但它是这类行唯一的品类线索（名称常是网络名 MCU STATUS）。
    # 以前这里返回 ""，后面就只能拿网名去搜，召回到双排母、接线端子这类完全无关的商品。
    if re.search(r"测试点|焊盘|测试环|test\s*point", raw, re.I):
        core = re.sub(r"(?i)test\s*point", "测试点", raw)
        core = re.sub(r"\s+", " ", core).strip(" ,;")
        return core
    t = raw.upper().replace("　", " ")

    m = _RE_PKG_NAMED.search(t)
    if m:
        pkg = m.group(1).upper().replace(" ", "")
        pkg = re.sub(r"^[VP]V?QFN", "QFN", pkg)
        pkg = re.sub(r"^[VP]DFN", "DFN", pkg)
        pkg = re.sub(r"^(?:E|T|L|V)(SOP-\d+)$", r"\1", pkg)  # ESOP/TSOP/LSOP/VSOP → SOP
        pkg = re.sub(r"^QFP", "QFP", pkg)
        if "-" not in pkg and re.match(r"^(SOT|TO|LQFP|TQFP|QFN|DFN|TSSOP|SSOP|SOP|SOIC|DIP|TDSON)", pkg):
            pkg = re.sub(r"^([A-Z]+)(\d)", r"\1-\2", pkg)
        if pkg in _METRIC:
            return f"{pkg} ({_METRIC[pkg]})"
        if pkg in _IMPERIAL:
            return f"{_IMPERIAL[pkg]} ({pkg})"
        return pkg

    m = _RE_PKG_CHIP.search(t)
    if m:
        pkg = m.group(1)
        if pkg in _METRIC:
            return f"{pkg} ({_METRIC[pkg]})"
        if pkg in _IMPERIAL:
            return f"{_IMPERIAL[pkg]} ({pkg})"
        return pkg

    if _RE_PKG_TH.search(t):
        return "插件"
    if _RE_PKG_SMD.search(t):
        return "SMD"
    return _altium_package(raw)


def pkg_variants(pkg: str) -> list:
    if not pkg:
        return []
    base = pkg.split(" ")[0].upper()
    out = [base]
    if base in _METRIC:
        out.append(_METRIC[base])
    for imp, met in _METRIC.items():
        if met == base:
            out.append(imp)
    return list(dict.fromkeys(out))


def connector_pkg_variants(pkg: str) -> list:
    """连接器封装的「标题可比对形式」。

    Altium 库名（XH2.50mm-4P / 排针 2.54mm 2P）跟淘宝标题写法对不上，
    这里换成标题里真会出现的写法：XH2.54-4P / XH2.5-4P / B4B-XH / 排针 / 端子台。
    """
    t = str(pkg or "").strip()
    if not t:
        return []
    out = [t.upper()]
    series = connector_series(t)
    pins = _parse_pins(t)
    pitch = _parse_pitch(t) or _parse_pitch_loose(t)
    if series == "XH":
        # JST XH 真实 2.50mm，淘宝通用 XH2.54：两种都要能命中
        for pv in ("2.54", "2.5"):
            if pins:
                out += [f"XH{pv}-{int(pins)}P", f"XH{pv}{int(pins)}P", f"XH{pv}-{int(pins)}"]
        if pins:
            out.append(f"B{int(pins)}B-XH")
    elif series in ("排针", "排母"):
        out.append(series)
        if pitch:
            out.append(f"{pitch:g}{series}")
    elif series == "端子台":
        out += ["端子台", "接线端子", "kf128", "mkds"]
    elif series:
        out.append(series)
        if pitch:
            out.append(f"{pitch:g}mm{series}")
    return list(dict.fromkeys(v.lower() for v in out if v))


def parse_package(text: str) -> str:
    if not text:
        return ""
    # 测试点/焊盘要整体保留（「测试点 D1.0mm」里直径是唯一能搜的词），
    # 按空格切开只会剩下「测试点」，搜出来的全是无关商品。
    if re.search(r"测试点|焊盘|测试环|test\s*point", str(text), re.I):
        return norm_package(str(text))
    for token in re.split(r"[\s,，;；/|]+", str(text)):
        p = norm_package(token)
        if p:
            return p
    return norm_package(str(text))


def _parse_resistance(t, loose=False):
    m = _RE_RES.search(t)
    if m:
        val = float(m.group(1))
        u = m.group(2) or ""
        mult = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}.get(u, 1.0)
        return val * mult
    m = _RE_RES_4K7.search(t)
    if m and ("电阻" in t or "resistor" in t.lower()):
        return float(f"{m.group(1)}.{m.group(2)}") * 1e3
    m = _RE_RES_K.search(t)
    if m:
        return float(m.group(1)) * {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}.get(m.group(2), 1.0)
    if not loose:
        return None

    # 33R / 120R / 0R
    m = _RE_RES_R_UNIT.search(t)
    if m:
        return float(m.group(1))
    # 4R7 / 2R2
    m = _RE_RES_R_DOT.search(t)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # 100k / 2.2k / 1M（没有 Ω 也没有“电阻”字样）
    m = _RE_RES_KM.search(t)
    if m:
        return float(m.group(1)) * {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}.get(m.group(2), 1.0)
    return None


_RE_CAP_MPN = re.compile(
    r"(?<!\d)(\d{2,3})\s*V\s*(\d{1,4})\s*M\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)"
)


def _cap_spec_from_mpn(mpn) -> dict:
    """从铝电解常见料号格式里读出规格：`PA100V100M10x12` → 100V / 100µF。

    只在「立创没有这颗料」时当第二证据用。写死这么窄，是因为放宽一点就会
    把别的命名当电容规格读（例如 IC 料号里恰好有数字）。
    """
    m = _RE_CAP_MPN.search(str(mpn or ""))
    if not m:
        return {}
    try:
        return {
            "voltage_v": float(m.group(1)),
            "capacitance_f": float(m.group(2)) * 1e-6,
        }
    except (TypeError, ValueError):
        return {}


def _parse_capacitance(t):
    m = _RE_CAP.search(t)
    if not m:
        return None
    val = float(m.group(1))
    # 归一化再查表：正则收了 P/N/U 大写，这里统一按小写取值
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}.get(
        m.group(2).lower(), 1.0
    )
    return val * mult


def _parse_inductance(t):
    vals = _parse_inductances(t)
    return vals[0] if vals else None


def _parse_inductances(t):
    """标题里出现的所有感值（系列链接会写 1MH/2.2MH/3MH/4.7MH）。

    只取第一个会误杀「1MH 2MH 3MH」这种多档链接：BOM 要 3mH，标题第一个
    1mH 对不上就整条淘汰，明明 SKU 里有 3MH。评分和页面闸门都要看全部档位。
    """
    out = []
    for m in _RE_IND.finditer(str(t or "")):
        try:
            val = float(m.group(1))
        except (TypeError, ValueError):
            continue
        mult = {"n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}.get(
            m.group(2).lower(), 1.0
        )
        out.append(val * mult)
    return out


def _parse_tolerance(t):
    m = _RE_TOL.search(t)
    return float(m.group(1)) if m else None


def _parse_power(t):
    m = _RE_POWER_FRAC.search(t)
    if m:
        try:
            return float(m.group(1)) / float(m.group(2))
        except ZeroDivisionError:
            return None
    m = _RE_POWER.search(t)
    return float(m.group(1)) if m else None


def _parse_powers(t):
    """文本里出现的所有功率档位（`1/4W`、`1W/2W`、`1/8W 1/4W`）。

    标题级判断必须看全部档位：多规格链接「1/8W/1/4W」里确实有 1/4W 这一档，
    只看第一个会把整条链接误杀；反过来「1/8W 只有这一档」才该拦。
    """
    s = str(t or "")
    out, spans = [], []
    for m in _RE_POWER_FRAC.finditer(s):
        try:
            out.append(float(m.group(1)) / float(m.group(2)))
        except ZeroDivisionError:
            pass
        spans.append((m.start(), m.end()))
    for m in _RE_POWER.finditer(s):
        # 分数功率已被上面的规则整体读过，`4W` 这一截不能重复算成 4W
        if any(a <= m.start() < b for a, b in spans):
            continue
        out.append(float(m.group(1)))
    return out


def _parse_voltage(t):
    m = _RE_VOLT.search(t)
    return float(m.group(1)) if m else None


def _parse_voltages(t):
    """标题里出现的所有耐压（多电压链接会写 10V/16V/25V/50V）。"""
    return [float(m.group(1)) for m in _RE_VOLT.finditer(str(t or ""))]


def _parse_current(t):
    m = _RE_CURR.search(t)
    if not m:
        return None
    val = float(m.group(1))
    # 500mA -> 0.5A
    return val / 1000 if m.group(2).lower().startswith("m") else val


def _parse_rds(t):
    m = _RE_RDS.search(t) or _RE_RDS_LOOSE.search(t)
    return float(m.group(1)) if m else None


def _parse_type(t):
    m = _RE_TYPE.search(t)
    return m.group(1).upper() if m else None


def _parse_pins(t):
    # 排针标题写「2*20P」= 双排各 20 针，总共 40。先认这种行列写法再认单个数字：
    # 直接抓 `(\d+)P` 会先撞上「20P」，把 40 针的排针当成 20 针，
    # 于是「2.54mm 双排排针 2*20P 40P」被判「少于要求 40P，剪不出这么多针」——
    # 明明是对的货，因为读错针数买不到。
    m = re.search(r"(?<![\d.])([1-9])\s*[*x×]\s*(\d{1,3})\s*[Pp]?(?![\d.])", str(t or ""))
    if m:
        _r, _per = int(m.group(1)), int(m.group(2))
        if 1 <= _r <= 4 and 1 <= _per <= 200:
            return _r * _per
    m = _RE_PINS.search(t)
    if m:
        return int(m.group(1))
    # Altium/KiCad 库名里的行列写法：JST_XH_B2B-XH-A_1x02_P2.50mm / PinHeader_2x20
    m = _RE_PIN_PAIRS.search(str(t or ""))
    if m:
        return int(m.group(2))
    return None


def _parse_rows(t):
    """连接器的排数（单排/双排）。针数对得上、排数不对，一样插不进去。

    BOM 写 40P 的排针，其实可能是 1×40 单排，也可能是 2×20 双排 ——
    两者针数都是 40，孔距也对，但一条 40 针的单排根本没法装到 2×20 的
    焊盘上（长度差一倍）。立创属性「排数=双排 / 插针结构=2x20P」和
    Altium 封装 `HDR-TH_40P-P2.54-V-M-R2-C20` 里的 R2 都能给出排数。
    """
    s = str(t or "")
    m = re.search(r"([单双两三])\s*排", s)
    if m:
        return {"单": 1, "双": 2, "两": 2, "三": 3}[m.group(1)]
    # 2x20 / 2*20P / 1×40：行列写法，前一段就是排数
    m = _RE_PIN_PAIRS.search(s)
    if m:
        return int(m.group(1))
    # Altium/KiCad 封装名：HDR-TH_40P-P2.54-V-M-R2-C20-S2.54 → R2 = 2 排
    m = re.search(r"(?<![A-Za-z0-9])R([1-4])-C\d+", s)
    if m:
        return int(m.group(1))
    return None


def _parse_pitch(t):
    for m in _RE_PITCH.finditer(t):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 0.3 <= v <= 10:
            return v
    return None


_KNOWN_PITCH = ("1.0", "1.25", "1.27", "1.5", "2.0", "2.5", "2.54", "3.96", "5.08")


def _parse_pitch_loose(t):
    """料号里夹着的间距：PHC2.54-6P / KF128-5.08-2P（没有 mm 单位）。"""
    hits = []
    for cand in _KNOWN_PITCH:
        m = re.search(r"(?<![\d.])" + re.escape(cand) + r"(?![\d])", t)
        if m:
            hits.append((m.start(), float(cand)))
    return min(hits)[1] if hits else None


def fmt_resistance(ohms):
    # 0Ω 是跨接/零欧电阻，卖家写「0R / 0欧 / 零欧」。落到下面的 mΩ 分支会输出
    # 「0mΩ」—— 那是毫欧级检流电阻（合金片、1W 以上、几块钱一颗），
    # 焊上去既不通路、体积也塞不进 0402 焊盘，是「数值看着对、器件全错」。
    if ohms is not None and abs(float(ohms)) < 1e-12:
        return "0Ω"
    if ohms >= 1e6:
        return f"{ohms / 1e6:g}MΩ"
    if ohms >= 1e3:
        return f"{ohms / 1e3:g}kΩ"
    if ohms >= 1:
        return f"{ohms:g}Ω"
    return f"{ohms * 1e3:g}mΩ"


def fmt_capacitance(f):
    if f >= 1e-3:
        return f"{f * 1e3:g}mF"
    if f >= 1e-6:
        return f"{f * 1e6:g}µF"
    if f >= 1e-9:
        return f"{f * 1e9:g}nF"
    return f"{f * 1e12:g}pF"


def fmt_inductance(h):
    if h >= 1e-3:
        return f"{h * 1e3:g}mH"
    if h >= 1e-6:
        return f"{h * 1e6:g}µH"
    return f"{h * 1e9:g}nH"


# 立创参数里哪些键值得并进选型参数。没列出的（芯片功耗、开关额定电压…）
# 只留在 `lcsc.attrs` 里给人看，不进 params —— 否则摘要行会被电参数顶掉。
_LCSC_PARAM_KEYS = {
    ("passive", "resistor"): {"resistance_ohm", "power_w", "tolerance_pct", "voltage_v"},
    ("passive", "capacitor"): {"capacitance_f", "voltage_v", "tolerance_pct"},
    ("passive", "inductor"): {"inductance_h", "tolerance_pct"},
    ("passive", "fuse"): {"current_a", "voltage_v"},
    ("passive", "diode"): set(),
    ("passive", "led"): set(),
    ("passive", "bead"): set(),
    ("connector", ""): {"pins", "pitch_mm", "rows"},
}


def _lcsc_param_keys(category: str, subtype: str) -> set:
    return _LCSC_PARAM_KEYS.get((category, subtype or "")) or set()


def _lcsc_by_code(info: dict) -> bool:
    """这条立创记录是不是「按编号查到」的（唯一标识，可以当权威）。

    老缓存的编号记录没有 matched_by 字段；型号反查的记录一定带 "model"，
    所以缺字段时按编号解析处理。
    """
    if not isinstance(info, dict):
        return False
    mb = str(info.get("matched_by") or "").strip()
    if mb:
        return mb == "code"
    return bool(str(info.get("code") or "").strip())


def _category_from_catalog(text: str) -> str:
    """立创的品类路径（Passives / Resistors…）→ 我们的品类。

    只在正则+位号都认不出来时兜底用；认不出返回空串，不硬塞。
    """
    t = str(text or "").lower()
    if not t:
        return ""
    if re.search(r"resistor|capacitor|inductor|ferrite|diode|led|fuse|passive", t):
        return "passive"
    if re.search(r"connector|terminal|header|socket", t):
        return "connector"
    if re.search(r"mosfet|transistor|igbt", t):
        return "power"
    if re.search(
        r"integrated circuit|\bic\b|microcontroller|logic|op amp|amplifier"
        # LDO/稳压器在立创是「Voltage Regulators - Linear…」。按 passive 处理会拿
        # 「必须有阻容感值」去卡它，一颗 LDO 被记成「缺少关键参数」判死。
        r"|voltage\s*regulator|\bldo\b|regulator|voltage\s*reference",
        t,
    ):
        return "ic"
    if re.search(r"module|bluetooth|wifi", t):
        return "module"
    return ""


def classify(text: str) -> str:
    t = (text or "").lower()
    # 封装写的是「测试点」：名称多半只是网络名（MCU STATUS / VBAT ADC），
    # 归到 other，别按芯片要求「必须有完整型号」把整行判死。
    if re.search(r"测试点|test\s*point|焊盘", t):
        return "other"
    if re.search(r"mosfet|mos\s*管|场效应|igbt|irf\d|stp\d|ao\d{4}|csd\d|bsc\d|功率管|功率mos", t):
        return "power"
    # 晶振也是被动件：名称「SMD 50MHz 晶振」、封装「OSC-SMD_4P」不带晶振二字时，
    # 型号里也常有 crystal/xtal（ABM8G、XTAL 网络名）。只敢认 crystal/xtal，
    # 不认裸 oscillator —— 芯片描述里「internal oscillator」太常见，会误判。
    if re.search(r"电阻|电容|电感|磁珠|二极管|三极管|led\b|resistor|capacitor|inductor|bead|diode|晶体管|晶振|crystal|xtal", t):
        return "passive"
    if re.search(
        r"连接器|排针|排母|端子|接插件|header|connector|conn[-_ ]|usb|type-c|fpc|idc|插座|牛角|kf\d{3}|xh\d?[\.\-]"
        r"|terminal\s*-?\s*block|mkds|pin_?socket",
        t,
        re.I,
    ):
        return "connector"
    if re.search(r"模组|模块|开发板|module|board|屏幕|lcd|oled", t):
        return "module"
    if re.search(
        r"mcu|单片机|stm32|esp32|esp8266|gd32|mspm?\d|drv\d{4}|tps\d{4}|ams1117|lm\d{3,4}|"
        r"芯片|\bic\b|运放|opamp|ldo|稳压|dcdc|flash|eeprom|传感器|sensor|74hc|逻辑|adc|dac|can\s*收发",
        t,
    ):
        return "ic"
    # 只写了数值的被动件（100nF / 1uF / 15pF / 15uH / 1kΩ）
    if _RE_CAP.search(t) or _RE_IND.search(t) or _RE_RES.search(t):
        return "passive"
    return "other"


_DIELECTRIC_RE = re.compile(
    r"(?<![A-Za-z])(C0G|COG|NP0|NPO)(?![A-Za-z])"
    r"|(X7R|X5R|X6S|X7S|X8R|Y5V|Y5U|Z5U|X7T)",
    re.I,
)


def parse_dielectric(text: str) -> str | None:
    """从 BOM 参数文本里认介质要求。NP0/COG/NPO 都归一成 C0G（Class I）。

    Class I（C0G/NP0）和 Class II（X7R/X5R/Y5V…）温漂差一个量级，
    振荡/定时/采样回路里不能互顶（P0-4）。
    """
    t = str(text or "").upper()
    m = _DIELECTRIC_RE.search(t)
    if not m:
        return None
    return "C0G" if (m.group(1) or "").upper() in ("C0G", "COG", "NP0", "NPO") else m.group(2).upper()


def parse_params(text: str, category: str) -> dict:
    t = (text or "").replace("％", "%").replace("，", ",")
    p = {}
    tol = _parse_tolerance(t)
    if tol is not None:
        p["tolerance_pct"] = tol
    # 介质（P0-4）：MLCC 的 C0G/NP0 与 X7R/X5R 温漂差一个量级，不能互顶。
    # NP0 与 C0G 是同一种材料的两种写法，归一成 C0G。
    d = parse_dielectric(t)
    if d:
        p["dielectric"] = d

    if category == "passive":
        c = _parse_capacitance(t)
        ind = _parse_inductance(t)
        # 只有当这段文本既不像电容也不像电感时，才用宽松规则猜电阻，
        # 否则 "10uF" 里的 10 会被误读成 10Ω。
        r = _parse_resistance(t, loose=(c is None and ind is None))
        if r is not None:
            p["resistance_ohm"] = r
        if c is not None:
            p["capacitance_f"] = c
        if ind is not None:
            p["inductance_h"] = ind
        pw = _parse_power(t)
        if pw is not None:
            p["power_w"] = pw
        v = _parse_voltage(t)
        if v is not None:
            p["voltage_v"] = v
    elif category == "power":
        ty = _parse_type(t)
        if ty:
            p["channel"] = ty
        v = _parse_voltage(t)
        if v is not None:
            p["vds_v"] = v
        cur = _parse_current(t)
        if cur is not None:
            p["id_a"] = cur
        rds = _parse_rds(t)
        if rds is not None:
            p["rds_mohm"] = rds
        pw = _parse_power(t)
        if pw is not None:
            p["power_w"] = pw
        v = _parse_voltage(t)
        if v is not None:
            p["voltage_v"] = v
    elif category == "connector":
        pins = _parse_pins(t)
        if pins is not None:
            p["pins"] = pins
        pitch = _parse_pitch(t)
        if pitch is None:
            pitch = _parse_pitch_loose(t)
        if pitch is not None:
            p["pitch_mm"] = pitch
        rows = _parse_rows(t)
        if rows is not None:
            p["rows"] = rows

    return p


def params_text(category: str, p: dict) -> str:
    parts = []
    if "resistance_ohm" in p:
        parts.append(fmt_resistance(p["resistance_ohm"]))
    if "capacitance_f" in p:
        parts.append(fmt_capacitance(p["capacitance_f"]))
    if "inductance_h" in p:
        parts.append(fmt_inductance(p["inductance_h"]))
    if "tolerance_pct" in p:
        parts.append(f"±{p['tolerance_pct']:g}%")
    if "channel" in p:
        parts.append(f"{p['channel']} 沟道")
    if "vds_v" in p:
        parts.append(f"Vds {p['vds_v']:g}V")
    if "id_a" in p:
        parts.append(f"Id {p['id_a']:g}A")
    if "rds_mohm" in p:
        parts.append(f"Rds(on) {p['rds_mohm']:g}mΩ")
    if "power_w" in p:
        parts.append(f"{p['power_w']:g}W")
    if "voltage_v" in p and "vds_v" not in p:
        parts.append(f"{p['voltage_v']:g}V")
    if "current_a" in p:
        parts.append(f"{p['current_a']:g}A")
    if "frequency" in p:
        # 晶振的摘要行就靠这个数：「SMD 50MHz」比裸型号一眼能核对
        parts.append(str(p["frequency"]))
    if "pins" in p:
        parts.append(f"{p['pins']}P")
    if "rows" in p:
        # 40P 可能是 1×40 也可能是 2×20，只写针数看不出来 —— 排数必须露在摘要里，
        # 否则人眼看「40P 间距 2.54mm」也分不清买的是不是同一个东西。
        parts.append({1: "单排", 2: "双排", 3: "三排"}.get(int(p["rows"] or 0), f"{int(p['rows'])} 排"))
    if "pitch_mm" in p:
        parts.append(f"间距 {p['pitch_mm']:g}mm")
    return " ".join(parts)


# 连接器系列：淘宝搜「XH2.54 4P」能出结果，搜「2.5mm 连接器」基本是废词
_CONN_SERIES = (
    (re.compile(r"jst[_\- ]?xh|_xh[_\- ]|\bb\d+b-xh|xh\s*[\-]?\s*2\.5", re.I), "XH"),
    (re.compile(r"jst[_\- ]?ph|\bph\s*[\-]?\s*2\.0|phc2\.0", re.I), "PH"),
    (re.compile(r"jst[_\- ]?zh|\bzh\s*1\.5|zh1\.5", re.I), "ZH"),
    (re.compile(r"jst[_\- ]?sh|\bsh\s*1\.0", re.I), "SH"),
    (re.compile(r"jst[_\- ]?eh|\beh\s*2\.5|eh2\.5", re.I), "EH"),
    (re.compile(r"jst[_\- ]?gh|\bgh\s*1\.25", re.I), "GH"),
    (re.compile(r"type\s*-?\s*c(?![a-z0-9])|usb\s*-?\s*c(?![a-z0-9])", re.I), "TYPE-C"),
    (re.compile(r"micro\s*-?\s*usb", re.I), "MICRO-USB"),
    (re.compile(r"mini\s*-?\s*usb", re.I), "MINI-USB"),
    (re.compile(r"usb\s*-?\s*a(?![a-z0-9])", re.I), "USB-A"),
    (re.compile(r"pinsocket|pin_socket|排母|母座|母头", re.I), "排母"),
    # 嘉立创的封装名写 HDR-TH_40P-P2.54…（HDR = header），淘宝卖排针的标题
    # 从来不写 HDR，得翻成「2.54mm 排针 40P」才搜得到。
    (re.compile(r"pinheader|pin_header|header_\d|\bhdr[\-_ ]?\d|\bhdr[_\-]|排针|针座", re.I), "排针"),
    (re.compile(r"terminalblock|terminal_block|mkds|kf128|端子台|接线端子|螺钉端子", re.I), "端子台"),
    (re.compile(r"\bidc\b|牛角|简牛|box\s*header", re.I), "牛角"),
    (re.compile(r"\bfpc\b|\bffc\b|软排线座", re.I), "FPC"),
)


def connector_series(*texts) -> str:
    """从封装/名称/型号里认出连接器系列（XH / PH / 排针 / 端子台 …）。"""
    blob = " ".join(str(t or "") for t in texts)
    for pat, name in _CONN_SERIES:
        if pat.search(blob):
            return name
    return ""
