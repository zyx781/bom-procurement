#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOM 选型规则引擎（纯离线）。

规则来源：taobao-bom-procurement/SKILL.md v0.3
  - §3 输入契约 / §4 搜索词 / §6 硬门槛 H01-H08 与品类权重 / §8 风险规则
  - §10 人工门 / §11 留痕

本模块不联网、不读取任何淘宝会话、不产生任何支付动作。
所有结论都是"规则结论"，不是实时商品报价。
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from pathlib import Path

ENGINE_VERSION = "0.1.0"

# 容差放宽上限：BOM 要求更严时也接受 ≤这个值的料（用户口径：耐压/数值/封装对就行）
TOLERANCE_RELAX_PCT = 20.0

# --------------------------------------------------------------------------
# 表头识别
# --------------------------------------------------------------------------

HEADER_ALIASES = {
    "line_no": ["行号", "序号", "编号", "项次", "line", "line_no", "line no", "no", "no.", "#", "item", "item no", "item_no"],
    "mpn": [
        "型号", "料号", "物料编码", "器件型号", "规格型号", "mpn", "part", "part_number", "part number", "pn",
        "manufacturer part", "manufacturer_part", "manufacturerpart", "mfr part", "mfrpart", "mfg part", "mpn.",
        "partno", "part no", "料号/型号",
    ],
    "desc": ["名称", "描述", "品名", "品类", "器件名称", "元件名称", "器件", "元件", "description", "desc", "name", "comment", "comment.", "标识"],
    "package": ["封装", "封装形式", "封装/规格", "footprint", "package", "case", "case code"],
    # Altium 导出的 BOM 常把值单独放一列（表头就一个字“值”），必须认
    "params": ["参数", "规格", "关键参数", "规格参数", "参数/规格", "值", "数值", "规格值", "参数值", "params", "spec", "specification", "value"],
    "qty": ["数量", "用量", "采购数", "数目", "qty", "quantity", "pcs"],
    "refs": ["位号", "参考位号", "位号/参考", "refs", "reference", "designator", "ref"],
    "critical": ["关键件", "关键", "critical", "criticality"],
    "brand": ["品牌", "厂商", "制造商", "brand", "mfr", "manufacturer"],
    "substitute": ["替代", "替代策略", "可替代", "substitute", "substitution"],
    "note": ["备注", "说明", "note", "remark"],
}

_CANON_INDEX = {}
for _canon, _aliases in HEADER_ALIASES.items():
    for _a in _aliases:
        _CANON_INDEX[re.sub(r"[\s_\-/\.\(\)（）]+", "", _a.lower())] = _canon


def _canon_header(name):
    return _CANON_INDEX.get(re.sub(r"[\s_\-/\.\(\)（）]+", "", str(name or "").strip().lower()))


def _cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# --------------------------------------------------------------------------
# 文件解析
# --------------------------------------------------------------------------


def parse_workbook(filename: str, raw: bytes) -> dict:
    """把 CSV / XLSX 解析成统一行结构。返回 {rows, columns, warnings, filename}。"""
    suffix = Path(filename or "").suffix.lower()
    warnings = []

    if suffix in (".xlsx", ".xlsm"):
        headers, raw_rows, meta = _read_xlsx(raw)
        used = meta.get("sheets_used") or []
        skipped = meta.get("sheets_skipped") or []
        if len(used) > 1:
            warnings.append(f"已合并 {len(used)} 个工作表：{'、'.join(used)}")
        if skipped:
            warnings.append(
                f"以下工作表表头与本表不一致，未合并：{'、'.join(skipped)}（请确认是否漏行）"
            )
    elif suffix == ".xls":
        raise ValueError("旧版 .xls 不支持，请在 Excel 里另存为 .xlsx 或 .csv")
    else:
        headers, raw_rows = _read_csv(raw)

    if not headers:
        raise ValueError("表头为空，无法解析")

    canon = {}
    unknown = []
    for i, h in enumerate(headers):
        key = _canon_header(h)
        if key and key not in canon:
            canon[key] = i
        elif not key and _cell_text(h):
            unknown.append(_cell_text(h))

    if not ({"mpn", "desc", "params"} & set(canon)):
        raise ValueError("未识别到型号/名称/参数列。表头至少需要包含：型号、名称或参数，另建议有数量、封装。")

    if "qty" not in canon:
        warnings.append("没有识别到数量列，所有行都会标记为待确认数量。")

    rows = []
    for idx, r in enumerate(raw_rows):
        if not any(_cell_text(x) for x in r):
            continue
        get = lambda key: (_cell_text(r[canon[key]]) if key in canon and canon[key] < len(r) else "")
        line_no = _to_int(get("line_no"), idx + 1)
        qty = _to_int(get("qty"), None)
        rows.append(
            {
                "line_no": line_no,
                "mpn": get("mpn"),
                "desc": get("desc"),
                "package": get("package"),
                "params": get("params"),
                "qty": qty,
                "refs": get("refs"),
                "critical": get("critical"),
                "brand": get("brand"),
                "substitute": get("substitute"),
                "note": get("note"),
                "raw": {_cell_text(h): _cell_text(r[i]) for i, h in enumerate(headers) if i < len(r) and _cell_text(h)},
            }
        )

    if not rows:
        raise ValueError("文件里没有数据行")

    return {
        "filename": filename,
        "columns": {k: _cell_text(headers[i]) for k, i in canon.items()},
        "unknown_headers": unknown,
        "warnings": warnings,
        "rows": rows,
    }


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_csv(raw: bytes):
    text = _decode(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    table = [row for row in reader if any(_cell_text(c) for c in row)]
    if not table:
        return [], []
    # 表头可能不在第一行（前面有标题/说明行），在前 10 行里找命中别名最多的一行
    best_i, best_hits = 0, -1
    for i, row in enumerate(table[:10]):
        hits = sum(1 for c in row if _canon_header(c))
        if hits > best_hits:
            best_i, best_hits = i, hits
    if best_hits < 1:
        best_i = 0
    return table[best_i], table[best_i + 1 :]


def _to_int(text, default=None):
    """从 "100" / "100pcs" / "1,000" 里取整数。"""
    if text is None:
        return default
    m = re.search(r"-?\d+", str(text).replace(",", ""))
    return int(m.group(0)) if m else default


def _xlsx_sheet_table(ws) -> list[list[str]]:
    """一个工作表 → 二维字符串表（跳过空行）。"""
    table = []
    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        cells = [_cell_text(c) for c in row]
        if any(cells):
            table.append(cells)
    return table


def _xlsx_split_header(table: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """在一个工作表里找出表头行（别名命中最多的一行），返回 (表头, 数据行)。"""
    if not table:
        return [], []
    best_i, best_hits = 0, -1
    for i, row in enumerate(table[:10]):
        hits = sum(1 for c in row if _canon_header(c))
        if hits > best_hits:
            best_i, best_hits = i, hits
    if best_hits < 1:
        best_i = 0
    return table[best_i], table[best_i + 1 :]


def _xlsx_header_matches(base: list[str], other: list[str]) -> bool:
    """两个工作表算不算同一张 BOM：规范化后的表头列集合有交集即可。"""
    a = {k for k in (_canon_header(c) for c in base) if k}
    b = {k for k in (_canon_header(c) for c in other) if k}
    if not a or not b:
        return False
    return len(a & b) >= max(1, min(len(a), len(b)) // 2)


def _read_xlsx(raw: bytes) -> tuple[list[str], list[list[str]], dict]:
    """读 XLSX / XLSM 的**所有工作表**并合并。

    2026-11-11：以前只读 `wb.sheetnames[0]`，KiCad/AD 导出的多页 BOM
    （主板 / 电源板 / 连接器）第 2 页起的行会**静默消失** —— 不报错最危险。
    现在逐页解析、表头能对上的合并，对不上的跳过并在 meta 里记账。
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    merged_headers: list[str] = []
    merged_rows: list[list[str]] = []
    used, skipped = [], []
    try:
        for name in wb.sheetnames:
            ws = wb[name]
            try:
                table = _xlsx_sheet_table(ws)
            except Exception:  # noqa: BLE001
                skipped.append(name)
                continue
            headers, rows = _xlsx_split_header(table)
            if not headers or not rows:
                continue  # 空表 / 只有表头的说明页
            if not merged_headers:
                merged_headers, merged_rows = list(headers), list(rows)
                used.append(name)
            elif _xlsx_header_matches(merged_headers, headers):
                merged_rows.extend(rows)
                used.append(name)
            else:
                skipped.append(name)
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return merged_headers, merged_rows, {"sheets_used": used, "sheets_skipped": skipped}


# --------------------------------------------------------------------------
# 规范化 / 参数识别
# --------------------------------------------------------------------------

UNITS = "Ω欧姆欧"

_RE_RES = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])?\s*(?:Ω|欧姆|欧|ohms?|ohm)", re.I)
_RE_RES_4K7 = re.compile(r"(\d+)[kK](\d)\b")
_RE_RES_K = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])\s*(?:电阻|resistor)")
_RE_CAP = re.compile(r"(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m)\s*[fF]\b")
_RE_IND = re.compile(r"(\d+(?:\.\d+)?)\s*(u|µ|μ|n|m)\s*[hH]\b")
_RE_TOL = re.compile(r"(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*%")
_RE_POWER_FRAC = re.compile(r"(\d+)\s*/\s*(\d+)\s*[wW]\b")
_RE_POWER = re.compile(r"(\d+(?:\.\d+)?)\s*[wW]\b")
_RE_VOLT = re.compile(r"(\d+(?:\.\d+)?)\s*[vV](?:dc|DC)?\b")
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


def _parse_capacitance(t):
    m = _RE_CAP.search(t)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}.get(m.group(2), 1.0)
    return val * mult


def _parse_inductance(t):
    m = _RE_IND.search(t)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}.get(m.group(2), 1.0)
    return val * mult


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
    m = _RE_PINS.search(t)
    if m:
        return int(m.group(1))
    # Altium/KiCad 库名里的行列写法：JST_XH_B2B-XH-A_1x02_P2.50mm / PinHeader_2x20
    m = _RE_PIN_PAIRS.search(str(t or ""))
    if m:
        return int(m.group(2))
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


def classify(text: str) -> str:
    t = (text or "").lower()
    # 封装写的是「测试点」：名称多半只是网络名（MCU STATUS / VBAT ADC），
    # 归到 other，别按芯片要求「必须有完整型号」把整行判死。
    if re.search(r"测试点|test\s*point|焊盘", t):
        return "other"
    if re.search(r"mosfet|mos\s*管|场效应|igbt|irf\d|stp\d|ao\d{4}|csd\d|bsc\d|功率管|功率mos", t):
        return "power"
    if re.search(r"电阻|电容|电感|磁珠|二极管|三极管|led\b|resistor|capacitor|inductor|bead|diode|晶体管", t):
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
    if "pins" in p:
        parts.append(f"{p['pins']}P")
    if "pitch_mm" in p:
        parts.append(f"间距 {p['pitch_mm']:g}mm")
    return " ".join(parts)


# --------------------------------------------------------------------------
# 单行分析
# --------------------------------------------------------------------------

RISK_WORDS = ["散新", "拆机", "翻新", "打磨", "改字", "白字", "原字", "洗板", "二手", "返修", "旧件", "拆板"]

CATEGORY_LABEL = {"passive": "被动件", "ic": "芯片", "power": "功率器件", "connector": "连接器", "module": "模组", "other": "其他"}

TYPICAL_POWER = {"0603": 0.1, "0805": 0.125, "1206": 0.25, "1210": 0.5, "2512": 1.0}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "是", "关键", "关键件", "√")


# 位号前缀 → 品类（BOM 只给位号/料号时用来兜底，Altium 导出的表很常见）
_REF_KIND = {
    "LED": "passive",
    "CN": "connector",
    "CON": "connector",
    "J": "connector",
    "P": "connector",
    "XH": "connector",
    "C": "passive",
    "R": "passive",
    "L": "passive",
    "FB": "passive",
    "D": "passive",
    "ZD": "passive",
    "F": "passive",
    "U": "ic",
    "IC": "ic",
    "Q": "power",
    "VT": "power",
    "SW": "other",
    "K": "other",
    "X": "other",
    "Y": "other",
}

# 这些封装基本只出现在功率管上（TDSON/TO-220/DPAK…）
_RE_POWER_PKG = re.compile(r"TDSON|TO-?220|TO-?252|TO-?263|LFPAK|DPAK|POWERPAK", re.I)

# 看起来像“纯数值”的 Comment：40MHz / 100nF / 1kΩ
_RE_VALUE_ONLY = re.compile(r"^[\d\.]+\s*[a-zA-ZΩµμ%]{0,3}$")


def _designator_prefix(refs) -> str:
    m = re.match(r"\s*([A-Za-z]+)", str(refs or ""))
    return m.group(1).upper() if m else ""


def _category_from_refs(refs) -> str:
    pref = _designator_prefix(refs)
    if not pref:
        return ""
    if pref in _REF_KIND:
        return _REF_KIND[pref]
    for key in sorted(_REF_KIND, key=len, reverse=True):
        if pref.startswith(key):
            return _REF_KIND[key]
    return ""


def _passive_subtype(refs, text, category) -> str:
    """被动件细分：resistor / capacitor / inductor / bead / diode / led。

    决定“必须有阻容感值”还是“必须有型号”，避免把二极管当成缺参数的电阻。
    """
    if category != "passive":
        return ""
    pref = _designator_prefix(refs)
    t = (text or "").lower()
    if pref == "LED" or re.search(r"led\b|发光二极管|light\s*emitting", t):
        return "led"
    if pref in ("D", "ZD") or re.search(r"二极管|diode|整流|schottky|肖特基|1n\d{3,}", t):
        return "diode"
    if pref in ("F", "FU") or re.search(r"保险丝|fuse|自恢复", t):
        return "fuse"
    if pref == "FB" or "磁珠" in t or re.search(r"\bbead\b", t):
        return "bead"
    if pref == "C" or re.search(r"电容|capacitor|[pnuµm]f\b", t):
        return "capacitor"
    if pref == "L" or re.search(r"电感|inductor|[pnuµm]h\b", t):
        return "inductor"
    if pref == "R" or re.search(r"电阻|resistor|Ω|ohm", t):
        return "resistor"
    return ""


def _footprint_model(footprint) -> str:
    """从 Altium 封装名里抠出料号：OSC-SMD_4P-L3.2-W2.5-BL_SIT8008BI → SIT8008BI。"""
    fp = str(footprint or "").strip()
    if "_" not in fp:
        return ""
    token = fp.rsplit("_", 1)[-1].strip()
    if len(token) >= 5 and re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
        return token
    return ""


# Comment 列里"料号 / 用途"的常见分隔符：TXU0104PWR / ENCODER
_DESC_SPLIT = re.compile(r"\s*[/|｜、;；]\s*|\s*,\s*")


def _pn_like(token) -> bool:
    t = str(token or "").strip(" ,;:()[]【】")
    if not t or " " in t:
        return False
    if re.fullmatch(r"\d{6,}", t):  # 0451008 这类纯数字料号
        return True
    if _RE_VALUE_ONLY.match(t):
        return False
    if not re.search(r"[A-Za-z]", t) or not re.search(r"\d", t):
        return False
    return 4 <= len(t) <= 40


def mpn_from_desc(desc) -> str:
    """'TXU0104PWR / ENCODER' → TXU0104PWR；'8A fast / 0451008' → 0451008。"""
    for part in _DESC_SPLIT.split(str(desc or "")):
        if _pn_like(part):
            return part.strip()
    return ""


def analyze_line(row: dict, fallback_no: int, options: dict | None = None) -> dict:
    row = row or {}
    combined = " ".join(
        str(row.get(k) or "") for k in ("mpn", "desc", "params", "package", "note")
    )
    refs = str(row.get("refs") or "").strip()
    footprint = str(row.get("package") or "").strip()

    category = classify(combined)
    if category == "other":
        # 兜底 1：位号前缀（R1/C12/U3/CN5…）
        from_refs = _category_from_refs(refs)
        if from_refs:
            category = from_refs
        else:
            # 兜底 2：封装形态（CONN-TH / TDSON-8 …）
            if re.search(r"\bCONN|连接", combined, re.I):
                category = "connector"
    if category == "ic" and _RE_POWER_PKG.search(footprint):
        category = "power"

    package = (
        parse_package(footprint)
        or parse_package(row.get("mpn") or "")
        or parse_package(row.get("desc") or "")
        or parse_package(row.get("params") or "")
    )
    desc = str(row.get("desc") or "").strip()
    mpn = str(row.get("mpn") or "").strip()

    # Comment 列常常放的就是料号（Altium 默认模板）：没有 Manufacturer Part 时用它顶上
    if (
        not mpn
        and desc
        and " " not in desc
        and 4 <= len(desc) <= 40
        and re.search(r"[A-Za-z]", desc)
        and not _RE_VALUE_ONLY.match(desc)
    ):
        mpn = desc
    if not mpn and category == "other":
        fp_model = _footprint_model(footprint)
        if fp_model:
            mpn = fp_model
    # 名称列把“料号 / 用途”写在一起时（TXU0104PWR / ENCODER），把料号抠出来，
    # 否则芯片行没有型号 → 一条搜索词都生成不出来。
    # 连接器例外：它的名称是用途/信号说明（GPIO: GND,VTREF,OUT0），抠出来只会得到
    # OUT0 这种假料号，把搜索词带偏。
    if not mpn and desc and category != "connector":
        mpn = mpn_from_desc(desc)
    if category == "connector":
        # 真料号藏在 Altium 封装名里：JST_XH_B2B-XH-A → B2B-XH-A。
        # 若 mpn 是信号清单（JY901S:5V,GND,TX,RX）或过长，同样视为无效。
        if not mpn or re.search(r"[:,，、]", mpn) or len(mpn) > 24:
            mpn = connector_part(footprint, package) or ""
        elif not connector_part(mpn):
            mpn = connector_part(footprint, package) or mpn
    # "AO3400A MODE open drain"：型号和用途只用空格分隔，取第一个像料号的词
    if not mpn and category in ("ic", "power", "module") and desc:
        first = desc.split()[0]
        if _pn_like(first):
            mpn = first.strip()

    params = parse_params(" ".join([combined, mpn]), category)
    subtype = _passive_subtype(refs, combined + " " + desc, category)
    # 保险丝的额定电流就是它的规格。不给它一个数值参数，SKU 选择就只能「保留页面默认」，
    # 结果 8A 的位子买成了 0.25A —— 比买贵更糟，是买错。
    if subtype == "fuse" and "current_a" not in params:
        _amp = _parse_current(" ".join([str(combined or ""), str(desc or ""), str(mpn or "")]))
        if _amp:
            params["current_a"] = _amp
    qty = row.get("qty")
    try:
        qty = int(qty) if qty is not None and str(qty).strip() != "" else None
    except (TypeError, ValueError):
        qty = None
    critical = _truthy(row.get("critical"))

    missing, blocking = [], []
    if package == "":
        missing.append("封装")
    if category == "passive":
        if subtype in ("diode", "led", "bead", "fuse"):
            if not mpn:
                missing.append("完整型号")
                blocking.append("二极管/LED/磁珠必须有完整型号，封装不能代替型号")
        elif not any(k in params for k in ("resistance_ohm", "capacitance_f", "inductance_h")):
            missing.append("阻值/容值/感值")
            blocking.append("缺少关键参数，无法锁定规格")
    if category in ("ic", "power") and not mpn:
        missing.append("完整型号")
        blocking.append("芯片/功率器件必须有完整型号与后缀")
    if qty is None:
        missing.append("数量")
        blocking.append("数量缺失或不是正整数")
    if not mpn and not desc and not params:
        blocking.append("整行没有可用信息")
    if category == "other" and not mpn and not desc:
        blocking.append("无法判断品类，请补充型号或名称")

    status = "needs_user" if blocking else "ready"

    gates = _build_gates(category, package, params, mpn, subtype)
    queries = _build_queries(category, package, params, mpn, desc, row.get("brand"), subtype)
    risks = _build_risks(category, package, params, mpn, gates)

    badge = {
        "line_no": row.get("line_no") or fallback_no,
        "row": row,
        "mpn": mpn,
        "desc": desc,
        "brand": str(row.get("brand") or "").strip(),
        "package": package,
        "category": category,
        "category_label": CATEGORY_LABEL.get(category, "其他"),
        "subtype": subtype,
        "params": params,
        "params_text": params_text(category, params) or desc or mpn,
        "qty": qty,
        "refs": str(row.get("refs") or "").strip(),
        "criticality": "critical" if critical else "normal",
        "substitution_allowed": _truthy(row.get("substitute")),
        "status": status,
        "missing": missing,
        "blocking": blocking,
        "hard_gates": gates,
        "query_plan": queries,
        "risks": risks,
        "decision": _build_decision(category, package, params, mpn, desc, qty, critical, gates, queries, risks, subtype),
        "source": "规则引擎 v" + ENGINE_VERSION,
    }
    return badge


def _build_gates(category, package, p, mpn, subtype=""):
    g = []
    if mpn and category in ("ic", "power"):
        g.append(f"型号 = {mpn}（含后缀，不接受同系列替代）")
    if mpn and subtype in ("diode", "led", "bead", "fuse"):
        g.append(f"型号 = {mpn}（同系列可替代需人工确认）")
    if package:
        g.append(f"封装 = {package}")
    if category == "passive":
        if "resistance_ohm" in p:
            g.append(f"阻值 = {fmt_resistance(p['resistance_ohm'])}")
        if "capacitance_f" in p:
            g.append(f"容值 = {fmt_capacitance(p['capacitance_f'])}")
        if "inductance_h" in p:
            g.append(f"感值 = {fmt_inductance(p['inductance_h'])}")
        if "tolerance_pct" in p:
            # 容差不作硬门槛：BOM 写 ±1% 也不拦 ±5%/±10%/±20% 的料
            g.append(f"精度 不限（BOM ±{p['tolerance_pct']:g}%，按 ≤±20% 放宽）")
        if "power_w" in p:
            g.append(f"功率 ≥ {p['power_w']:g}W")
        if "voltage_v" in p:
            g.append(f"耐压 ≥ {p['voltage_v']:g}V")
        if "current_a" in p:
            # 保险丝的电流必须是同一个值：8A 的位子不能用 0.25A 顶
            g.append(f"额定电流 = {p['current_a']:g}A（不可用其他档位替代）")
    elif category == "power":
        if "channel" in p:
            g.append(f"{p['channel']} 沟道")
        if "vds_v" in p:
            g.append(f"Vds ≥ {p['vds_v']:g}V")
        if "id_a" in p:
            g.append(f"Id ≥ {p['id_a']:g}A")
        if "rds_mohm" in p:
            g.append(f"Rds(on) ≤ {p['rds_mohm']:g}mΩ")
    elif category == "connector":
        series = connector_series(package, mpn)
        if "pins" in p:
            g.append(f"针数 = {p['pins']}P")
        if "pitch_mm" in p:
            if pitch_match(p["pitch_mm"], 2.54):
                # JST XH 真实 2.50mm，淘宝通用写法 XH2.54 —— 让人知道两者是一回事
                g.append(f"间距 = {p['pitch_mm']:g}mm（XH 系列 2.5/2.54 同档）")
            else:
                g.append(f"间距 = {p['pitch_mm']:g}mm（1.27 与 1.25 不通用）")
        if series in ("排针", "排母"):
            g.append("整条可自行剪断，针数多于需求也能用")
    if category in ("ic", "power"):
        g.append("原装/全新，非翻新、散新、拆机、打磨")
        g.append("丝印清晰、有实拍图或可核验的正品证据")
    g.append("数量按 BOM 用量，不静默放大")
    return g


# 连接器间距的「同一档」：JST XH 实际 2.50mm，淘宝普遍写 XH2.54 —— 这两个必须视为通用，
# 否则 BOM 写 XH2.50mm 会把所有 XH2.54 的 SKU 全判成间距不符。
# 1.27 与 1.25 仍是不同料（H01 明确要求区分），所以不做通用容差，只认下面这组等价。
PITCH_SAME_PAIRS = ((2.5, 2.54),)


def pitch_match(a, b) -> bool:
    """间距是否同一档（2.50 与 2.54 视为通用）。"""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if abs(fa - fb) <= 1e-9:
        return True
    lo, hi = min(fa, fb), max(fa, fb)
    return any(abs(lo - x) <= 0.01 and abs(hi - y) <= 0.01 for x, y in PITCH_SAME_PAIRS)


# 连接器系列：淘宝搜「XH2.54 4P」能出结果，搜「2.5mm 连接器」基本是废词
_CONN_SERIES = (
    (re.compile(r"jst[_\- ]?xh|_xh[_\- ]|\bb\d+b-xh|xh\s*[\-]?\s*2\.5", re.I), "XH"),
    (re.compile(r"jst[_\- ]?ph|\bph\s*[\-]?\s*2\.0|phc2\.0", re.I), "PH"),
    (re.compile(r"jst[_\- ]?zh|\bzh\s*1\.5|zh1\.5", re.I), "ZH"),
    (re.compile(r"jst[_\- ]?sh|\bsh\s*1\.0", re.I), "SH"),
    (re.compile(r"jst[_\- ]?eh|\beh\s*2\.5|eh2\.5", re.I), "EH"),
    (re.compile(r"jst[_\- ]?gh|\bgh\s*1\.25", re.I), "GH"),
    (re.compile(r"pinsocket|pin_socket|排母|母座|母头", re.I), "排母"),
    (re.compile(r"pinheader|pin_header|header_\d|排针|针座", re.I), "排针"),
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


# Altium 库里藏着的真料号：MainParts:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical → B2B-XH-A
_CONN_PN_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])([BPS]\d+B-[A-Z]{2}(?:-[A-Z]{1,3})?)(?![A-Za-z0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9])((?:PH|ZH|SH|EH|GH|XH)\d?\.?\d*-\d*[A-Z]{1,2}[A-Z]?)(?![A-Za-z0-9])", re.I),
    re.compile(r"(MKDS[- ,\d.]{3,18}\d)", re.I),
)


def connector_part(*texts) -> str:
    """从封装名/名称里抠连接器料号（只认真正的系列料号，不认信号名）。"""
    for t in texts:
        s = str(t or "")
        for pat in _CONN_PN_PATTERNS:
            m = pat.search(s)
            if m:
                cand = m.group(1).upper().strip(" ,")
                if re.search(r"\d", cand) and re.search(r"[A-Z]", cand) and 5 <= len(cand) <= 24:
                    return cand
    return ""


def _parse_freq_label(text):
    """'8MHz ABM3' -> '8MHz'（晶振只认频率，型号可有可无）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*MHz", str(text or ""), re.I)
    return f"{m.group(1)}MHz" if m else ""


def _parse_amp_label(text):
    """'8A / branch fault backup' -> '8A'（保险丝按额定电流选，只要显示用的字符串）。"""
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[Aa](?![\w])", str(text or ""))
    return f"{m.group(1)}A" if m else ""


def title_amps(text) -> list:
    """标题/SKU 里出现的所有额定电流数值（A）。

    保险丝链接常把整档写成「1/1.5/2/3/8/25A」，只匹配最后一个数会漏掉 8A，
    于是把一条本来有 8A 的链接判成「不符」—— 必须把斜杠档位整组展开。
    """
    out = []
    for m in re.finditer(r"((?:\d+(?:\.\d+)?\s*/\s*)*\d+(?:\.\d+)?)\s*[Aa](?![\w])", str(text or "")):
        for part in m.group(1).split("/"):
            part = part.strip()
            if not part:
                continue
            try:
                val = float(part)
            except ValueError:
                continue
            if 0 < val <= 100:
                out.append(val)
    return out


def _build_queries(category, package, p, mpn, desc, brand, subtype=""):
    brand = str(brand or "").strip()
    q = []
    pkg = package.split(" ")[0] if package else ""
    metric = ""
    if package and "(" in package:
        metric = package.split("(")[1].split(")")[0]

    # 有些封装名本身就是品类（测试点 / 焊盘），BOM 名称只是网络名（MCU STATUS、VBAT ADC），
    # 拿网名去搜淘宝必然空结果 —— 直接用封装名当搜索词。
    if re.search(r"测试点|焊盘|test\s*point", f"{package or ''} {desc or ''}", re.I):
        # 直径常写在名称里（「D1.5mm 测试点」），封装位却可能是空的；
        # 只搜「测试点」会召回到双排母、接线端子这类完全无关的东西。
        dia = ""
        m = re.search(r"(?:D|Φ|φ|直径)\s*(\d+(?:\.\d+)?)\s*mm", f"{desc or ''} {package or ''}", re.I)
        if m:
            dia = f"{m.group(1)}mm"
        kw = re.sub(r"\s+", " ", str(package or "")).strip()
        if dia:
            return [f"{dia} 测试点 焊盘", f"{dia} 测试点", f"PCB 测试点 {dia}"]
        if kw:
            return [kw, f"{kw} 焊盘", f"PCB {kw} 测试点"]
        return ["PCB 测试点 焊盘", "贴片测试点", "测试点 焊盘"]

    if category == "passive":
        if "resistance_ohm" in p:
            val = fmt_resistance(p["resistance_ohm"])
        elif "capacitance_f" in p:
            val = fmt_capacitance(p["capacitance_f"])
        elif "inductance_h" in p:
            val = fmt_inductance(p["inductance_h"])
        else:
            val = ""
        # 精度已不作为门槛，写进搜索词只会把结果收窄
        tol = ""
        kind = "电阻" if "resistance_ohm" in p else ("电容" if "capacitance_f" in p else ("电感" if "inductance_h" in p else "元器件"))
        if subtype in ("diode", "led", "bead", "fuse") and mpn:
            # 二极管/LED/磁珠靠型号定位，不靠“数值”
            head = f"{mpn} {pkg}".strip()
            q.append(head)
            q.append(f"{mpn} {pkg} 全新".strip())
            if subtype == "led":
                q.append(f"{mpn} LED")
            else:
                q.append(f"{mpn} 贴片")
        if pkg and val:
            q.append(f"{pkg} {val}{tol} {kind}".strip())
            if pkg == "插件":
                q.append(f"{val} 直插{kind}".strip())
            else:
                q.append(f"{pkg} {val} 贴片{kind}".strip())
            if metric:
                q.append(f"{metric} {val}{tol} {kind}".strip())
            # 电阻卖家另一套写法是「120R」：100Ω 以下补一条，Ω 搜不到时还有得试
            if "resistance_ohm" in p and 0 < p["resistance_ohm"] < 1000:
                q.append(f"{pkg} {p['resistance_ohm']:g}R".strip())
        if brand and val:
            q.append(f"{brand} {val} {kind}".strip())
        # 保险丝常常只有额定电流、没有料号（「8A / branch fault backup」+ 封装 SMD 保险丝），
        # 以前会拿整句英文去搜，必然零结果 —— 按电流 + 「保险丝」搜。
        if not val and subtype == "fuse":
            amp = _parse_amp_label(desc)
            if amp:
                q.append(f"{amp} 贴片保险丝")
                if pkg and pkg.upper() != "SMD":
                    q.append(f"{pkg} {amp} 保险丝")
                q.append(f"{amp} 保险丝 快断")
        if not val and not q:
            if mpn:
                q.append(f"{mpn} {pkg}".strip())
            elif desc:
                q.append(desc)
    elif category in ("ic", "power", "module"):
        if mpn:
            # 先裸型号：带封装的组合词经常把结果收窄到 0（DPAK / SMD / 模块 都不是淘宝常用词），
            # 裸料号反而能命中，命中后再靠型号门槛筛。
            q.append(mpn)
            q.append(f"{mpn} {pkg}".strip())
            q.append(f"{mpn} 原装 {pkg}".strip() if pkg else f"{mpn} 原装")
            q.append(f"{mpn} 全新")
    elif category == "connector":
        # 连接器的搜索词必须带「系列 + 针数」：淘宝搜「2.5mm 连接器」基本是废词，
        # 搜「XH2.54 4P」「2.54mm 排针 2P」才有对的结果。
        pins = p.get("pins")
        pitch = p.get("pitch_mm")
        pin_txt = f"{int(pins)}P" if pins else ""
        series = connector_series(desc, package, mpn, subtype)
        part = connector_part(package, desc, mpn)

        if series == "XH":
            # JST XH 淘宝通用写法是 XH2.54（真实间距 2.50），两种写法都要搜
            if part:
                q.append(part)
            q.append(f"XH2.54 {pin_txt} 连接器".strip())
            q.append(f"XH2.5 {pin_txt} 连接器".strip())
            if pins and not part:
                q.append(f"B{int(pins)}B-XH-A")
        elif series in ("排针", "排母"):
            ptxt = f"{pitch:g}mm" if pitch else "2.54mm"
            q.append(f"{ptxt} {series} {pin_txt}".strip())
            q.append(f"单排{series} {ptxt} {pin_txt}".strip())
        elif series == "端子台":
            ptxt = f"{pitch:g}mm" if pitch else ""
            q.append(" ".join(b for b in (ptxt, "端子台", pin_txt) if b))
            if pins:
                pn_pitch = f"{pitch:g}" if pitch else "5.08"
                q.append(f"KF128-{pn_pitch}-{int(pins)}P")
        elif series:
            ptxt = f"{pitch:g}mm" if pitch else ""
            q.append(" ".join(b for b in (series, ptxt, pin_txt, "连接器") if b))
            if part:
                q.insert(0, part)
        else:
            if part:
                q.append(part)
            if pins and pitch:
                q.append(f"{pitch:g}mm {pin_txt} 连接器")
            if pkg:
                q.append(f"{pkg} 连接器")
        # 名称是纯用途说明（TRACK POWER OUTPUT / VENC SELECT）时不该进搜索词；
        # 带冒号/逗号的是信号清单（GPIO: GND,VTREF,OUT0），也不是料号
        if desc and _pn_like(desc) and not re.search(r"[:,，、]", str(desc)):
            q.append(desc)
    else:
        # 晶振：BOM 里常写「8MHz ABM3」，直接搜型号可能没有货，
        # 补一条「8MHz 贴片晶振」这种通用词更容易出结果。
        freq = _parse_freq_label(desc)
        if freq and "晶振" in str(package or ""):
            q.append(f"{freq} 贴片晶振")
            q.append(f"{freq} 晶振 {pkg}".strip())
        if mpn:
            q.append(f"{mpn} {pkg}".strip())
        elif desc:
            q.append(desc)

    # 兜底：型号和参数都没解析出来时，至少拿名称的前半段去搜，
    # 别让整行 query_plan 为空（页面上会变成“没有可用搜索词”，等于无法执行）。
    if not q and desc:
        head = _DESC_SPLIT.split(str(desc))[0].strip()
        head = re.sub(r"(?i)\b(dnp|tune|transient only|fast|backup)\b", " ", head)
        head = re.sub(r"[（(]\s*[)）]", " ", head)
        head = re.sub(r"\s+", " ", head).strip(" ,;")
        if head:
            q.append(head)

    out, seen = [], set()
    for item in q:
        item = item.replace("µ", "u").replace("　", " ")
        item = re.sub(r"\s+", " ", item).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out[:4]


def _build_risks(category, package, p, mpn, gates):
    risks = []
    base = package.split(" ")[0] if package else ""
    if category == "passive":
        if "power_w" in p and base in TYPICAL_POWER and p["power_w"] > TYPICAL_POWER[base] * 1.4:
            risks.append(
                {
                    "code": "H06",
                    "text": f"{base} 电阻常见功率约 {TYPICAL_POWER[base]:g}W，本行标 {p['power_w']:g}W，要求规格书确认（物理合理性复核，不是直接淘汰）。",
                }
            )
        if "capacitance_f" in p and "voltage_v" in p and base == "0603" and p["voltage_v"] > 50:
            risks.append({"code": "H06", "text": "0603 陶瓷电容常见耐压 ≤50V，更高耐压需规格书确认。"})
        risks.append({"code": "H04", "text": "标题出现「散新/拆机/翻新/打磨/改字/洗板」→ 降权并人工复核。"})
    if category == "ic":
        risks.append({"code": "H02", "text": "后缀不同视为不同料；「国产兼容/可替代」只能作为 G2 提案，不能默认替代。"})
        risks.append({"code": "H04", "text": "翻新、散新、打磨、改字命中即淘汰；必须看丝印与实拍。"})
        risks.append({"code": "H07", "text": "店铺信息不可得且无实拍/正品证据 → 淘汰。"})
    if category == "power":
        risks.append({"code": "H04", "text": "功率器件是翻新/打磨重灾区：引脚氧化、丝印重影、批次混杂、显著低价 → 直接淘汰。"})
        risks.append({"code": "H07", "text": "必须能确认店铺类型或拿到实物正品证据，否则不下单。"})
    if category == "connector":
        risks.append({"code": "H01", "text": "间距 1.27mm 与 1.25mm 视为不同料；针数、排数、高度任一不符即淘汰。"})
    risks.append({"code": "H05", "text": "低于同规格候选中位价 P10 触发 H05 淘汰；低价不提升排名。"})
    risks.append({"code": "S01", "text": "高于 P90 需写明理由（关键件/原厂授权），否则降权。"})
    return risks


def _build_decision(category, package, p, mpn, desc, qty, critical, gates, queries, risks, subtype=""):
    pkg = package.split(" ")[0] if package else ""
    qty_text = f" ×{qty}" if qty else ""
    spec = params_text(category, p)
    if category == "passive":
        if "resistance_ohm" in p:
            what = f"{pkg} {fmt_resistance(p['resistance_ohm'])}"
            kind = "贴片电阻"
        elif "capacitance_f" in p:
            what = f"{pkg} {fmt_capacitance(p['capacitance_f'])}"
            kind = "贴片电容"
        elif "inductance_h" in p:
            what = f"{pkg} {fmt_inductance(p['inductance_h'])}"
            kind = "贴片电感"
        elif subtype in ("diode", "led", "bead", "fuse"):
            what = f"{mpn or desc} {pkg}".strip()
            kind = {"diode": "二极管", "led": "LED", "bead": "磁珠", "fuse": "保险丝"}.get(subtype, "被动件")
        else:
            what, kind = pkg, (desc or "被动件")
        tol = f" ±{p['tolerance_pct']:g}%" if "tolerance_pct" in p else ""
        headline = f"{what}{tol} {kind}{qty_text}".strip()
        if pkg == "插件" and kind.startswith("贴片"):
            headline = f"{what}{tol} 直插{kind[2:]}{qty_text}".strip()
    elif category == "ic":
        headline = f"{mpn or desc} {pkg} 原装{qty_text}".strip()
    elif category == "power":
        headline = f"{mpn or desc} {pkg} {spec}{qty_text}".strip()
    elif category == "connector":
        headline = f"{desc or mpn} {spec} {pkg}{qty_text}".strip()
    else:
        headline = f"{mpn or desc} {spec} {pkg}{qty_text}".strip()

    verify = [
        "封装/引脚数/间距与 BOM 完全一致（H01）",
        "型号含后缀，未授权不改型号（H02）",
        "关键参数不低于 BOM，无法确认按缺失处理（H03）",
        "无翻新/散新/拆机/打磨信号（H04）",
        "价格不低于同规格中位价 P10（H05）",
    ]
    if category in ("ic", "power"):
        verify.append("店铺类型/实拍/丝印可核验（H07）")
    if critical:
        verify.append("关键件：G3 逐行人工确认")

    return {
        "headline": re.sub(r"\s+", " ", headline).strip(),
        "buy_spec": gates,
        "search_queries": queries,
        "must_verify": verify,
        "risk_notes": risks,
        "gate": "G1 解析确认 → G3 候选确认" + ("（关键件逐行）" if critical else ""),
        "manual_approval_required": True,
        "next_action": "把候选标题/价格/店铺信息粘贴到下方评分区；本页不访问淘宝，不生成商品链接。",
    }


# --------------------------------------------------------------------------
# 整表分析
# --------------------------------------------------------------------------


def analyze(rows, options=None) -> dict:
    options = options or {}
    lines = []
    for i, row in enumerate(rows or []):
        try:
            lines.append(analyze_line(row, i + 1, options))
        except Exception as exc:  # 单行失败不拖垮整表
            lines.append(
                {
                    "line_no": (row or {}).get("line_no") or i + 1,
                    "row": row,
                    "category": "other",
                    "category_label": "其他",
                    "status": "needs_user",
                    "missing": ["解析失败"],
                    "blocking": [f"解析异常：{exc}"],
                    "hard_gates": [],
                    "query_plan": [],
                    "risks": [],
                    "decision": {
                        "headline": "该行解析失败，请人工检查",
                        "buy_spec": [],
                        "search_queries": [],
                        "must_verify": [],
                        "risk_notes": [],
                        "gate": "G1",
                        "manual_approval_required": True,
                        "next_action": "人工核对原始行后重新上传",
                    },
                }
            )

    counts = {"total": len(lines), "ready": 0, "needs_user": 0, "critical": 0}
    by_cat = {}
    for ln in lines:
        counts[ln.get("status", "needs_user")] = counts.get(ln.get("status", "needs_user"), 0) + 1
        if ln.get("criticality") == "critical":
            counts["critical"] += 1
        cat = ln.get("category", "other")
        by_cat[cat] = by_cat.get(cat, 0) + 1

    warnings = []
    seen, dup = set(), set()
    for ln in lines:
        no = ln.get("line_no")
        if no in seen:
            dup.add(no)
        seen.add(no)
        q = ln.get("qty")
        if q is not None and q <= 0:
            warnings.append(f"第 {no} 行数量为 {q}，应为正整数")
    if dup:
        warnings.append("行号重复：" + "、".join(str(x) for x in sorted(dup, key=str)))

    return {
        "engine_version": ENGINE_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": counts,
        "categories": by_cat,
        "lines": lines,
        "warnings": warnings,
        "compliance": {
            "manual_approval_required": True,
            "offline_only": True,
            "payment_by_human": True,
            "note": "本站只做离线规则决策：不登录、不访问、不操作淘宝，不生成商品链接，不产生支付动作。结算与付款永远人工完成。",
            "gates": [
                {"id": "G1", "name": "解析确认", "text": "确认字段/单位/封装/数量/关键件"},
                {"id": "G2", "name": "替代授权", "text": "型号/封装/参数变更必须逐项人工授权"},
                {"id": "G3", "name": "候选确认", "text": "主选/备选/价格/风险/缺失维度需人工确认"},
                {"id": "G4", "name": "加购清单", "text": "本页不执行加购；如另行加购须人工复核数量与购物车"},
                {"id": "G5", "name": "结算付款", "text": "永远由人在淘宝内完成，Skill 永不执行"},
                {"id": "G6", "name": "收货发票", "text": "地址/抬头/税号由人确认"},
            ],
        },
    }


# --------------------------------------------------------------------------
# 候选评分
# --------------------------------------------------------------------------

WEIGHTS = {
    "passive": {"match": 40, "shop": 10, "review": 15, "price": 20, "sales": 15},
    "ic": {"match": 35, "shop": 20, "review": 20, "price": 15, "sales": 10},
    "power": {"match": 35, "shop": 15, "review": 15, "price": 15, "sales": 20},
    "connector": {"match": 40, "shop": 10, "review": 15, "price": 15, "sales": 20},
    "module": {"match": 35, "shop": 20, "review": 20, "price": 15, "sales": 10},
    "other": {"match": 40, "shop": 15, "review": 15, "price": 15, "sales": 15},
}

CAND_ALIASES = {
    "title": ["标题", "商品标题", "title", "name"],
    "price": ["价格", "单价", "price"],
    "sales": ["销量", "月销", "sales", "sold"],
    "rating": ["好评率", "评分", "rating", "good_rate"],
    "shop_type": ["店铺类型", "店铺", "shop_type", "shop"],
    "is_tmall": ["天猫", "tmall", "is_tmall"],
    "real_photo": ["实拍", "有实拍", "real_photo", "photo"],
    "note": ["备注", "note", "remark"],
}


def _cand_key(name):
    n = re.sub(r"[\s_\-/]+", "", str(name or "").strip().lower())
    for canon, aliases in CAND_ALIASES.items():
        for a in aliases:
            if n == re.sub(r"[\s_\-/]+", "", a.lower()):
                return canon
    return None


def _parse_number(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("¥", "").replace("￥", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _parse_bool(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "是", "有", "√")


def parse_candidates_csv(text: str):
    text = (text or "").strip()
    if not text:
        raise ValueError("候选为空：请粘贴含表头的 CSV（title,price,sales,rating,shop_type,is_tmall,real_photo）")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("候选缺少表头")
    mapping = {}
    for f in reader.fieldnames:
        k = _cand_key(f)
        if k and k not in mapping:
            mapping[k] = f
    if "title" not in mapping:
        raise ValueError("候选表头缺少 title（或「标题」）列")
    out = []
    for row in reader:
        title = str(row.get(mapping["title"]) or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "price": _parse_number(row.get(mapping.get("price", ""), "")) if "price" in mapping else None,
                "sales": _parse_number(row.get(mapping.get("sales", ""), "")) if "sales" in mapping else None,
                "rating": _parse_number(row.get(mapping.get("rating", ""), "")) if "rating" in mapping else None,
                "shop_type": str(row.get(mapping.get("shop_type", ""), "") or "").strip(),
                "is_tmall": _parse_bool(row.get(mapping.get("is_tmall", ""), "")),
                "real_photo": _parse_bool(row.get(mapping.get("real_photo", ""), "")),
                "note": str(row.get(mapping.get("note", ""), "") or "").strip(),
            }
        )
    if not out:
        raise ValueError("候选表里没有有效数据行")
    return out


def _norm_text(s):
    return re.sub(r"\s+", "", str(s or "")).lower().replace("Ω", "ω")


def _percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] * (c - k) + vals[c] * (k - f)


def _shop_score(cand):
    s = str(cand.get("shop_type") or "")
    if "旗舰" in s or "官方" in s:
        return 100, "旗舰/官方店"
    if "专营" in s or "专卖" in s or "企业" in s:
        return 80, "企业/专营店"
    if "个人" in s or "c店" in s.lower() or "c 店" in s:
        return 55, "个人 C 店"
    if cand.get("is_tmall"):
        return 85, "天猫（店铺类型未知）"
    return None, "店铺类型未知"


def _review_score(cand):
    rating = cand.get("rating")
    photo = bool(cand.get("real_photo"))
    base = None
    if rating is not None:
        if rating <= 1:
            base = rating * 100
        elif rating <= 100:
            base = rating
    if base is None and not photo:
        return None
    if base is None:
        base = 66
    if photo:
        base = min(100, base + 8)
    return base


def _value_match(actual, required, rel_tol):
    """数值匹配。required 为 0（如 0Ω 跳线电阻）时改用绝对差，避免除零。"""
    if actual is None or required is None:
        return False
    if required <= 0:
        return actual <= max(abs(required), 1e-6)
    return abs(actual - required) / required <= rel_tol


def _module_family_hit(line, title: str) -> bool:
    """模块类没有型号时的兜底：标题里同时出现「品类词」和「描述里的特征词」就算对上。

    淘宝上大量模块（亿佰特 E104-BT40 这类）标题根本不写型号，只写
    「双模蓝牙 BLE5.4 透传模块 SPP3.0」。硬要求型号会让整行永远无解。
    这里只在「模块」语境下放宽，并把「型号未标注」记进风险，报告里能看见。
    """
    desc = str(line.get("desc") or "")
    mpn = str(line.get("mpn") or "").strip()
    if mpn:
        desc = re.sub(re.escape(mpn), " ", desc, flags=re.I)
    blob = _norm_text(f"{line.get('package') or ''} {desc}")
    if not any(w in blob for w in ("模块", "module", "蓝牙", "无线", "射频", "wifi")):
        return False
    generic = [w for w in ("蓝牙", "无线", "模块", "通信", "透传", "射频") if w in _norm_text(title)]
    tokens = [
        t for t in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", desc)
        if len(t) >= 3 and not t.isdigit()
    ]
    spec_hits = [t for t in tokens if _norm_text(t) and _norm_text(t) in _norm_text(title)]
    return bool(generic) and len(spec_hits) >= 1


def _check_title(line, cand, extra_ctx):
    """返回 (reject_code, reject_text, checks[(label, weight, hit)])。"""
    title = str(cand.get("title") or "")
    nt = _norm_text(title)
    cat = line.get("category") or "other"
    p = line.get("params") or {}
    pkg = (line.get("package") or "").split(" ")[0]
    checks = []

    # 风险词
    hit_risk = [w for w in RISK_WORDS if w in title]

    # 封装
    # 料号里也含数字串：TS-1088-AR02016 里的「0201」不是 0201 封装，
    # 直接扫原文本会把整行判成「封装不符」。先把 MPN 和长料号从扫描面里剔掉
    # （必须在带空格的原文上剔，_norm_text 会把空白全删掉、把 0805 和 10k 粘成一个词）。
    scan_src = title
    _mpn_raw = str(line.get("mpn") or "").strip()
    if _mpn_raw:
        scan_src = re.sub(re.escape(_mpn_raw), " ", scan_src, flags=re.I)
    scan_src = re.sub(r"[A-Za-z0-9][A-Za-z0-9\-_/#]{5,}", " ", scan_src)
    scan = _norm_text(scan_src)
    pkg_found = []
    for v in ("0201", "0402", "0603", "0805", "1206", "1210", "1812", "2512"):
        if v in scan:
            pkg_found.append(v)
    if pkg:
        variants = pkg_variants(pkg)
        if cat == "connector":
            # Altium 库名（XH2.50mm-4P / 排针 2.54mm 2P）跟标题写法完全对不上，
            # 按系列 + 针数比对才有意义。
            variants = connector_pkg_variants(pkg)
        pkg_hit = any(v.lower() in nt for v in variants)
        pkg_conflict = bool(pkg_found) and not any(v in pkg_found for v in variants)
        checks.append((f"封装 {pkg}", 30 if cat == "passive" else 20, pkg_hit))
        if cat == "passive":
            # A listing that spans 0402/0603/0805/1206 often has the required
            # package disabled; prefer a listing dedicated to one package.
            checks.append(("单一封装链接", 15, len(set(pkg_found)) < 3))
        if pkg_conflict:
            return "H01", f"标题封装为 {'/'.join(pkg_found)}，与要求 {pkg} 不符", checks, hit_risk
    else:
        pkg_hit = True

    # 型号
    mpn = str(line.get("mpn") or "").strip()
    if mpn and cat in ("ic", "power", "module"):
        mpn_hit = _norm_text(mpn) in nt
        checks.append(("完整型号", 55 if cat == "ic" else 40, mpn_hit))
        if not mpn_hit:
            if _module_family_hit(line, title):
                hit_risk = hit_risk + ["型号未标注"]
                checks.append(("按模块特征匹配", 10, True))
            else:
                return "H02", f"标题未出现完整型号 {mpn}", checks, hit_risk

    if cat == "passive":
        req_r, req_c, req_i = p.get("resistance_ohm"), p.get("capacitance_f"), p.get("inductance_h")
        tol = p.get("tolerance_pct")
        t_r, t_c, t_i = _parse_resistance(title), _parse_capacitance(title), _parse_inductance(title)
        if req_r is not None:
            # 0Ω 跳线电阻：要求值为 0，相对误差会除零，改用绝对差
            hit = t_r is not None and _value_match(t_r, req_r, 0.005)
            checks.append((f"阻值 {fmt_resistance(req_r)}", 35, hit))
            if t_r is not None and not hit:
                return "H03", f"标题阻值 {fmt_resistance(t_r)} ≠ 要求 {fmt_resistance(req_r)}", checks, hit_risk
        if req_c is not None:
            hit = t_c is not None and _value_match(t_c, req_c, 0.02)
            checks.append((f"容值 {fmt_capacitance(req_c)}", 35, hit))
            if t_c is not None and not hit:
                return "H03", f"标题容值 {fmt_capacitance(t_c)} ≠ 要求 {fmt_capacitance(req_c)}", checks, hit_risk
        if req_i is not None:
            hit = t_i is not None and _value_match(t_i, req_i, 0.02)
            checks.append((f"感值 {fmt_inductance(req_i)}", 35, hit))
            if t_i is not None and not hit:
                return "H03", f"标题感值 {fmt_inductance(t_i)} ≠ 要求 {fmt_inductance(req_i)}", checks, hit_risk
        if tol is not None:
            # 容差只做加减分，不再淘汰：只要数值/耐压/封装对，±20% 以内都可用
            t_tol = _parse_tolerance(title)
            tol_cap = max(TOLERANCE_RELAX_PCT, float(tol))
            hit = t_tol is not None and t_tol <= tol_cap + 1e-9
            checks.append((f"精度 ≤ ±{tol_cap:g}%（放宽）", 8, hit))
        if "power_w" in p:
            t_pw = _parse_power(title)
            checks.append((f"功率 ≥ {p['power_w']:g}W", 10, t_pw is not None and t_pw >= p["power_w"] * 0.999))
        if "voltage_v" in p:
            # 耐压必须够：只有标题里所有电压都低于要求才淘汰（多电压链接留给 SKU 去选）
            req_v = p["voltage_v"]
            t_vs = _parse_voltages(title)
            enough = [v for v in t_vs if v >= req_v - 1e-9]
            checks.append((f"耐压 ≥ {req_v:g}V", 20, bool(enough)))
            if t_vs and not enough:
                return "H03", f"标题耐压 {'/'.join(f'{v:g}' for v in t_vs)}V 均低于要求 {req_v:g}V", checks, hit_risk
        if "current_a" in p:
            # 保险丝：档位必须对上（标题写成 1/1.5/2/3/8/25A 这种整档链接也算有）
            req_a = p["current_a"]
            t_as = title_amps(title)
            hit = any(abs(a - req_a) <= max(0.005, req_a * 0.01) for a in t_as)
            checks.append((f"电流 {req_a:g}A", 35, hit))
            if t_as and not hit:
                return (
                    "H03",
                    f"标题电流 {'/'.join(f'{a:g}' for a in t_as[:6])}A 没有 {req_a:g}A 这一档",
                    checks,
                    hit_risk,
                )
    elif cat == "power":
        if "channel" in p:
            t_ty = _parse_type(title)
            checks.append((f"{p['channel']} 沟道", 15, t_ty == p["channel"]))
            if t_ty and t_ty != p["channel"]:
                return "H03", f"标题为 {t_ty} 沟道，要求 {p['channel']} 沟道", checks, hit_risk
        if "vds_v" in p:
            t_v = _parse_voltage(title)
            checks.append((f"Vds ≥ {p['vds_v']:g}V", 15, t_v is not None and t_v >= p["vds_v"] - 1e-9))
            if t_v is not None and t_v < p["vds_v"] - 1e-9:
                return "H03", f"标题耐压 {t_v:g}V 低于要求 {p['vds_v']:g}V", checks, hit_risk
        if "id_a" in p:
            t_i = _parse_current(title)
            checks.append((f"Id ≥ {p['id_a']:g}A", 15, t_i is not None and t_i >= p["id_a"] - 1e-9))
            if t_i is not None and t_i < p["id_a"] - 1e-9:
                return "H03", f"标题电流 {t_i:g}A 低于要求 {p['id_a']:g}A", checks, hit_risk
        if "rds_mohm" in p:
            t_rds = _parse_rds(title)
            checks.append((f"Rds(on) ≤ {p['rds_mohm']:g}mΩ", 15, t_rds is not None and t_rds <= p["rds_mohm"] * 1.001))
            if t_rds is not None and t_rds > p["rds_mohm"] * 1.001:
                return "H03", f"标题 Rds(on) {t_rds:g}mΩ 高于要求 {p['rds_mohm']:g}mΩ", checks, hit_risk
    elif cat == "connector":
        # 排针 / 排母、单排 / 双排 是不同料：标题写反必须直接淘汰，不能只看针数。
        want = _norm_text(
            " ".join(
                [str(line.get(k) or "") for k in ("params_text", "desc", "mpn", "package")]
                + [str((line.get("row") or {}).get("params") or "")]
            )
        )
        wants_header = any(w in want for w in ("排针", "header", "针座"))
        wants_female = any(w in want for w in ("排母", "母座", "母头"))
        if wants_header and not wants_female and "母" in nt:
            return "H01", "标题是排母/母座（母头），BOM 要排针（公头）", checks, hit_risk
        if "单排" in want and "双排" in nt and "单排" not in nt:
            return "H01", "标题是双排，BOM 要单排", checks, hit_risk
        series = connector_series(line.get("desc"), line.get("package"), line.get("mpn"))
        # 排针/排母整条卖（40P 一条、自行剪断），针数多于需求可用；XH/端子台必须严格等针
        loose_pins = series in ("排针", "排母")
        if "pins" in p:
            t_pins = _parse_pins(title)
            want_pins = p["pins"]
            ok = t_pins == want_pins or (loose_pins and t_pins is not None and t_pins >= want_pins)
            checks.append((f"针数 {want_pins}P", 30, ok))
            if t_pins is not None and not ok:
                if loose_pins and t_pins < want_pins:
                    return "H01", f"标题 {t_pins}P 少于要求 {want_pins}P，剪不出这么多针", checks, hit_risk
                if not loose_pins:
                    return "H01", f"标题 {t_pins}P ≠ 要求 {want_pins}P", checks, hit_risk
        if "pitch_mm" in p:
            t_pitch = _parse_pitch(title)
            if t_pitch is None:
                t_pitch = _parse_pitch_loose(title)
            same = t_pitch is not None and pitch_match(t_pitch, p["pitch_mm"])
            checks.append((f"间距 {p['pitch_mm']:g}mm", 30, same))
            if t_pitch is not None and not same:
                return "H01", f"标题间距 {t_pitch:g}mm ≠ 要求 {p['pitch_mm']:g}mm", checks, hit_risk
    else:
        desc = _norm_text(line.get("desc"))
        checks.append(("名称匹配", 60, bool(desc) and desc in nt))
        if pkg:
            checks.append((f"封装 {pkg}", 40, any(v.lower() in nt for v in pkg_variants(pkg))))

    return None, "", checks, hit_risk


# --------------------------------------------------------------------------
# 「买一份实际发 N 只」——整包报价的包装数识别
#
# 淘宝散料（阻容感/排针/保险丝…）常按整包报价：1 份 = 50/100/1000 只。
# 只按 BOM 用量填数量会严重少买；按裸报价比价又会误判「异常低价」。
# 所以比价一律折算成单只价，下单数量按 ceil(用量 / 每份只数) 折算。
# --------------------------------------------------------------------------

_PACK_EXPLICIT = [
    # 1盘1000 / 一盘1000只 / 整盘=1000 / 每袋500（数字后面不带量词，编带盘装最常这么写）
    # 注意排除「1盘 47UF」「1盘 50V」这种后面跟参数单位的写法
    (
        re.compile(
            r"(?:^|[^\d.A-Za-z一])(?:一|1|整|每|单)\s*(?:盘|袋|盒|箱|卷|排|条|罐|管|托盘|编带)"
            r"\s*[=＝:：]?\s*(?:约|共|装|有)?\s*(\d{2,6})(?![\d.])"
            r"(?!\s*(?:[uμnp]?[fF]\b|[vV]\b|[wW]\b|[oΩ]\b|%|mm\b|mil\b|pin\b|[kKmM]\s*[oΩ]))",
            re.I,
        ),
        "1盘N",
    ),
    # 1000/盘、5000/卷（数字在前，单位在后）
    (
        re.compile(r"(?<![\d.])(\d{2,6})\s*(?:只|片|颗|粒|个)?\s*[/／]\s*(?:盘|袋|盒|卷|箱|罐|托盘)", re.I),
        "N/盘",
    ),
    # 50只/包、100个/袋、1000pcs/卷
    (re.compile(r"(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)\s*[/／]\s*(?:包|袋|盒|卷|盘|排|罐|箱|组|套|份|件)", re.I), "N只/包"),
    # 1包50个、一袋100只、一件50只、1卷=5000只
    (re.compile(r"(?:一|1)\s*(?:包|袋|盒|卷|盘|排|罐|箱|组|套|件)\s*[=＝]?\s*(?:约|共|装|有)?\s*(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)", re.I), "1包N只"),
    # 每包50个
    (re.compile(r"(?:每包|每袋|每盒|每卷|每盘|每排|每罐|每箱)\s*(?:约|共|装|有)?\s*(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)", re.I), "每包N只"),
    # 拍1发50、拍下1件发50只、买1件发50只（「拍1发50 0603电阻」不带量词也要认）
    (re.compile(r"(?:拍|买)下?\s*1\s*(?:件|份|个|单)?\s*(?:发|送|得|给|共|约)\s*(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)?(?![\d.])", re.I), "拍1发N只"),
    # 1件=50个
    (re.compile(r"1\s*(?:件|份|个|单)\s*[=＝]\s*(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)", re.I), "1份=N只"),
    # 50个装、100只装、50只一包
    (re.compile(r"(\d+)\s*(?:个|只|片|支|条|颗|件|粒|枚|pcs)\s*(?:装|一包|每包|左右|起)", re.I), "N只装"),
]
# 「数字 + 件数量词」：只/片/支/颗/粒/枚/pcs 本身就是件数，标题里出现基本就是包装数
_PACK_WEAK_UNIT = re.compile(r"(\d+)\s*(?:只|片|支|条|颗|件|粒|枚|pcs)", re.I)
# 「数字 + 个」太含糊（3个串口 / 4个通道），只在标题带包/装/袋等词时才认
_PACK_GENERIC = re.compile(r"(\d+)\s*个", re.I)
# 量词后面跟这些词说明「N个」是规格参数，不是包装数（3个引脚 / 4个通道）
_PACK_FALSE = re.compile(
    r"^\s*(?:引?脚|管脚|通道|焊盘|针|孔|线|位|层|极|触点|端子|接口|串口|端口|档位|瓦|安|伏|欧|法|亨)", re.I
)
# 标题里出现这些词，才允许用「弱」写法（避免把「3个串口」当成 3 只/份）
_PACK_HINT_WORDS = ("包", "装", "袋", "盒", "卷", "盘", "份", "拍", "发", "整")


def _pack_number(match, text: str):
    n = int(match.group(1))
    if not (2 <= n <= 100000):
        return None
    if _PACK_FALSE.match(text[match.end():]):
        return None
    if re.search(r"[±]\s*$", text[: match.start()]):
        return None
    return n


def detect_pack(text, explicit_only: bool = True, allow_generic: bool = False):
    """从一句话里解析「一份含多少只」，返回 (数量, 命中文本)。

    explicit_only=True 只认「100只/包」这类明确写法；=False 时再认「100只」；
    allow_generic=True 才把「50个」也算进去（「个」容易和 3个引脚 这类规格混）。
    """
    s = str(text or "")
    if not s:
        return None, ""
    for pat, _kind in _PACK_EXPLICIT:
        for m in pat.finditer(s):
            n = _pack_number(m, s)
            if n:
                return n, s.strip()[:60]
    if explicit_only:
        return None, ""
    for m in _PACK_WEAK_UNIT.finditer(s):
        n = _pack_number(m, s)
        if n:
            return n, s.strip()[:60]
    if allow_generic:
        for m in _PACK_GENERIC.finditer(s):
            n = _pack_number(m, s)
            if n:
                return n, s.strip()[:60]
    return None, ""


def pack_from_texts(texts, explicit_only: bool = True, allow_generic: bool = False):
    for t in texts or []:
        n, src = detect_pack(t, explicit_only=explicit_only, allow_generic=allow_generic)
        if n:
            return n, src
    return None, ""


def title_pack(title: str):
    """标题里的包装数：先看明确写法，再按「包/装/袋」等词决定要不要放宽。"""
    n, src = detect_pack(title, explicit_only=True)
    if n:
        return n, src
    if any(w in str(title or "") for w in _PACK_HINT_WORDS):
        return detect_pack(title, explicit_only=False, allow_generic=True)
    return detect_pack(title, explicit_only=False)


def pack_size(texts):
    """兼容旧接口：只返回包装数。"""
    n, _ = pack_from_texts(texts)
    return n


def plan_qty(need, pack):
    """BOM 需要 need 只、一份 pack 只 → (加购份数, 每份只数, 实际到手只数)。"""
    need = int(need or 0)
    if need < 1:
        need = 1
    p = int(pack or 1)
    if p < 2:
        return need, 1, need
    q = max(1, math.ceil(need / p))
    return q, p, q * p


def score_candidates(line, csv_text=None, candidates=None, execution=False) -> dict:
    if candidates is None:
        candidates = parse_candidates_csv(csv_text)
    cat = line.get("category") or "other"
    weights = WEIGHTS.get(cat, WEIGHTS["other"])

    prepared = []
    # 买「够这一行用」要花多少钱，而不是单只看多少钱：
    # BOM 只要 1 个电阻，链接是 100 只/包 ¥3.5 —— 单只价 0.035 看着最便宜，
    # 但实际要掏 3.5 还多拿 99 个。按采购合计比价，才会去别家找散卖。
    _need = max(1, int(line.get("qty") or 1))
    for c in candidates:
        code, text, checks, risk_hits = _check_title(line, c, {})
        _pack, _packsrc = title_pack(c.get("title") or "")
        _pack = _pack if (_pack and _pack > 1) else None
        _price = c.get("price")
        if _pack:
            _buy, _per, _actual = plan_qty(_need, _pack)
            _over = round(_actual / _need, 2)
            _cost = round(_price * _buy, 4) if _price is not None else None
        else:
            _buy, _actual, _over = _need, _need, 1.0
            _cost = round(_price * _need, 4) if _price is not None else None
        prepared.append(
            {
                "title": c["title"],
                "price": _price,
                "pack_size": _pack,
                "unit_price": round(_price / _pack, 6) if (_pack and _price) else _price,
                "buy_qty": _buy,
                "actual_pcs": _actual,
                "over_ratio": _over,
                "cost": _cost,
                "sales": c.get("sales"),
                "rating": c.get("rating"),
                "shop_type": c.get("shop_type"),
                "is_tmall": c.get("is_tmall"),
                "real_photo": c.get("real_photo"),
                "note": c.get("note"),
                "index": c.get("index"),
                "url": c.get("url"),
                "reject_code": code,
                "reject_text": text,
                "checks": checks,
                "risk_hits": risk_hits,
            }
        )

    # 结构门槛通过者才进入价格统计（H05 之前）
    survivors = [x for x in prepared if not x["reject_code"]]
    stats_pool = [x for x in survivors if not x["risk_hits"]] or survivors
    prices = [x["unit_price"] for x in stats_pool if x.get("unit_price") is not None and x["unit_price"] > 0]
    stats = {"count": len(prices), "median": None, "p10": None, "p25": None, "p75": None, "p90": None, "price_conclusion": False}
    if len(prices) >= 5:
        stats.update(
            {
                "median": round(_percentile(prices, 0.5), 4),
                "p10": round(_percentile(prices, 0.1), 4),
                "p25": round(_percentile(prices, 0.25), 4),
                "p75": round(_percentile(prices, 0.75), 4),
                "p90": round(_percentile(prices, 0.9), 4),
                "price_conclusion": True,
            }
        )

    # 采购合计价（按需买够的总花费）单独统计：价格维度改用它，H05 仍看单只价
    costs = [x["cost"] for x in stats_pool if x.get("cost") is not None and x["cost"] > 0]
    cost_stats = {"count": len(costs), "median": None, "p10": None, "p25": None, "p75": None, "p90": None, "price_conclusion": False}
    if len(costs) >= 5:
        cost_stats.update(
            {
                "median": round(_percentile(costs, 0.5), 4),
                "p10": round(_percentile(costs, 0.1), 4),
                "p25": round(_percentile(costs, 0.25), 4),
                "p75": round(_percentile(costs, 0.75), 4),
                "p90": round(_percentile(costs, 0.9), 4),
                "price_conclusion": True,
            }
        )

    max_sales = max([x["sales"] for x in stats_pool if x.get("sales")], default=None)

    for x in prepared:
        reasons, risks = [], []
        cand = {"title": x["title"], "shop_type": x["shop_type"], "is_tmall": x["is_tmall"], "real_photo": x["real_photo"], "rating": x["rating"]}

        if x["reject_code"]:
            x["score"] = 0
            x["decision"] = "淘汰"
            x["reasons"] = [f"{x['reject_code']}：{x['reject_text']}"]
            x["risks"] = [{"code": x["reject_code"], "text": x["reject_text"]}]
            x["hits"] = [c[0] for c in x["checks"] if c[2]]
            x["misses"] = [c[0] for c in x["checks"] if not c[2]]
            continue

        price = x.get("unit_price")
        _ptag = "单只价" if x.get("pack_size") else "价格"
        if stats["price_conclusion"] and price is not None:
            if price < stats["p10"] and price < (stats["median"] or 0) * 0.75:
                x["score"] = 0
                x["decision"] = "淘汰"
                x["reasons"] = [f"H05：{_ptag} {price:g} 低于同规格 P10（{stats['p10']:g}），异常低价不参与排名"]
                x["risks"] = [{"code": "H05", "text": "低于 P10"}]
                if x["risk_hits"]:
                    x["risks"].insert(0, {"code": "H04", "text": "标题含风险词 " + "/".join(x["risk_hits"])})
                    x["reasons"].append("同时命中 H04 风险词：" + "/".join(x["risk_hits"]))
                x["hits"] = [c[0] for c in x["checks"] if c[2]]
                x["misses"] = [c[0] for c in x["checks"] if not c[2]]
                continue

        shop, shop_note = _shop_score(cand)
        review = _review_score(cand)
        if max_sales and x.get("sales"):
            sales_score = 100 * math.log10(1 + x["sales"]) / math.log10(1 + max_sales)
        else:
            sales_score = None

        if cost_stats["price_conclusion"] and x.get("cost") is not None:
            cost = x["cost"]
            if cost <= cost_stats["p25"]:
                price_score = 88
                reasons.append("采购合计价处于低价区间（P25 以下），需确认不是引流价")
            elif cost <= cost_stats["p75"]:
                price_score = 100
                reasons.append("采购合计价落在 P25–P75 正常区间")
            elif cost <= cost_stats["p90"]:
                price_score = 78
                reasons.append("采购合计价高于 P75，仍可接受")
            else:
                price_score = 52
                risks.append({"code": "S01", "text": "高于 P90，需写明关键件/原厂授权理由"})
                reasons.append("采购合计价高于 P90")
        else:
            price_score = None

        if x.get("pack_size") and x.get("price"):
            reasons.append(
                f"整包报价：{x['pack_size']} 只/份，¥{x['price']:g} → 单只 ¥{x['unit_price']:.4g}"
            )

        checks = x["checks"]
        if checks:
            total_w = sum(c[1] for c in checks)
            hit_w = sum(c[1] for c in checks if c[2])
            match_score = 100 * hit_w / total_w if total_w else None
        else:
            match_score = None

        if cat in ("ic", "power") and shop is None and not cand["real_photo"]:
            if not execution:
                x["score"] = 0
                x["decision"] = "淘汰"
                x["reasons"] = ["H07：店铺类型不可得且无实拍/正品证据"]
                x["risks"] = [{"code": "H07", "text": "店铺与实物证据均缺失"}]
                x["hits"] = [c[0] for c in checks if c[2]]
                x["misses"] = [c[0] for c in checks if not c[2]]
                continue
            risks.append({"code": "H07", "text": "店铺类型不可得，加购前必须人工确认正品证据"})

        dims = {"match": match_score, "shop": shop, "review": review, "price": price_score, "sales": sales_score}
        known = {k: v for k, v in dims.items() if v is not None}
        if not known:
            x["score"] = 0
            x["decision"] = "淘汰"
            x["reasons"] = ["没有任何可核实维度，无法评分"]
            x["risks"] = [{"code": "A06", "text": "数据不足"}]
            x["hits"] = []
            x["misses"] = [k for k in dims]
            continue

        wsum = sum(weights[k] for k in known)
        score = sum(weights[k] * known[k] for k in known) / wsum

        if x["risk_hits"]:
            if cat in ("ic", "power"):
                x["score"] = 0
                x["decision"] = "淘汰"
                x["reasons"] = [f"H04：标题含风险词 {'/'.join(x['risk_hits'])}"]
                x["risks"] = [{"code": "H04", "text": f"标题含 {'/'.join(x['risk_hits'])}"}]
                x["hits"] = [c[0] for c in checks if c[2]]
                x["misses"] = [c[0] for c in checks if not c[2]]
                continue
            score -= 12
            risks.append({"code": "H04", "text": f"标题含风险词 {'/'.join(x['risk_hits'])}，被动件降权并人工复核"})
            reasons.append("标题含翻新/散新类风险词，已降权")

        # 整包超量：只买 1 个却要到手 100 个，钱多花、料还占地方。
        # 注意：惩罚只进 rank_score（影响排序），不改 score/decision ——
        # 万一一整行只有整包卖，扣到 60 以下会直接判淘汰、整行采购失败，
        # 那是把「不划算」误判成「买不到」。
        over = x.get("over_ratio") or 1.0
        penalty = 0
        if x.get("pack_size") and over > 1:
            tail = f"合计 ¥{x['cost']:g}" if x.get("cost") is not None else ""
            reasons.append(
                f"买 {x['buy_qty']} 份 → 到手 {x['actual_pcs']} 只"
                f"（需求 {_need}，超量 {over:.1f} 倍）{('，' + tail) if tail else ''}"
            )
            if over >= 20:
                penalty = 30
                risks.append({"code": "OV01", "text": f"超量 {over:.0f} 倍"})
                reasons.append(f"超量太狠（{over:.0f} 倍），优先换一家卖散只/小包装的店")
            elif over >= 8:
                penalty = 18
                risks.append({"code": "OV01", "text": f"超量 {over:.0f} 倍"})
            elif over >= 3:
                penalty = 8
        x["over_penalty"] = penalty
        x["rank_score"] = round(max(0.0, score - penalty), 1)

        score = max(0, round(score, 1))
        x["score"] = score
        if score >= 75:
            x["decision"] = "主选"
        elif score >= 60:
            x["decision"] = "备选"
        else:
            x["decision"] = "淘汰"

        hits = [c[0] for c in checks if c[2]]
        misses = [c[0] for c in checks if not c[2]]
        if hits:
            reasons.insert(0, "命中：" + "、".join(hits))
        if misses:
            reasons.append("未核实：" + "、".join(misses) + "（权重按已知维度分摊）")
        if shop is not None:
            reasons.append(f"店铺：{shop_note}")
        elif "shop" not in known:
            reasons.append("缺失维度：店铺类型（不按 0 分计）")
        if review is None:
            reasons.append("缺失维度：评价质量/实拍（不按 0 分计）")
        if price_score is None:
            reasons.append("缺失维度：价格（候选 <5 条，不下价格结论）")
        if sales_score is None:
            reasons.append("缺失维度：销量")
        if cand["real_photo"]:
            reasons.append("有实拍证据")

        x["hits"] = hits
        x["misses"] = misses
        x["reasons"] = reasons
        x["risks"] = risks

    ranked = sorted(
        prepared,
        key=lambda z: (
            -(z.get("rank_score") if z.get("rank_score") is not None else (z.get("score") or 0)),
            z.get("cost") if z.get("cost") is not None else 1e9,
            z.get("over_ratio") or 1.0,
        ),
    )
    for i, x in enumerate(ranked, 1):
        x["rank"] = i

    top = [x for x in ranked if (x.get("score") or 0) > 0]
    ambiguous = False
    if len(top) >= 2 and top[0]["score"] >= 60:
        if top[0]["score"] - top[1]["score"] < 5 and top[1]["score"] >= 60:
            ambiguous = True

    if not top:
        verdict = "无合格候选：全部触发硬门槛或数据不足。请补充店铺类型/实拍信息，或换搜索词后重新收集候选。"
    elif top[0]["score"] >= 75:
        verdict = f"建议主选：{top[0]['title']}（{top[0]['score']} 分）" + ("；前两名过于接近，需人工裁决。" if ambiguous else "")
    elif top[0]["score"] >= 60:
        verdict = f"仅有备选：{top[0]['title']}（{top[0]['score']} 分），须人工确认关键属性后再下单。"
    else:
        verdict = "最高分低于 60，按规则淘汰；建议换关键词重搜或走 G2 替代授权。"

    return {
        "line_no": line.get("line_no"),
        "category": cat,
        "weights": weights,
        "stats": stats,
        "cost_stats": cost_stats,
        "need": _need,
        "candidates": ranked,
        "verdict": verdict,
        "ambiguous": ambiguous,
        "note": "评分只使用你提供的字段；缺失维度按比例分摊权重，不会按 0 分处理。价格结论仅在 ≥5 条同规格候选时给出。",
    }


# --------------------------------------------------------------------------
# CLI（便于本地自测）
# --------------------------------------------------------------------------


def _main(argv):
    import json
    import sys

    if len(argv) < 2:
        print("用法: python engine.py analyze <bom.csv|xlsx> [candidates.csv]")
        return 1
    path = Path(argv[1])
    parsed = parse_workbook(path.name, path.read_bytes())
    result = analyze(parsed["rows"])
    if len(argv) > 2:
        cand_path = Path(argv[2])
        line = result["lines"][0]
        result["score_demo"] = score_candidates(line, cand_path.read_text(encoding="utf-8"))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv))
