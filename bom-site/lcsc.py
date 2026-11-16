#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""立创商城（LCSC）器件库查询 —— 把「立创编号」换成权威规格。

为什么需要它
------------
嘉立创EDA 导出的 BOM 每行都带一个立创编号（`Supplier Part` 列，形如 `C46749`），
这是唯一确定物料身份的东西。嘉立创官方对 BOM 匹配的规定是：

  1. 有编号 → **以编号为准**。编号与 Comment/封装写得不一样时信编号，官方原话：
     「错误的编号比缺少编号更容易造成错误匹配」。
  2. 无编号 → 按「器件值 → 封装 → 库存 → 基础库 → 拓展库」逐级匹配。
  3. 匹配不上的标「待确认」，不替用户猜。

本模块只做这两条里最保守的部分：

  - `resolve(code)`      立创编号 → 权威规格（型号/品牌/封装/阻值/耐压/精度/功率/库存）
  - `resolve_model(mpn)` 完整型号 → 同上，但要求 productModel **完全相等**才认

**绝不**根据模糊搜索结果改写设计值 —— 那是换规格，不是换词。
所有函数失败即返回 None，不抛异常、不阻塞主流程；结果落盘缓存，离线可复算。
全程只读：不登录、不下单、不碰购物车。

数据来源（公开接口，无需登录）
  GET  https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C25804
  POST https://wmsc.lcsc.com/ftps/wm/product/query/list   {"keyword": "...", "pageSize": n}
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

_DETAIL_API = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"
_LIST_API = "https://wmsc.lcsc.com/ftps/wm/product/query/list"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 详情缓存 30 天（器件规格几乎不变，库存会变但不影响选型）；查不到的结果只记 6 小时，
# 免得新品上架后永远查不到。
_TTL_HIT = 30 * 86400
_TTL_MISS = 6 * 3600

CACHE_VERSION = 1


# --------------------------------------------------------------------------
# 编号 / 型号 规范化
# --------------------------------------------------------------------------

_RE_CODE = re.compile(r"^[Cc]\s*(\d{3,9})$")


def normalize_code(text) -> str:
    """把 "c46749" / "C 46749" / " C46749 " 统一成 "C46749"；不合规返回空串。"""
    m = _RE_CODE.match(str(text or "").strip())
    return f"C{m.group(1)}" if m else ""


# 一行里找立创编号时看的列名（不同导出模板叫法不一样）
_CODE_COL_HINTS = ("supplierpart", "供应商编号", "立创编号", "嘉立创编号", "lcsc")


def code_of_row(row: dict) -> str:
    """从一行里取立创编号。

    先看 `lcsc` 字段（parse_workbook 会填）；老会话/旧数据只有 `raw` 原始列，
    就在 raw 里按列名找，再不济找「值本身就是 C+数字」的那一列。
    """
    if not isinstance(row, dict):
        return ""
    code = normalize_code(row.get("lcsc"))
    if code:
        return code
    raw = row.get("raw")
    if not isinstance(raw, dict):
        return ""
    for k, v in raw.items():
        key = re.sub(r"[\s_\-/\.\(\)（）#]+", "", str(k or "").lower())
        if any(h in key for h in _CODE_COL_HINTS):
            code = normalize_code(v)
            if code:
                return code
    # 不做「整行扫一遍 C+数字」的兜底：名称列里出现 C123 之类会绑错编号，
    # 而错编号比没编号更危险（嘉立创官方 BOM 规范原话）。
    return ""


def normalize_model(text) -> str:
    """型号归一化：统一 Ω/µ 写法、去空白、转大写。

    只做「同一个型号的不同写法」这一层，不做任何模糊化 —— 因为
    `resolve_model` 要求完全相等才认，放宽了就等于替用户换料。
    归一化结果只用于**比较和缓存键**，发去检索的始终是原始型号。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("µ", "u").replace("μ", "u")
    s = re.sub(r"(?i)OHMS?(?![A-Z])", "Ω", s)
    # Ω 有几个同形码位（U+2126 欧姆符号 / U+03A9 希腊字母），统一成后者
    s = s.replace("\u2126", "Ω")
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.upper()


# --------------------------------------------------------------------------
# 参数值解析（立创给的是人话："10kΩ" / "100mW" / "±1%" / "-20%~+80%"）
# --------------------------------------------------------------------------

_MULT = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

_RE_NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)")


def _numbers(text) -> list[float]:
    """把 "4.5V~16V" 拆成 [4.5, 16]；"100mW" → [0.1]。"""
    out = []
    for m in _RE_NUM_UNIT.finditer(str(text or "")):
        try:
            # 700mW → 0.7000000000000001 这种浮点残渣会一路带进闸门文字，
            # 统一收一下精度
            out.append(round(float(m.group(1)) * _MULT.get(m.group(2), 1.0), 12))
        except (TypeError, ValueError):
            continue
    return out


def _one(text):
    vals = _numbers(text)
    return vals[0] if vals else None


def _resistance(text):
    # 立创写 "10kΩ" / "0.5Ω" / "1MΩ"；偶见 "4R7"
    s = str(text or "")
    m = re.search(r"(\d+)\s*[Rr]\s*(\d+)", s)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            pass
    return _one(s)


def _capacitance(text):
    return _one(text)


def _inductance(text):
    return _one(text)


def _power(text):
    s = str(text or "")
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*[wW]", s)
    if m:
        try:
            return float(m.group(1)) / float(m.group(2))
        except ZeroDivisionError:
            return None
    return _one(s)


def _voltage(text):
    # 量程写成 "4.5V~16V" 时取上限：耐压按最保守的那个算
    vals = _numbers(text)
    if not vals:
        return None
    return max(vals) if ("~" in str(text) or "-" in str(text)) else vals[0]


def _tolerance(text):
    vals = _numbers(text)
    return max(vals) if vals else None


def _pins(text):
    m = re.search(r"(\d+)\s*[Pp]\b", str(text or ""))
    if m:
        return int(m.group(1))
    v = _one(text)
    return int(v) if v else None


def _rows(text):
    """「双排 / 1x2P」→ 排数。排针是单排还是双排，买错就插不进焊盘。"""
    s = str(text or "").strip()
    m = re.search(r"(\d+)\s*[x×*]\s*(\d+)", s)
    if m:
        return int(m.group(1))
    if "双排" in s or re.search(r"(?<!\d)2\s*排", s):
        return 2
    if "三排" in s:
        return 3
    if "单排" in s or re.search(r"(?<!\d)1\s*排", s):
        return 1
    return None


def _mm(text):
    """「2.54mm」→ 2.54。

    这里的 m 是毫米单位本身，不是「毫」——按 `_one` 走会先把 "2.54m" 当 0.00254，
    脚距闸门拿它去比 2.54/5.08 就永远对不上（Type-C、排针整行会莫名被拒）。
    """
    m = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    return float(m.group(1)) if m else None


def _amperes(text):
    """「500mA」→ 0.5；「8A」→ 8。电流的 m 是毫，_MULT 已按 1e-3 处理。"""
    return _one(text)


# 立创参数名 → 我们引擎的参数键。用 startswith 匹配，因为立创会带括号后缀
# （"耗散功率(Pd)"、"直流反向耐压（Vr）"）。
_PARAM_RULES = (
    ("阻值", "resistance_ohm", _resistance),
    ("容值", "capacitance_f", _capacitance),
    ("感值", "inductance_h", _inductance),
    ("电感量", "inductance_h", _inductance),
    ("额定电压", "voltage_v", _voltage),
    ("最大工作电压", "voltage_v", _voltage),
    ("耐压", "voltage_v", _voltage),
    ("直流反向耐压", "voltage_v", _voltage),
    ("精度", "tolerance_pct", _tolerance),
    ("容差", "tolerance_pct", _tolerance),
    ("功率", "power_w", _power),
    ("耗散功率", "power_w", _power),
    ("触点数量", "pins", _pins),
    ("引脚数", "pins", _pins),
    ("总PIN数", "pins", _pins),
    ("脚间距", "pitch_mm", _mm),
    # 排针必须区分单排/双排：BOM 写 2x20P，按「单排 40P」买回来是两排各 40 针，
    # 尺寸差一倍，插不进去。
    ("排数", "rows", _rows),
    ("插针结构", "rows", _rows),
    # 连接器/排针写的是「间距 = 2.54mm」，跟「脚间距」是两套叫法；
    # 少了这一条，排针的搜索词就拼不出「2.54mm 排针 40P」，只能拿立创料号去搜
    # ——淘宝卖排针的标题里没有料号，一条结果都不会有。
    ("间距", "pitch_mm", _mm),
    ("额定电流", "current_a", _amperes),
)


def extract_params(attrs: dict) -> dict:
    """立创的中文参数表 → 引擎参数（只取认识的键，绝不臆造）。"""
    out: dict = {}
    for name, value in (attrs or {}).items():
        key_name = str(name or "")
        for prefix, key, conv in _PARAM_RULES:
            if not key_name.startswith(prefix):
                continue
            if key in out:  # 先到先得，和规则表顺序一致
                break
            val = conv(value)
            if val is not None:
                out[key] = val
            break
    return out


def mount_from_attrs(attrs: dict) -> str:
    """安装方式 → 插件 / 贴片（空串表示立创没写）。"""
    for key in ("安装方式", "引脚样式"):
        v = str((attrs or {}).get(key) or "")
        if "贴片" in v or "SMD" in v.upper():
            return "贴片"
        if "插" in v:
            return "插件"
    return ""


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------


def _runs_dir() -> Path:
    return Path(os.environ.get("TAOBAO_RUNS_DIR") or (BASE / "runs")).resolve()


def _cache_path() -> Path:
    return _runs_dir() / "lcsc-cache.json"


_cache_lock = threading.Lock()
_cache_mem: dict | None = None


def _load_cache() -> dict:
    global _cache_mem
    with _cache_lock:
        if _cache_mem is not None:
            return _cache_mem
        data = {}
        try:
            raw = json.loads(_cache_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                data = raw
        except Exception:  # noqa: BLE001
            data = {}
        data.setdefault("version", CACHE_VERSION)
        data.setdefault("items", {})
        data.setdefault("models", {})
        _cache_mem = data
        return data


def _save_cache() -> None:
    data = _load_cache()
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _cache_get(bucket: str, key: str, ttl: int):
    data = _load_cache()
    row = (data.get(bucket) or {}).get(key)
    if not isinstance(row, dict):
        return None, False
    try:
        age = time.time() - float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return None, False
    if age > ttl:
        return None, False
    return row.get("data"), True


def _cache_put(bucket: str, key: str, value) -> None:
    data = _load_cache()
    with _cache_lock:
        (data.get(bucket) or {}).setdefault(key, {})
        data[bucket][key] = {"ts": time.time(), "data": value}
    _save_cache()


def clear_cache() -> None:
    """清空内存与磁盘缓存（测试/排障用）。"""
    global _cache_mem
    with _cache_lock:
        _cache_mem = None
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict | None:
    data = None
    headers = {"Accept": "application/json", "User-Agent": _UA}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        out = json.loads(body)
        return out if isinstance(out, dict) else None
    except Exception:  # noqa: BLE001  网络/解析失败一律当作「没查到」
        return None


def _pack(raw: dict) -> dict:
    """立创详情 JSON 的 result → 我们内部的精简记录。"""
    attrs = {}
    for p in raw.get("paramVOList") or []:
        name = str(p.get("paramName") or "").strip()
        val = str(p.get("paramValue") or "").strip()
        if name and val and val != "-":
            attrs[name] = val
    cats = [c.get("catalogNameEn") for c in (raw.get("parentCatalogList") or []) if c.get("catalogNameEn")]
    return {
        "code": normalize_code(raw.get("productCode")) or str(raw.get("productCode") or ""),
        "model": str(raw.get("productModel") or "").strip(),
        "brand": str(raw.get("brandNameEn") or "").strip(),
        "package": str(raw.get("encapStandard") or "").strip(),
        "desc": str(raw.get("productIntroEn") or "").strip(),
        "catalog": str(raw.get("catalogName") or "").strip(),
        "catalog_path": cats,
        "attrs": attrs,
        "params": extract_params(attrs),
        "mount": mount_from_attrs(attrs),
        "stock": raw.get("stockNumber"),
        "unit": str(raw.get("productUnit") or "").strip(),
        "packet": str(raw.get("minPacketUnit") or "").strip(),
        "datasheet": str(raw.get("pdfUrl") or "").strip(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "lcsc",
        # 这条记录是怎么来的：编号解析（唯一标识，可当权威）还是同型号反查（只是参考）。
        # 以前只有 resolve_model 才写这个字段，导致「按编号查到」的行被误判成
        # 「没有权威身份」，采购时不敢用型号锚定。
        "matched_by": "code",
    }


def resolve(code, *, fetch=None, timeout: float = 10.0, use_cache: bool = True) -> dict | None:
    """立创编号 → 权威规格。失败返回 None，绝不抛异常。"""
    c = normalize_code(code)
    if not c:
        return None
    if use_cache:
        hit, ok = _cache_get("items", c, _TTL_HIT)
        if ok:
            return hit
    http = fetch or _http_json
    try:
        raw = http(_DETAIL_API.format(code=c), None, timeout)
    except TypeError:  # 注入的 fetch 只接一个参数
        raw = http(_DETAIL_API.format(code=c))
    except Exception:  # noqa: BLE001
        raw = None
    result = (raw or {}).get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict) or not result.get("productModel"):
        if use_cache:
            _cache_put("items", c, None)  # 短期负缓存，避免反复打空
        return None
    rec = _pack(result)
    if use_cache:
        _cache_put("items", c, rec)
    return rec


def resolve_model(model, *, fetch=None, timeout: float = 10.0, use_cache: bool = True) -> dict | None:
    """完整型号 → 权威规格。**只认 productModel 完全相等**，模糊命中一律放弃。

    无立创编号的行靠它判断「BOM 写的型号到底是什么规格」，
    用来发现「型号列与名称列自相矛盾」，不是用来替用户选料的。
    """
    key = normalize_model(model)
    if not key or len(key) < 3:
        return None
    if use_cache:
        hit, ok = _cache_get("models", key, _TTL_HIT)
        if ok:
            return hit
    http = fetch or _http_json
    payload = {"keyword": str(model).strip(), "currentPage": 1, "pageSize": 10}
    try:
        raw = http(_LIST_API, payload, timeout)
    except TypeError:
        raw = http(_LIST_API, payload)
    except Exception:  # noqa: BLE001
        raw = None
    result = (raw or {}).get("result") if isinstance(raw, dict) else None
    items = (result or {}).get("dataList") or []
    code = ""
    for it in items:
        if normalize_model(it.get("productModel")) == key:
            code = normalize_code(it.get("productCode"))
            break
    if not code:
        if use_cache:
            _cache_put("models", key, None)
        return None
    rec = resolve(code, fetch=fetch, timeout=timeout, use_cache=use_cache)
    if rec:
        rec = dict(rec, matched_by="model")
        if use_cache:
            _cache_put("models", key, rec)
    return rec


# --------------------------------------------------------------------------
# 批量富化：给 BOM 每行挂上 lcsc_info
# --------------------------------------------------------------------------


def _online() -> bool:
    return str(os.environ.get("LCSC_OFFLINE") or "").strip().lower() not in ("1", "true", "yes", "on")


def enrich_rows(
    rows: list,
    *,
    allow_model: bool = True,
    fetch=None,
    timeout: float = 10.0,
    workers: int = 6,
    budget: float = 30.0,
    use_cache: bool = True,
) -> dict:
    """给每行挂 `row["lcsc_info"]`（查得到才有），返回统计。

    两种来源：
      - 行上有立创编号（`row["lcsc"]`）→ 编号解析，证据最强；
      - 没有编号但有完整型号 → 型号精确匹配，用来核查 BOM 是否自相矛盾。

    整个过程有总预算（budget 秒），超时直接放弃剩余行 —— 解析 BOM 不能被
    一次慢网络拖住十分钟。离线（LCSC_OFFLINE=1）时只读缓存。
    """
    rows = rows or []
    stats = {"resolved": 0, "by_code": 0, "by_model": 0, "skipped": [], "failed": [], "online": _online()}
    if not rows:
        return stats

    by_code: dict[str, list] = {}
    by_model: dict[str, list] = {}
    model_text: dict[str, str] = {}  # 归一化键 → 原始 MPN（检索必须用原文，不能发「欧」）
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = code_of_row(row)
        if code:
            by_code.setdefault(code, []).append(row)
        elif allow_model and row.get("mpn"):
            key = normalize_model(row.get("mpn"))
            if key and len(key) >= 3:
                by_model.setdefault(key, []).append(row)
                model_text.setdefault(key, str(row.get("mpn")).strip())

    jobs: list[tuple[str, str]] = [("code", c) for c in by_code]
    if allow_model:
        jobs += [("model", m) for m in by_model]

    if not jobs:
        return stats

    deadline = time.time() + max(1.0, budget)

    def work(job):
        kind, key = job
        if not _online() and use_cache:
            # 离线：只允许读已有缓存
            if kind == "code":
                hit, ok = _cache_get("items", key, _TTL_HIT)
                return job, (hit if ok else None)
            hit, ok = _cache_get("models", key, _TTL_HIT)
            return job, (hit if ok else None)
        if time.time() > deadline:
            return job, None
        if kind == "code":
            return job, resolve(key, fetch=fetch, timeout=timeout, use_cache=use_cache)
        return job, resolve_model(
            model_text.get(key) or key, fetch=fetch, timeout=timeout, use_cache=use_cache
        )

    results = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            for job, rec in pool.map(work, jobs):
                results[job] = rec
    except Exception:  # noqa: BLE001  线程池异常不该影响整表解析
        pass

    for code, group in by_code.items():
        rec = results.get(("code", code))
        if rec:
            stats["resolved"] += len(group)
            stats["by_code"] += len(group)
            for row in group:
                row["lcsc_info"] = rec
        else:
            stats["failed"].append(code)

    for key, group in by_model.items():
        rec = results.get(("model", key))
        if rec:
            stats["resolved"] += len(group)
            stats["by_model"] += len(group)
            for row in group:
                row["lcsc_info"] = dict(rec)
        else:
            stats["skipped"].append(str((group[0] or {}).get("mpn") or key))

    return stats


def summary_text(stats: dict) -> str:
    """一行中文说明，给页面顶部提示用。"""
    stats = stats or {}
    n_code = stats.get("by_code") or 0
    n_model = stats.get("by_model") or 0
    parts = []
    if n_code:
        parts.append(f"{n_code} 行按立创编号核准")
    if n_model:
        parts.append(f"{n_model} 行按完整型号核准")
    if not parts:
        return "未启用立创数据" if not stats.get("online") else "立创未查到任何匹配"
    tail = ""
    if stats.get("failed"):
        tail = f"；{len(stats['failed'])} 个编号立创查不到"
    return "，".join(parts) + tail
