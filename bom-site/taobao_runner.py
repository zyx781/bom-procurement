#!/usr/bin/env python3
"""淘宝执行层：管理 Taobao-Search-Skill v4 的常驻 session 子进程。

协议：stdin 逐行 JSON，stdout 返回 JSON 信封（缩进格式，因此按 JSON 流解析）。
不做登录、不碰验证码、不结算付款；只执行 search / open / select-sku / cart-add / cart-view。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

# 采购规则 / SKU 决策（纯函数簇，见 rules.py）。
# 这里把规则层入口全部 re-export：测试、脚本、服务端都按 `tr.suggest_sku` 这样用，
# 拆分模块不应该改动调用方看到的接口。
from rules import (
    _FREQ_SCALE,
    _VALUE_PATTERNS,
    _bom_cap_family,
    _candidate_veto,
    _conn_family,
    _dielectric_of,
    _is_standard_value,
    _line_blob,
    _line_rows,
    _material_conflict,
    _procurement_basis,
    _required_spec_kinds,
    _required_value_kinds,
    _review_reason,
    _score_sku_value,
    _sku_has_spec,
    _sku_model_conflict,
    _sku_resistance,
    _sku_veto,
    _spec_kinds,
    _title_has_mpn,
    _title_value_kinds,
    _title_with_spec,
    _under_power,
    condense_sku_groups,
    groups_without_selection,
    suggest_sku,
)

__all__ = [
    "_bom_cap_family",
    "_conn_family",
    "_dielectric_of",
    "_is_standard_value",
    "_line_blob",
    "_line_rows",
    "_score_sku_value",
    "_sku_has_spec",
    "_sku_resistance",
    "_sku_veto",
]

import diagnostics
import engine
import errors

try:  # 窗口/焦点控制只对本地 Edge 有意义，缺依赖时降级为空实现
    import focus_guard as _focus_guard
    import window_pos as _window_pos
except Exception:  # noqa: BLE001  pragma: no cover
    _focus_guard = None
    _window_pos = None

BASE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("TAOBAO_SKILL_REPO") or (BASE.parent / "Taobao-Search-Skill")).resolve()
ARTIFACTS = (REPO / "scripts" / ".cache" / "taobao-search-skill" / "artifacts").resolve()
# 测试/演练可通过 TAOBAO_RUNS_DIR 把批量进度隔离到临时目录，
# 否则集成测试会把用户真实的 batch-progress.json 覆盖成 1 行假数据。
RUNS = Path(os.environ.get("TAOBAO_RUNS_DIR") or (BASE / "runs")).resolve()
RUNS.mkdir(parents=True, exist_ok=True)
# 本机配置不进仓库：调过的节奏、单价上限、CDP 端口都是「这台机器的事」。
# 仓库里只放 settings.example.json，新人 clone 下来直接可用；他改自己的
# settings.json 也不会把本机参数带进下一次提交。
SETTINGS_FILE = BASE / "settings.json"
SETTINGS_EXAMPLE_FILE = BASE / "settings.example.json"


PROGRESS_FILE = RUNS / "batch-progress.json"


def _log(msg: str) -> None:
    """统一日志出口。批量跑是父进程读子进程输出，必须 flush，否则日志会攒在缓冲里。"""
    print(msg, flush=True)


def verification_next_step(added: bool, auto_retries: int, human_retries: int) -> str:
    """撞验证之后干什么（2026-11-08 规则）—— 纯函数，便于单测。

    "auto"  : 执行层会先刷新自恢复，这里立刻重试同一行，不惊动人；
    "human" : 自动重试用完（= 刷新后仍然出不来商品），等人工验证；
    "stop"  : 人工也没解开 / 已经点过加购（不能重跑，否则重复下单），停下。

    `added=True` 一律 stop：购物车可能已经进了，重跑会重复下单。
    """
    if added:
        return "stop"
    if auto_retries < VERIFY_AUTO_RETRY:
        return "auto"
    if human_retries < VERIFY_HUMAN_RETRY:
        return "human"
    return "stop"


def load_batch_progress() -> dict:
    """上一次批量的进度，按 line_no 记 —— 用于「暂停后下次接着跑」。"""
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_batch_progress(data: dict) -> None:
    """原子写：先写 .tmp 再 replace，避免断电/重启留下半个 JSON。"""
    try:
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROGRESS_FILE)
    except Exception:
        pass


def _progress_archive_file() -> Path:
    """上一批进度的底档路径。

    跟着 PROGRESS_FILE 走（而不是固定写 RUNS）：测试把 PROGRESS_FILE 指到临时目录时，
    底档也自动落在临时目录，不会污染真实的 runs/。
    """
    pf = PROGRESS_FILE
    return pf.with_name(f"{pf.stem}-prev{pf.suffix}")


def _sku_obs_file() -> Path:
    """真机见过的 SKU 档位流水（JSONL，一行一家店）。

    跟着 PROGRESS_FILE 走，测试/演练不会写进真实 runs/。刻意用 append 追加，
    每一行自带时间与行号，重复跑同一行只是多几条记录 —— 回放集按「最新一条」
    取即可，不做去重逻辑，省掉一整类「合并时把好数据覆盖掉」的麻烦。
    """
    pf = PROGRESS_FILE
    return pf.with_name("sku-observations.jsonl")


def record_sku_observation(rec: dict) -> None:
    """把一次开页看到的 SKU 档位追加到流水里（失败不影响主流程）。"""
    try:
        path = _sku_obs_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        _log(f"[sku] 记录 SKU 档位失败（忽略）：{exc}")


def load_archived_progress() -> dict:
    try:
        data = json.loads(_progress_archive_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def clear_batch_progress() -> None:
    """清进度文件。清之前先留一份底档。

    为什么留底：购物车「删除上一批」要靠进度里的 candidate.url / 标题去精准定位，
    重跑或重置把进度清掉之后，那些商品就再也认不出来了 —— 而这时恰恰最需要清理，
    因为重跑会重复加购。
    """
    try:
        data = load_batch_progress()
        if data.get("lines"):
            tmp = _progress_archive_file().with_suffix(PROGRESS_FILE.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(_progress_archive_file())
    except Exception:
        pass
    try:
        PROGRESS_FILE.unlink(missing_ok=True)
    except Exception:
        pass


BATCH_LINES_FILE = RUNS / "batch-lines.json"


def save_batch_lines(lines: list) -> None:
    """把这一批要跑的行存盘。

    只存进度是不够的：服务重启后行数据（query_plan / params / qty）都在前端内存里，
    没有它就没法「接着跑」，只能重新上传 BOM。落一份到磁盘，重启后可以直接续跑。
    """
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        tmp = BATCH_LINES_FILE.with_suffix(".json.tmp")
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "fingerprint": batch_fingerprint(lines),
            "lines": lines,
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(BATCH_LINES_FILE)
    except Exception:
        pass


def load_batch_lines() -> list:
    """读回上一次存盘的行；没有或损坏时返回空列表。"""
    try:
        data = json.loads(BATCH_LINES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    return lines if isinstance(lines, list) else []


def batch_fingerprint(lines: list) -> str:
    """这一批 BOM 的身份指纹（16 位十六进制）。

    续跑只能复用「同一份 BOM」的进度。踩过的坑：换了一份同样是 19 行的新表，
    done_map 只按 line_no 匹配，把上一批 19 条结果整套套过来 —— 一行都不跑，
    收尾对账又拿旧候选商品去购物车里找，19 行全降级成「待核对」。
    指纹只取 BOM 身份字段，不含候选/价格/搜索词（那些会随引擎版本变化，
    同表重跑却变了指纹会导致重复加购）。

    2026-11-12：`decision.headline` 也一并剔掉了。那是引擎算出来的展示文案，
    一改选型规则（比如接入立创核准、改了摘要格式）它就变，用户没换表却被告知
    「BOM 已更换，旧进度作废」—— 和本函数「只认 BOM 身份」的初衷正相反。
    BOM 换没换，看型号/名称/封装/参数/数量这几列就够了。

    2026-11-17：连 `params` 也不能用**解析后**的字典。那是引擎派生值：同一条
    「1kΩ」以前解析成 int 1000、现在 float 1000.0；晶振补了 frequency 键 ——
    指纹就变了，29 行进度被误判「换过 BOM」。改用原始参数列（row.params）。
    """
    parts = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        raw_params = (ln.get("row") or {}).get("params")
        if raw_params is None:
            # 兼容没有 row 的调用方（测试/旧数据）：退回原始字符串列，别用解析字典
            raw_params = ln.get("params_text") or ""
        parts.append(
            "\x1f".join(
                [
                    str(ln.get("line_no") or ""),
                    str(ln.get("mpn") or "").strip(),
                    str(ln.get("desc") or "").strip(),
                    str(ln.get("package") or "").strip(),
                    str(raw_params or "").strip(),
                    str(ln.get("qty") or ""),
                ]
            )
        )
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]


def load_batch_lines_meta() -> dict:
    """上一次「开跑」时存下的 BOM 指纹（只读元信息，不返回整份行数据）。"""
    try:
        data = json.loads(BATCH_LINES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "saved_at": data.get("saved_at"),
        "fingerprint": str(data.get("fingerprint") or ""),
    }


def progress_belongs_to(prior: dict, lines: list) -> tuple[bool, str]:
    """这份进度是不是当前 BOM 跑出来的？返回 (是否匹配, 判定依据)。

    老版本进度文件里没有 fingerprint，退化成按 line_no 比对 headline：
    只要有一条对不上就说明换过 BOM（同一份表的 headline 不会变）。
    """
    prior = prior if isinstance(prior, dict) else {}
    rows = prior.get("lines") or {}
    if not rows:
        return True, "无进度"
    fp = batch_fingerprint(lines)
    prior_fp = prior.get("fingerprint") or ""
    if prior_fp:
        return (prior_fp == fp), "fingerprint"
    compared = 0
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        rec = rows.get(str(ln.get("line_no"))) or {}
        old_head = str(rec.get("headline") or "").strip()
        new_head = str((ln.get("decision") or {}).get("headline") or "").strip()
        if old_head and new_head:
            compared += 1
            if old_head != new_head:
                return False, "headline"
    return True, ("legacy-headline" if compared else "legacy-unknown")


def should_clear_stale_progress(batch_state: dict, ran: int) -> bool:
    """整批一行没跑、且所有「续跑跳过」的行都没在购物车里找到 → 清掉错配进度。

    典型场景就是换 BOM 或购物车被清空过：留着这份进度，下次点「继续加购」
    还是整批空跑，用户会反复看到「全识别成功、一件没加」。
    """
    state = batch_state or {}
    rec = state.get("reconcile") or {}
    downgraded = rec.get("downgraded") or []
    resumed_n = sum(1 for r in (state.get("results") or []) if r.get("resumed"))
    return bool(ran == 0 and resumed_n and downgraded and len(downgraded) >= resumed_n)


def _line_skip_reason(line: dict) -> str:
    """跑批前判断这一行为什么要直接跳过（不进搜索）。返回原因文案，空串 = 正常跑。

    两类跳过：
    - dont_buy：网页上人工标记「这行不买了」（结构件/自制件/自己有库存）。
      不买是采购决策不是解析失败，记 skipped、写明原因，进度条不欠账。
    - 残缺行：型号/名称/参数都空，没有搜索词可搜，跑下去只会在 search() 里
      抛 NO_QUERY，白等一轮再记一条「失败」。
    """
    if line.get("dont_buy"):
        return "人工标记不买"
    if line.get("query_plan") or (line.get("decision") or {}).get("search_queries"):
        return ""
    return (
        "；".join(line.get("blocking") or [])
        or "该行没有可用搜索词（型号/名称/参数都为空）"
    )


def _result_progress_payload(r: dict) -> dict:
    """一条执行结果 → 进度文件里的一行（批量、单行共用）。

    放在模块级而不是 SessionRunner 方法：测试里的假 runner 只绑定个别方法，
    用 self 调用会 AttributeError（实测踩到）。
    """
    return {
        "status": r.get("status"),
        "headline": r.get("headline"),
        "message": r.get("message"),
        "qty": r.get("qty"),
        "price": r.get("final_price") if r.get("final_price") is not None else r.get("price"),
        "final_price": r.get("final_price"),
        "cost": r.get("cost"),
        "unit_price": r.get("unit_price"),
        "pack_size": r.get("pack_size"),
        "actual_pcs": r.get("actual_pcs"),
        # 选了哪个 SKU、靠什么信号确认加购：续跑/导出都要用，缺了就查不出买了什么规格
        "sku": r.get("sku") or [],
        "cart_verified_via": r.get("cart_verified_via"),
        "query": r.get("query"),
        # uncertain 行要留着候选和截图，续跑跳过时还能让人核对
        "candidate": r.get("candidate"),
        "screenshot": r.get("screenshot"),
        # 耗时埋点：只用于复盘速度，不影响续跑判定
        "total_ms": r.get("total_ms"),
        "search_ms": r.get("search_ms"),
        "open_ms": r.get("open_ms"),
        "select_ms": r.get("select_ms"),
        "add_ms": r.get("add_ms"),
        "attempts_n": len(r.get("attempts") or []),
        # 错误分类（errors.py）：续跑/导出时要能看出「重试有没有意义、要不要人工」
        "error_code": r.get("error_code"),
        "error_kind": r.get("error_kind"),
        "error_retryable": r.get("error_retryable"),
        "error_needs_human": r.get("error_needs_human"),
        "error_stage": r.get("error_stage"),
    }


def batch_progress_summary(active_fingerprint: str = "") -> dict:
    """给前端/接口用的进度摘要。

    传入 active_fingerprint（当前这份 BOM）时，若进度属于另一份 BOM，
    对外表现成「没有进度」：否则前端会弹「上次已完成 N 行，接着跑」，
    用户以为跑过了，实际一行都不会执行。
    """
    data = load_batch_progress()
    rows = data.get("lines") or {}
    ok = [k for k, v in rows.items() if (v or {}).get("status") == "ok"]
    bad = [k for k, v in rows.items() if (v or {}).get("status") in ("failed", "uncertain")]
    fp = str(data.get("fingerprint") or "")
    stale = bool(active_fingerprint and fp and fp != active_fingerprint)
    if stale:
        ok, bad = [], []
    return {
        "has_progress": bool(rows) and not stale,
        "updated_at": data.get("updated_at"),
        "total": data.get("total") or 0,
        "ok_count": len(ok),
        "problem_count": len(bad),
        "ok_line_nos": sorted(int(k) for k in ok if str(k).isdigit()),
        # 重跑按钮要知道「哪些行是问题行」（失败 + 待核对），否则只能整批重来
        "problem_line_nos": sorted(int(k) for k in bad if str(k).isdigit()),
        "fingerprint": fp,
        "stale": stale,
    }


# 当前界面上正在看的那份 BOM（由 /api/analyze 写入）。
# 持久化到 runs/，服务重启后进度摘要也不会误报「上次已完成 N 行」。
ACTIVE_BOM_FILE = RUNS / "active-bom.json"
ACTIVE_BOM_FINGERPRINT = ""
# 上一次分析的完整结果。
# 前端原来只把结果放在 sessionStorage：刷新能恢复，但**新开标签页/浏览器重启**
# 就丢了 —— 执行区整块 `.hidden`，页面回到空白首屏，用户以为「跑批没动静」。
# 落一份到磁盘，任何标签页打开都能接上。
LAST_ANALYSIS_FILE = RUNS / "last-analysis.json"


def save_last_analysis(result: dict, rows: list | None = None) -> None:
    """存一份上次分析结果（前端刷新/新标签页恢复用）。失败不影响主流程。"""
    try:
        if not isinstance(result, dict) or not result.get("lines"):
            return
        RUNS.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "result": result,
            # 原始行也带上：换套数（1 套 → 10 套）要在前端重新分析，
            # 只恢复结果没恢复 rows，那个功能就成了死按钮。
            "rows": rows if isinstance(rows, list) else [],
        }
        tmp = LAST_ANALYSIS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LAST_ANALYSIS_FILE)
    except Exception:  # noqa: BLE001
        pass


def load_last_analysis() -> dict:
    """读回上次分析结果；没有/损坏/和当前 BOM 对不上时返回空。"""
    try:
        data = json.loads(LAST_ANALYSIS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    result = (data or {}).get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("lines"), list):
        return {}
    # 和当前生效的 BOM 指纹不一致，说明是上一张表的结果，宁可让用户重传
    fp_saved = str(result.get("bom_fingerprint") or "")
    fp_now = active_bom_fingerprint()
    if fp_saved and fp_now and fp_saved != fp_now:
        return {}
    return {
        "saved_at": data.get("saved_at"),
        "result": result,
        "rows": data.get("rows") if isinstance(data.get("rows"), list) else [],
    }


def clear_last_analysis() -> bool:
    """忘掉「上次分析」：删落盘结果和当前 BOM 指纹。

    只影响「打开页面时接不接上次那摊活」，不碰购物车、不碰跑批进度文件。
    """
    global ACTIVE_BOM_FINGERPRINT
    removed = False
    for f in (LAST_ANALYSIS_FILE, ACTIVE_BOM_FILE):
        try:
            f.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    ACTIVE_BOM_FINGERPRINT = ""
    return removed


# 人工在网页上改过的行（确认放行 / 改搜索词 / 标记不买）要落盘的字段白名单
_LINE_PATCH_KEYS = ("query_plan", "status", "blocking", "missing", "user_approved", "dont_buy")


def update_saved_line(index: int, patch: dict) -> dict:
    """把网页上的人工修改写进落盘的两份行数据（last-analysis / batch-lines）。

    前端内存是当前会话的事实来源；但刷新/新标签页从 last-analysis.json 恢复，
    服务重启后「接着跑」从 batch-lines.json 恢复 —— 只改内存的话，两处都会
    把确认和修改吞掉，用户明明放行了那行却还弹「待确认」。
    只按 index + line_no 双重定位，只覆盖白名单字段；两份文件有一份不在
    （还没开跑过）就跳过那份，不算失败。
    """
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch 不能为空")
    clean = {k: patch[k] for k in _LINE_PATCH_KEYS if k in patch}
    if not clean:
        raise ValueError(f"patch 里没有可更新的字段（允许：{', '.join(_LINE_PATCH_KEYS)}）")
    if "query_plan" in clean:
        qp = clean["query_plan"]
        if not isinstance(qp, list) or not all(isinstance(x, str) for x in qp):
            raise ValueError("query_plan 必须是字符串数组")
        qp = [re.sub(r"\s+", " ", x).strip() for x in qp]
        qp = [x for x in qp if x][:4]
        if not qp:
            raise ValueError("搜索词不能全部为空（清空等于让这一行没法跑）")
        clean["query_plan"] = qp
    if "status" in clean and clean["status"] not in ("ready", "needs_user"):
        raise ValueError("status 只能是 ready / needs_user")
    for k in ("blocking", "missing"):
        if k in clean and not isinstance(clean[k], list):
            raise ValueError(f"{k} 必须是数组")

    want_no = patch.get("line_no")
    applied = {"last_analysis": False, "batch_lines": False}

    def _apply(lines: list) -> bool:
        if not isinstance(lines, list) or not (0 <= index < len(lines)):
            return False
        ln = lines[index]
        if not isinstance(ln, dict):
            return False
        # 行号对不上 = 前端和服务端的顺序已经错位，宁可不动也不能改错行
        if want_no is not None and str(want_no) != str(ln.get("line_no")):
            return False
        if "query_plan" in clean:
            ln["query_plan"] = list(clean["query_plan"])
            d = ln.get("decision")
            if isinstance(d, dict):
                d["search_queries"] = list(clean["query_plan"])
        for k in ("status", "blocking", "missing", "user_approved", "dont_buy"):
            if k in clean:
                ln[k] = list(clean[k]) if isinstance(clean[k], list) else clean[k]
        return True

    def _rewrite(path, lines_key):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(data, dict):
            return False
        target = data.get(lines_key)
        if lines_key == "result" and isinstance(target, dict):
            target = target.get("lines")
        if not _apply(target if isinstance(target, list) else []):
            return False
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:  # noqa: BLE001
            return False

    applied["last_analysis"] = _rewrite(LAST_ANALYSIS_FILE, "result")
    applied["batch_lines"] = _rewrite(BATCH_LINES_FILE, "lines")
    ok = applied["last_analysis"] or applied["batch_lines"]
    return {"ok": ok, "applied": applied, "patch": clean}


def set_active_bom(lines: list) -> str:
    global ACTIVE_BOM_FINGERPRINT
    ACTIVE_BOM_FINGERPRINT = batch_fingerprint(lines)
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        ACTIVE_BOM_FILE.write_text(
            json.dumps(
                {
                    "fingerprint": ACTIVE_BOM_FINGERPRINT,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return ACTIVE_BOM_FINGERPRINT


def active_bom_fingerprint() -> str:
    """当前界面在看的 BOM 指纹。

    正常流程里内存和磁盘是同一份（`set_active_bom` 同时写两边），谁先谁后无所谓。
    但磁盘可能被外部改写（比如打真实服务的 HTTP 测试调了 /api/analyze），
    这时以磁盘为准：内存那份已经失真，而磁盘是下一次重启会读到的值。
    """
    try:
        on_disk = str(json.loads(ACTIVE_BOM_FILE.read_text(encoding="utf-8")).get("fingerprint") or "")
    except Exception:
        on_disk = ""
    if on_disk and ACTIVE_BOM_FINGERPRINT and on_disk != ACTIVE_BOM_FINGERPRINT:
        return on_disk
    return ACTIVE_BOM_FINGERPRINT or on_disk


_RE_ITEM_ID = re.compile(r"[?&](?:id|itemId)=(\d+)")


def _item_id_of(url: str) -> str:
    """从商品链接里取商品 id（淘宝 item.htm?id= / 天猫 detail.tmall.com）。"""
    m = _RE_ITEM_ID.search(url or "")
    return m.group(1) if m else ""


SHOT_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def batch_screenshot_names(prog: dict | None = None) -> set[str]:
    """进度里点名的截图文件名 —— 这一批「买了什么」的证据图。

    这些图要留到下一次点「删上一次加购」时核对，所以清理时按名单豁免。
    进度被清过（重跑 / 重置）时退到底档，理由同 batch_removal_targets。
    """
    data = prog if isinstance(prog, dict) else load_batch_progress()
    if not (data.get("lines") or {}):
        data = load_archived_progress()
    out: set[str] = set()
    for rec in (data.get("lines") or {}).values():
        if not isinstance(rec, dict):
            continue
        val = rec.get("screenshot")
        if isinstance(val, str) and val.strip():
            out.add(Path(val).name)
    return out


def batch_removal_targets(prog: dict | None = None) -> list[dict]:
    """从上次批量记录里挑出「本工具加进去的商品」，供购物车精准删除。

    只取 status=ok 的行：uncertain 连"到底加没加上"都不确定，不能凭它去删东西。
    2026-11 起加购记录里带 `candidate.url`，用商品 id 精确匹配；
    更早的进度没有 url，退回标题匹配（由执行层做数量守卫兜底）。
    """
    data = prog if isinstance(prog, dict) else load_batch_progress()
    rows = data.get("lines") or {}
    if not rows:
        # 进度刚被「重跑 / 重置」清过，但商品还在购物车里 —— 用底档还能精确删掉。
        data = load_archived_progress()
        rows = data.get("lines") or {}
    keys = sorted(
        rows.keys(),
        key=lambda x: (0, int(x)) if str(x).isdigit() else (1, 0),
    )
    out: list[dict] = []
    for key in keys:
        rec = rows.get(key) or {}
        if (rec.get("status") or "") != "ok":
            continue
        cand = rec.get("candidate") or {}
        title = str(cand.get("title") or "").strip()
        url = str(cand.get("url") or "").strip()
        item_id = _item_id_of(url)
        if not item_id and not title:
            continue
        out.append(
            {
                "item_id": item_id,
                "title": title[:120],
                "qty": int(rec.get("qty") or 0),
                "line_no": key,
            }
        )
    return out


def cart_target_for_line(prog: dict | None, line_no) -> dict:
    """单行删车：从进度里挑出「这一行买的那个商品」，做成执行层的删除目标。

    和 batch_removal_targets 的区别：
    - 只认这一行，一次一件；
    - qty 传 0。执行层的数量守卫是给「整批删上一批」用的（车里件数多于工具
      加购数 = 你自己也加过同款，不删）；行级删车是用户明确点了「从购物车删掉」，
      整行删除才是他要的结果，被守卫拦住反而删不掉（车里 3 件、工具加了 2 件
      是常见情形）。

    返回 {"ok": bool, "target": dict|None, "code": str, "message": str}
    """
    rec = ((prog or {}).get("lines") or {}).get(str(line_no))
    if not isinstance(rec, dict):
        return {
            "ok": False,
            "target": None,
            "code": "NO_LINE_RECORD",
            "message": "这一行还没有加购记录（没跑过或进度已清空），不知道要删哪一件",
        }
    status = str(rec.get("status") or "")
    if status not in ("ok", "uncertain"):
        return {
            "ok": False,
            "target": None,
            "code": "NOT_IN_CART",
            "message": f"这一行当前是「{status or '未跑'}」，购物车里没有它的加购记录",
        }
    cand = rec.get("candidate") or {}
    title = str(cand.get("title") or rec.get("headline") or "").strip()
    item_id = _item_id_of(str(cand.get("url") or ""))
    if not item_id and not title:
        return {
            "ok": False,
            "target": None,
            "code": "NO_TARGET",
            "message": "这一行没有商品标题/链接，认不出购物车里哪件是它",
        }
    return {
        "ok": True,
        "target": {
            "item_id": item_id,
            "title": title[:120],
            "qty": 0,
            "line_no": str(line_no),
        },
        "code": "",
        "message": "",
    }


def mark_line_removed(line_no, message: str) -> int:
    """单行删车后只把这一行改回「待加购」，其它行的结论原样不动。

    status=removed 与整批删车一致：既不是 ok（会被续跑跳过），也不是 failed
    （会被误当成搜索失败），页面显示「已删车」，再点加购会重新买这一行。
    """
    prog = load_batch_progress() or {}
    rec = (prog.get("lines") or {}).get(str(line_no))
    if not isinstance(rec, dict):
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    rec["status"] = "removed"
    rec["removed_at"] = now
    rec["message"] = message
    prog["updated_at"] = now
    save_batch_progress(prog)
    return 1


def _norm_title(text) -> str:
    return "".join(str(text or "").strip().lower().split())


def _title_match(a: str, b: str) -> bool:
    """同一个商品的标题，可能一边被截断到 120 字。前缀互含就算同一件。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 12 and long_.startswith(short)


def invalidate_removed_progress(titles, all_ok: bool = False) -> dict:
    """购物车删完，把被删掉的行从「已加购」改回「待加购」。

    进度文件的唯一含义是「这些行已经在购物车里」。车删空之后它还写着 ok 就是假话：
    点「继续加购」会把这些行全跳过，页面显示已加购 17 行、购物车却一件没有。
    2026-11-16 真机踩到：新 BOM 跑完 17 行（购物车 0 → 17）后点了「删上次加购的」，
    车清回 0，进度却仍是 17 行 ok —— 再点加购会瞬间「跑完」，一件都不加。

    all_ok=True 用于「执行层没回标题、但删掉的件数 ≥ 目标数」的兜底：
    目标全部落删，那这批 ok 行必然都不在车里了。
    """
    wanted = [_norm_title(t) for t in (titles or []) if _norm_title(t)]
    if not wanted and not all_ok:
        return {"updated": 0}
    data = load_batch_progress() or {}
    lines = data.get("lines") or {}
    updated, now = 0, datetime.now().isoformat(timespec="seconds")
    for rec in lines.values():
        rec = rec or {}
        if rec.get("status") != "ok":
            continue
        title = _norm_title((rec.get("candidate") or {}).get("title"))
        if not all_ok and not any(_title_match(title, w) for w in wanted):
            continue
        rec["status"] = "removed"
        rec["removed_at"] = now
        rec["message"] = "已从购物车删除，需要重新加购"
        updated += 1
    if updated:
        data["updated_at"] = now
        save_batch_progress(data)
    return {"updated": updated}


def done_map_from_progress(prior: dict, redo_lines=None) -> dict:
    """续跑时哪些行可以跳过。

    ok = 确认已加购；uncertain = 点过「加入购物车」但页面没给确认信号。
    两者默认都不能重跑：uncertain 重跑会在购物车里多一份（淘宝同款同 SKU 会叠加数量），
    只能跳过、交给人工核对。

    redo_lines 是用户明确点名「这一行我要重跑」的集合 —— 必须是**显式**的，
    不能靠猜。踩过的坑：用户觉得某行买错了想重跑，但 resume 把 ok 行全跳过，
    点一下按钮瞬间「跑完」，实际一行没动，还以为系统跑了。
    """
    force = {str(x) for x in (redo_lines or []) if str(x).strip()}
    out = {}
    for k, v in ((prior or {}).get("lines") or {}).items():
        if str(k) in force:
            continue
        row = v or {}
        if row.get("status") in ("ok", "uncertain"):
            out[str(k)] = row
    return out


def line_has_query(line: dict) -> bool:
    """这行有没有搜索词可搜（型号/名称/参数全空的行跑下去只会白等一轮）。"""
    ln = line or {}
    return bool(ln.get("query_plan") or (ln.get("decision") or {}).get("search_queries"))


_settings_warned = False


def load_settings() -> dict:
    """读本机配置；没有 settings.json 就退回仓库里的 example。

    settings.json 现在被 .gitignore 挡着，纯本地文件。缺它时不能直接返回 {}：
    example 里有 cdp_url / 节奏 / 单价上限，新人 clone 下来必须拿到同一套默认，
    否则「打开调试版 Edge → 启动会话」这条 README 主流程会连不上。
    """
    global _settings_warned
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 —— 手改坏一个逗号不该让服务起不来
        if not _settings_warned:
            _settings_warned = True
            _log(f"[site] settings.json 解析失败（{exc}），改用 settings.example.json")
    try:
        return json.loads(SETTINGS_EXAMPLE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def session_env() -> dict:
    """执行层子进程的环境变量。

    TAOBAO_PROFILE_DIR 用 setdefault：默认登录态落在项目内的
    .browser-profile（跟项目走，删项目就没了，也不会串到系统用户目录），
    但允许外部用环境变量改到别处 —— 以前是硬赋值，设了也不生效。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("TAOBAO_PROFILE_DIR", str(BASE / ".browser-profile"))
    return env


# ---------------- 调试浏览器（CDP）自动就绪 ----------------
# settings.json 里一旦配了 cdp_url，执行层只会“连”那个浏览器，不会自己开。
# 用户点“启动”时如果调试版 Edge 没开着，就会连不上、会话秒退、页面回到“未启动”。
# 所以这里在拉会话之前，先按同一个 profile 把带调试端口的浏览器拉起来。

CDP_PROFILE_DIR = BASE / ".cdp-profile"


def cdp_endpoint_ok(cdp_url: str, timeout: float = 1.2) -> bool:
    if not cdp_url:
        return False
    url = str(cdp_url).rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except Exception:
        return False


def cdp_port(cdp_url: str) -> int:
    m = re.search(r":(\d{2,5})", str(cdp_url or ""))
    return int(m.group(1)) if m else 9222


def find_browser_exe() -> str | None:
    candidates: list[Path] = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def ensure_debug_browser(cdp_url: str, wait_seconds: float = 25.0, log=None) -> None:
    """确保 cdp_url 可连；连不上就按同一 profile 拉起调试版浏览器。"""
    if cdp_endpoint_ok(cdp_url):
        return
    exe = find_browser_exe()
    if not exe:
        raise SessionError(
            "调试浏览器连不上，也没找到 Edge / Chrome。请装 Edge，或删掉 settings.json 里的 cdp_url 改用内置浏览器。",
            "CDP_NO_BROWSER",
        )
    port = cdp_port(cdp_url)
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CDP_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # 上次会话是被 taskkill 收掉的（结束会话就是这么关的），Chromium 会认定
        # 「上次异常退出」并在窗口右下角弹「还原页面」气泡。那个气泡依附主窗口，
        # 主窗口藏到屏幕外时它会被夹回桌面左上角——用户看到的就是屏幕边上露一条。
        "--hide-crash-restore-bubble",
        # 绝不要 --restore-last-session，也绝不要在这里传 https://www.taobao.com：
        # 前者把上次留下的淘宝标签恢复出来，后者又新开一个，两个叠加就是
        # 「一启动出现两个淘宝页」。而且恢复出来的旧标签可能是登录页/验证页/搜索
        # 结果，会干扰新会话挑页。淘宝页统一由 navigate_to_taobao() 在选定 context
        # 里打开，从启动到就绪全程只有一个。
    ]
    kwargs: dict = {"close_fds": True}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不跟着服务进程一起死，也不弹黑框
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"启动调试浏览器失败：{exc}", "CDP_LAUNCH_FAILED") from exc
    if log:
        log(f"[site] 已启动调试浏览器 {exe}（端口 {port}），等待就绪 …")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.4)
        if cdp_endpoint_ok(cdp_url):
            if log:
                log(f"[site] 调试浏览器已就绪 {cdp_url}")
            return
    raise SessionError(
        f"调试浏览器起来了但 {wait_seconds:g} 秒内没监听端口 {port}。"
        f"如果已经有用同一个 profile（{CDP_PROFILE_DIR}）开着的浏览器窗口，"
        "先全部关掉再点一次“启动淘宝服务”。",
        "CDP_NOT_READY",
    )

# 节奏参数：淘宝的验证码是按「短时间内的页面导航次数」触发的，
# 调行间隔只能拖长到某个点，真正管用的是周期性停一段让风控计数回落。
LINE_GAP_S = float(os.environ.get("TAOBAO_LINE_GAP", "1.2"))
COOLDOWN_EVERY = max(1, int(os.environ.get("TAOBAO_COOLDOWN_EVERY", "20")))
COOLDOWN_SEC = int(os.environ.get("TAOBAO_COOLDOWN_SEC", "40"))
# 自适应降温（2026-11-08）：uncertain（点了加购但接口不给回执）是淘宝软限流的
# 早期信号 —— 实测连续出现后 1~2 行内就会升级成硬拦截。所以连续 N 行 uncertain
# 就自动拉长冷却，别等验证弹出来才停。
UNCERTAIN_STREAK_TRIGGER = max(1, int(os.environ.get("TAOBAO_UNC_TRIGGER", "2")))
# 淘宝购物车约 200 行上限，满了之后每行都会失败。开跑前先看一眼件数：
# 到 STOP 直接不跑（省 20 分钟白跑），到 WARN 记日志提醒清车。
CART_LIMIT_WARN = max(1, int(os.environ.get("TAOBAO_CART_WARN", "150")))
CART_LIMIT_STOP = max(1, int(os.environ.get("TAOBAO_CART_STOP", "190")))
UNCERTAIN_COOL_BASE_S = int(os.environ.get("TAOBAO_UNC_COOL", "60"))
UNCERTAIN_COOL_MAX_S = int(os.environ.get("TAOBAO_UNC_COOL_MAX", "300"))

# 一行「已定局」的状态：进度条和 done 计数都只认这几个。
# 单行执行也要按同一口径算，否则会出现「第 1 行已加购」但进度 0/1 未开始。
SETTLED_STATUS = ("ok", "failed", "uncertain", "skipped", "removed")

# 撞验证的处置顺序（2026-11-08 规则变更）：
#   ① 先自己扛 —— 执行层在下一次 search 前会刷新页面自恢复，这里立刻重试同一行；
#   ② 自动重试用完还是被拦（= 刷新后仍然出不来商品），才等人工。
VERIFY_AUTO_RETRY = max(1, int(os.environ.get("TAOBAO_VERIFY_AUTO_RETRY", "2")))
VERIFY_RETRY_GAP_S = float(os.environ.get("TAOBAO_VERIFY_RETRY_GAP", "12"))
# 自动恢复失败后，等人工完成验证（滑块/图片点选）的最长时间。
VERIFY_WAIT_S = int(os.environ.get("TAOBAO_VERIFY_WAIT", "1800"))
# 人工验证解除后，这一行最多再重试几次
VERIFY_HUMAN_RETRY = max(1, int(os.environ.get("TAOBAO_VERIFY_HUMAN_RETRY", "3")))

# I3 容差：淘宝的 0.1% 精密电阻常被一个「系列链接」覆盖多档精度，一刀切否决
# 会把本来能买的行判死；但 BOM 本身是精密档时，±1% 是**另一个等级的料**，
# 装上去分压/采样就偏 —— 所以普通档只降权，精密档才否决。
# 1% 也是常见精密档：BOM 写 ±1%，SKU 明确标 ±5% 就是降一个等级（5 倍误差）。
# 只拦「明确标了更宽」的选项，不要求 SKU 必须标精度 —— 后者会把大量不写精度的行判死。

# 收尾对账的「读车异常」判据：全批都没匹配上，更可能是购物车没读对，
# 不是 N 行同时加购失败。达到下面任一条就只标「未核实」，不逐行降级。
RECONCILE_MIN_CHECK = max(2, int(os.environ.get("TAOBAO_RECONCILE_MIN", "5")))
RECONCILE_MIN_RATIO = float(os.environ.get("TAOBAO_RECONCILE_RATIO", "0.35"))
# 读到的购物车行数 / 页面显示件数低于此值 → 判定「滚动加载不全」，不逐行降级。
RECONCILE_ROW_RATIO = float(os.environ.get("TAOBAO_RECONCILE_ROW_RATIO", "0.85"))

DEFAULT_TIMEOUTS = {
    "ping": 20,
    "search": 90,
    "open": 90,
    "select-sku": 90,
    "cart-add": 150,
    "cart-view": 60,
    "cart-remove": 180,
    "screenshot": 60,
    "dom": 60,
}


# --------------------------------------------------------------------------
# 「购物车里的是不是这件东西」——标题比对
# 搜索结果标题和购物车标题经常被打乱/截断/加后缀，纯字符串相似度分不开
# 「0603 10kΩ」和「0603 1kΩ」（相似度 0.96，却是两种料）。所以先抽规格原子：
# 料号 / 封装 / 阻值 / 容值 / 感值 / 耐压 / 功率 / 电流 / 频率 / 针数 / 间距，
# 单位相同的原子**必须相等**（冲突直接判不同），再退回「封装+值+包含关系」。
# --------------------------------------------------------------------------

_KEY_PUNCT = re.compile(
    r"[\s\-_/\\、,，。;；:：!！?？'\"“”‘’()（）\[\]【】{}<>《》*×~·|+&^%$#@=]+"
)
_PKG_RE = re.compile(
    r"(?<![0-9a-z])(0201|0402|0603|0805|1206|1210|1812|2010|2512|sot-23|sot23|sod-123|sod123|"
    r"sma|smb|smc|smd|dpak|to-252|to-220|sop-8|soic-8|tssop|qfn|dfn|lqfp|vqfn|tdson|"
    r"bga|dip|插件|直插)(?![0-9a-z])"
)
_MPN_RE = re.compile(r"[a-z0-9]{4,}")
# 介质代码（C0G/X7R/NP0…）是材质不是阻值：不剔掉的话 "X7R" 会被当成 7Ω
_DIELECTRIC_RE = re.compile(r"(?:c0g|np0|npo|x5r|x6s|x7r|x8r|y5v|z5u)")
# 这些「像料号」的片段其实是封装或参数值，不能当料号用（否则同封装不同料会误判）
_PKG_TOKENS = {
    "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512",
    "sot23", "sod123", "sma", "smb", "smc", "smd", "dpak", "to252", "to220",
    "sop8", "soic8", "tssop", "qfn", "dfn", "lqfp", "vqfn", "tdson", "bga", "dip",
}
_VALUE_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?(?:p|[pnuµmk]?[fvhar]|[km])$")

# 值 + 单位：先按「数字前缀 + 单位后缀」抓，再换算成基准值（Ω/F/H/V/W/A/Hz/mm/针）
_RES_UNIT = {"": 1.0, "k": 1e3}
_VALUE_SCALE = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
                "k": 1e3, "K": 1e3, "M": 1e6, "g": 1e9}


def _norm_key(text: str) -> str:
    """标题归一：全半角统一、去空白与标点、小写。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _KEY_PUNCT.sub("", s)


def _res_values(base: str) -> set:
    """从（已小写、已去介质的）标题里抓所有阻值：4K7 / 2R2 / R24 / 100R / 10kΩ / 10k。"""
    vals = set()
    # 4K7 / 1R2 / 2M2 这类夹点写法（4.7k、1.2Ω、2.2M）
    for a, unit, b in re.findall(r"(?<![0-9.])(\d+)([rkm])(\d+)(?![0-9])", base):
        vals.add(round(float(f"{a}.{b}") * {"r": 1.0, "k": 1e3, "m": 1e6}[unit], 12))
    # R24 = 0.24Ω
    for v in re.findall(r"(?<![0-9a-z])r(\d{1,2})(?![0-9])", base):
        vals.add(float("0." + v))
    # 100R / 2.2R（R 当 Ω 用，淘宝最常见写法）
    for v in re.findall(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*r(?![0-9a-z])", base):
        vals.add(float(v))
    # 10kΩ / 100Ω / 100 ohm / 10K欧
    for v, unit in re.findall(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*([km]?)\s*(?:ω|Ω|欧|ohm)", base):
        vals.add(round(float(v) * _RES_UNIT.get(unit, 1.0), 12))
    # 裸 10k（标题常写「0603 10K 1%」）
    for v, unit in re.findall(r"(?<![0-9.a-z])(\d+(?:\.\d+)?)\s*([km])(?![a-z0-9])", base):
        vals.add(round(float(v) * _RES_UNIT.get(unit, 1.0), 12))
    return vals


def _spec_atoms(text: str) -> dict:
    """从标题里抽「这件东西是什么」的原子：封装、料号、各项参数值。"""
    raw = str(text or "")
    # 原子必须在**保留分隔符**的文本上抽：去掉标点后 "1N5819W SOD-123" 会粘成
    # 一个假料号 "1n5819wsod123"，跟另一条标题里的 "1n5819w" 就对不上了。
    base = unicodedata.normalize("NFKC", raw).lower()
    base = _DIELECTRIC_RE.sub(" ", base)
    atoms = {
        "packages": set(_PKG_RE.findall(base)),
        "mpns": set(),
        "power": set(),
    }
    for tok in _MPN_RE.findall(base):
        # 料号：≥4 位、同时含字母和数字（纯数字是数量/封装，不能当料号）；
        # 封装代号和「100nf / 100v / 47uf」这类纯参数值也要剔掉，
        # 否则「0603 100nF 50V」和「0603 100nF 100V」会因为 100nf 相同而误判同一件。
        if not (re.search(r"[a-z]", tok) and re.search(r"\d", tok)):
            continue
        if tok in _PKG_TOKENS or _VALUE_TOKEN_RE.match(tok):
            continue
        atoms["mpns"].add(tok)
    # 1/4W、1/8W、100mW 统一成 W
    for den in re.findall(r"(?<![0-9.])1\s*/\s*(\d+)\s*w", base):
        atoms["power"].add(1.0 / float(den))
    # 100mW / 0.5W（1/4W 已单独处理，这里排除「/4W」的尾巴，避免记成 4W）
    for val, unit in re.findall(r"(?<![0-9./])(\d+(?:\.\d+)?)\s*(m?)w(?![a-z])", base):
        atoms["power"].add(float(val) * (1e-3 if unit else 1.0))
    res = _res_values(base)
    if res:
        atoms["resistance"] = res
    for name, pat in _VALUE_PATTERNS.items():
        vals = set()
        for m in pat.finditer(base):
            num = float(m.group(1))
            # 只有「值+单位」这类模式才有第 2 组；针数/间距没有单位组
            unit = (m.group(2) if m.re.groups > 1 else "") or ""
            unit = str(unit).strip()
            if name == "frequency":
                vals.add(num * _FREQ_SCALE.get(unit.lower(), 1.0))
            elif name in ("pins", "pitch"):
                vals.add(num)
            else:
                vals.add(round(num * _VALUE_SCALE.get(unit, 1.0), 12))
        if vals:
            atoms[name] = vals
    return atoms


# 参与「冲突判定」的单位：两边都写了就必须相等，否则不是同一件东西
_CONFLICT_UNITS = ("resistance", "capacitance", "inductance", "voltage", "power",
                   "current", "frequency", "pins", "pitch")


def _titles_match(a: str, b: str) -> bool:
    """两条标题是不是同一件商品（同料号 / 同封装同规格 / 一个是另一个的片段）。"""
    ka, kb = _norm_key(a), _norm_key(b)
    if not ka or not kb:
        return False
    A, B = _spec_atoms(a), _spec_atoms(b)
    # ① 料号级证据：最硬的判据（0603WAF1002T5E、1N5819W、AMS1117-3.3 …）
    if A["mpns"] & B["mpns"]:
        return True
    # ② 单位相同但值不同 → 直接判不同（10kΩ vs 1kΩ 只差一个字符，相似度骗人）
    for unit in _CONFLICT_UNITS:
        va, vb = A.get(unit) or set(), B.get(unit) or set()
        if va and vb and not (va & vb):
            return False
    # ③ 封装冲突（0603 vs 0805；XH 与 PH 这类不含数字的靠包含关系兜底）
    pa, pb = A["packages"], B["packages"]
    if pa and pb and not (pa & pb):
        return False
    # ④ 两边都写明了主规格（阻/容/感）且相等 → 认为是同一件
    for unit in ("resistance", "capacitance", "inductance"):
        va, vb = A.get(unit) or set(), B.get(unit) or set()
        if va and vb and (va & vb):
            return True
    # ⑤ 最后一档：短的那条完整出现在长的里面（淘宝常把后缀/包装词截掉）
    short, long = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(short) >= 6 and short in long:
        return True
    return False


class SessionError(RuntimeError):
    def __init__(self, message, code="SESSION_ERROR", detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class SessionRunner:
    def __init__(self, command=None, repo=None, artifacts=None):
        self._command = list(command) if command else None
        self._repo = Path(repo).resolve() if repo else REPO
        self._artifacts = Path(artifacts).resolve() if artifacts else ARTIFACTS
        self._proc: subprocess.Popen | None = None
        self._cmd_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._responses: queue.Queue = queue.Queue()
        self._logs: deque[str] = deque(maxlen=400)
        self._task_id = None
        self._ready = False
        self._waiting_login = False
        self._login_state = "unknown"
        self._verification = False
        self._verify_probe_at = 0.0
        self._desynced = False
        self._started_at = None
        self._started_mono: float | None = None
        # 启动卡住的判定阈值：从拉起会话进程算起，既没就绪、也没进入「等待登录」
        # 超过这个秒数，就当它卡住了（正常运行 10 秒内一定会有其中一个信号）。
        # 新机器上 Playwright 驱动被杀软拦、浏览器被对话框挡住，都会坐在这里不动，
        # 前端原来只会一直显示「启动中」，用户不知道是慢还是死了。
        try:
            self._startup_timeout = max(5.0, float(os.environ.get("BOM_STARTUP_TIMEOUT") or 45))
        except (TypeError, ValueError):
            self._startup_timeout = 45.0
        self._stall_logged = False
        self._log_path = None
        self._last_error = None
        self._guard = None  # 前台守卫：跑批时别让浏览器盖住用户正在用的软件
        self._guard_lock = threading.RLock()
        self._autohide_done = False
        self._browser_gone = False
        self._batch = {
            "running": False,
            "total": 0,
            "done": 0,
            "index": -1,
            "phase": "未开始",
            "results": [],
            "started_at": None,
            "finished_at": None,
            "stopped_reason": None,
            "verification": None,
        }
        # I7：把上次的进度装回状态机。否则重启后前端只看到 0/0，
        # 分不清「没跑过」和「跑了一半」，也没法判断 running 是真在跑还是残留。
        self._restore_batch_from_progress()

    def _restore_batch_from_progress(self) -> None:
        """启动时装载磁盘上的上次进度（不启动线程，running 恒为 False）。

        关键点：**没有活线程就不能说 running=true**。以前站点重启后
        `_batch` 可能是脏的（或干脆空着），前端要么显示「还在跑」点不动按钮，
        要么显示 0/0 看不出能续跑。
        """
        try:
            prog = load_batch_progress()
        except Exception:  # noqa: BLE001
            return
        # 演练批次的进度不该存在（见 _save_progress），旧版本可能残留过：见到就别装
        if (prog or {}).get("dry_run"):
            _log("[batch] 进度文件来自演练批次，忽略（不能当成已加购）")
            return
        lines = (prog or {}).get("lines") or {}
        if not lines:
            return
        results = []
        for key in sorted(lines, key=lambda x: int(x) if str(x).isdigit() else 0):
            v = lines[key] or {}
            try:
                line_no = int(v.get("line_no") or key)
            except (TypeError, ValueError):
                continue
            results.append(
                {
                    "line_no": line_no,
                    "headline": v.get("headline") or "",
                    "qty": v.get("qty"),
                    "status": v.get("status") or "pending",
                    "message": v.get("message") or "",
                    "attempts": v.get("attempts") or [],
                    "candidate": v.get("candidate"),
                    "screenshot": v.get("screenshot"),
                    "cost": v.get("cost"),
                    "final_price": v.get("final_price"),
                    "total_ms": v.get("total_ms"),
                }
            )
        # removed 也是定局：这一行本来加购成功，被「删最近一批」从车里删掉了。
        # 不算定局的话，重启装载时会把整批当成「没跑完」，页面挂「服务重启」文案。
        settled = SETTLED_STATUS
        has_pending = any(r["status"] not in settled for r in results)
        # P2-4：上一轮已经跑完（没有 pending 行）就不要说「服务重启」——
        # 那是中断态文案，会误导人以为有一批没跑完。
        if has_pending:
            phase = "已装载上次进度（服务重启，点「继续加购」接着跑）"
            stopped = "server restarted"
        else:
            phase = "已装载上次进度（上一轮已跑完）"
            stopped = (prog or {}).get("stopped_reason") or ""
        with self._state_lock:
            self._batch.update(
                {
                    "running": False,
                    "total": int((prog or {}).get("total") or len(results)),
                    "done": sum(1 for r in results if r["status"] in settled),
                    "index": -1,
                    "phase": phase,
                    "results": results,
                    "started_at": (prog or {}).get("started_at"),
                    "finished_at": (prog or {}).get("finished_at"),
                    "stopped_reason": stopped,
                    "restored": True,
                    # 指纹要一起装回来：前端/接口判断「这批进度属于哪份 BOM」要用它
                    "fingerprint": (prog or {}).get("fingerprint") or "",
                }
            )

    # ---------------- 生命周期 ----------------
    def start(self) -> dict:
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                # 会话进程还活着，但浏览器被用户手动关了：那是个「僵尸会话」——
                # 命令全都会失败。先收掉再重建，否则点「启动」什么都不会发生。
                gone = self._browser_gone or self._browser_unreachable()
                if not gone:
                    return self.status()
                self._append_log("[site] 浏览器已关闭，重建会话 …")
                self.stop(timeout=5)
            self._reset_state()
            self._task_id = "bom-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            self._log_path = RUNS / f"session-{self._task_id}.log"
            self._log_path.write_text("", encoding="utf-8")

            env = session_env()
            settings = load_settings()
            cdp_url = os.environ.get("TAOBAO_CDP_URL") or settings.get("cdp_url")
            if cdp_url:
                cdp_url = str(cdp_url).strip()
                # 页面点“启动”时调试版 Edge 常常没开着：先自动拉起，否则必然秒退
                if not cdp_endpoint_ok(cdp_url):
                    self._append_log(f"[site] {cdp_url} 未就绪，正在自动启动调试浏览器 …")
                    ensure_debug_browser(cdp_url, log=self._append_log)
                env["TAOBAO_CDP_URL"] = cdp_url

            # Pace profile: "fast" trims the simulated human pauses, the
            # networkidle caps and the per-step screenshots. Only timings
            # change — every SKU / cart check stays exactly the same.
            pace = str(settings.get("pace") or "fast").lower()
            if pace == "fast":
                env.setdefault("TAOBAO_SPEED", str(settings.get("speed") or 0.35))
                env.setdefault("TAOBAO_SHOTS", "fail")
                env.setdefault("TAOBAO_IDLE_TIMEOUT", str(settings.get("idle_timeout") or 3))
                env.setdefault("TAOBAO_BROWSE_SIM", "0")

            cmd = list(self._command) if self._command else [
                sys.executable,
                str(self._repo / "scripts" / "taobao.py"),
                "session",
                "--task-id",
                self._task_id,
            ]
            try:
                sub_kwargs: dict = {}
                if os.name == "nt":
                    # 不建控制台：站点是 cmd 拉起来的，会话进程再开一个黑框纯属添乱
                    sub_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(self._repo),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    # 子进程偶尔会按控制台编码（GBK）输出中文，严格 utf-8 解码会
                    # 在读取线程里抛 UnicodeDecodeError（日志直接断掉）。替换掉就好。
                    errors="replace",
                    bufsize=1,
                    env=env,
                    **sub_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                raise SessionError(f"无法启动会话进程：{exc}", "SESSION_START_FAILED") from exc

            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._started_mono = time.monotonic()
            threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()
            self._append_log(f"[site] session process started pid={self._proc.pid}")
            self._start_guard()
            return self.status()

    # ---------------- 窗口 / 焦点（别盖住用户正在用的软件） ----------------
    def _start_guard(self) -> None:
        """起前台守卫：自动化执行期间，浏览器一抢前台就还给用户。

        只在**发命令的时候**生效（begin/end），命令结束留 2s 尾巴；用户自己点
        这个浏览器窗口时守卫不动手，不会跟用户抢。
        """
        if _focus_guard is None:
            return
        with self._guard_lock:
            guard = self._guard
            if guard is not None and guard.is_alive():
                return
            try:
                port = cdp_port(
                    os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url")
                )
                self._guard = _focus_guard.FocusGuard(port=port, log=self._append_log).start()
                self._append_log("[site] 前台守卫已启动：执行命令时浏览器抢前台会立刻还给用户")
            except Exception as exc:  # noqa: BLE001
                self._guard = None
                self._append_log(f"[site] 前台守卫启动失败（不影响跑批）：{exc}")

    def _stop_guard(self) -> None:
        with self._guard_lock:
            guard, self._guard = self._guard, None
        if guard is not None:
            try:
                guard.stop_guard()
            except Exception:  # noqa: BLE001
                pass

    def _guard_begin(self) -> None:
        guard = self._guard
        if guard is not None:
            try:
                guard.begin()
            except Exception:  # noqa: BLE001
                pass

    def _guard_end(self) -> None:
        guard = self._guard
        if guard is not None:
            try:
                guard.end()
            except Exception:  # noqa: BLE001
                pass

    def _guard_pids(self) -> set[int]:
        """调试版 Edge 的 pid 集合（守卫里缓存过就直接用，省一次 powershell）。"""
        guard = self._guard
        if guard is not None:
            try:
                pids = guard.known_pids()
                if pids:
                    return pids
            except Exception:  # noqa: BLE001
                pass
        if _window_pos is None:
            return set()
        try:
            port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
            # 浏览器已经关了就别去查进程：那要起 powershell，每起一次就多一个
            # 控制台窗口（用户看到的就是「命令窗口一直弹出又关闭」）。
            if not _window_pos.debug_browser_alive(port):
                return set()
            return (
                _window_pos.debug_edge_pids()
                if port == 9222
                else _window_pos.debug_edge_pids_for(port)
            )
        except Exception:  # noqa: BLE001
            return set()

    def _browser_unreachable(self) -> bool:
        """调试浏览器还连得上吗（关掉网页/进程后 CDP 端口就不通了）。"""
        if _window_pos is None:
            return False
        try:
            port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
            return not _window_pos.debug_browser_alive(port, timeout=0.5)
        except Exception:  # noqa: BLE001
            return False

    def hide_browser(self) -> bool:
        """把调试浏览器藏到屏幕外（不最小化，渲染不停，截图照常）。"""
        if _window_pos is None:
            return False
        guard = self._guard
        if guard is not None:
            try:
                guard.set_keep_hidden(True)  # 后冒出来的气泡/新窗口也接着藏
            except Exception:  # noqa: BLE001
                pass
        try:
            ok = _window_pos.hide(self._guard_pids())
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] 隐藏浏览器窗口失败：{exc}")
            return False
        if ok:
            self._append_log("[site] 浏览器窗口已藏到屏幕外（需要看时点「显示淘宝窗口」）")
        return ok

    def show_browser(self, pos: tuple[int, int] | None = None) -> bool:
        """把调试浏览器移回屏幕内（人工过验证 / 用户主动看），不抢前台。

        用户主动点过「显示」= 接管窗口显示权：本会话之后 status() 不再自动
        藏回屏幕外（否则启动期间点显示，ready 一到就被藏，看着像「闪一下就没」）。
        """
        if _window_pos is None:
            return False
        guard = self._guard
        if guard is not None:
            try:
                guard.set_keep_hidden(False)  # 先撤意图，否则守卫会把刚显示出来的又藏回去
            except Exception:  # noqa: BLE001
                pass
        try:
            ok = _window_pos.show(self._guard_pids(), pos or _window_pos.SCREEN_POS)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] 显示浏览器窗口失败：{exc}")
            return False
        if ok:
            self._autohide_done = True  # 用户已主动接管，别再自动藏
        return ok

    def stop(self, timeout: float = 8.0) -> dict:
        # UI 承诺「结束会话会关闭淘宝浏览器窗口」——只 quit 会话进程的话
        # Edge 还留在后台（2026-11-09 用户实测点关闭没反应）。这里补杀
        # 调试版 Edge 的进程树；只杀带 remote-debugging-port 的，
        # 用户自己开的普通 Edge 不受影响。会话进程不在（proc=None）时
        # 也要关 —— 那正是「浏览器还挂着、会话早就停了」的常见残留态。
        if _window_pos is not None and _window_pos.debug_browser_alive():
            _closed = _window_pos.close()
            self._append_log(
                "[site] debug Edge closed" if _closed else "[site] debug Edge close failed"
            )
        with self._cmd_lock:
            proc = self._proc
            if proc is None:
                return self.status()
            if proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self._append_log("[site] session stopped")
            # 先落盘再清内存：顺序反了会把进度覆盖成空（见 _save_progress 注释）
            self._save_progress()
            self._reset_state()
            # 会话收工也照旧只留本批证据图：下一批开跑前要对着这些图核对「上次买了什么」
            self.cleanup_screenshots()
            return self.status()

    def _reset_state(self):
        self._stop_guard()  # 会话都停了，守卫也别再动人家窗口
        self._autohide_done = False
        self._browser_gone = False
        self._proc = None
        self._ready = False
        self._started_mono = None
        self._stall_logged = False
        self._waiting_login = False
        self._login_state = "unknown"
        self._verification = False
        self._verify_probe_at = 0.0
        self._desynced = False
        self._task_id = None
        self._started_at = None
        self._last_error = None
        with self._state_lock:
            was_running = bool(self._batch.get("running"))
            self._batch = {
                "running": False,
                "total": 0,
                "done": 0,
                "index": -1,
                "phase": "未开始",
                "results": [],
                "started_at": None,
                "finished_at": None,
                "stopped_reason": "会话已结束，批量任务中止" if was_running else None,
                "verification": None,
            }
        # 会话重启不能把「哪些行已经在购物车里」从界面上抹掉：进度文件才是事实。
        # 以前这里清完就完，点一次「启动淘宝会话」台账整列变「未跑」，页头却
        # 还写着「已完成 N 行」，两边打架，用户根本不敢确认自己买了什么。
        self._restore_batch_from_progress()
        if was_running:
            with self._state_lock:
                self._batch["stopped_reason"] = "会话已结束，批量任务中止（进度已保存）"
                self._batch["phase"] = "会话已结束，进度已保存，可点「继续加购」接着跑"
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    def _should_probe_verification(self) -> bool:
        """要不要在这一轮状态轮询里顺手探验证。"""
        if not self._verification:
            return False
        with self._state_lock:
            if self._batch.get("running"):
                return False  # 批量自己在发命令，成功即复位，别插队
        return (time.monotonic() - self._verify_probe_at) >= 2.5

    def recheck_verification(self, timeout: float = 20.0) -> bool:
        """探一次页面：验证是否已经人工处理完。返回 True = 已无验证，可以继续。

        为什么需要它：`_verification` 原先只在「下一条命令成功」时复位。
        批量一停就再没有命令发出，标志会一直挂着 true，前端据此把所有按钮
        锁死（继续加购点不动），必须重启会话才能解开 —— 就是那个死锁。

        抢锁用非阻塞：批量命令正在跑时直接跳过，绝不排队等，
        否则一旦等待超时会话被判成 desynced，反而更糟。
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        if not self._cmd_lock.acquire(blocking=False):
            return False
        try:
            try:
                resp = self.send({"cmd": "ping"}, timeout=timeout)
            except SessionError:
                return False
            ok = str(resp.get("status")) == "success"
            with self._state_lock:
                self._verification = not ok
                self._verify_probe_at = time.monotonic()
            if ok:
                self._append_log("[session] 验证已解除，按钮恢复可用")
            return ok
        finally:
            self._cmd_lock.release()

    def status(self, auto_probe: bool = True) -> dict:
        proc = self._proc
        running = proc is not None and proc.poll() is None
        # 标志卡住时顺手探一下：人工过完滑块，2~3 秒内界面自己就恢复了，
        # 不用重启会话，也不用点任何按钮。
        if auto_probe and running and self._should_probe_verification():
            self.recheck_verification()
        # 会话就绪（登录都好了）就把浏览器藏到屏幕外：调试版 Edge 是用户自己的
        # 窗口，开着不动也会盖住别人正在用的软件。等登录/验证期间不藏，那时候
        # 用户本来就要看这个窗口。每个会话只自动藏一次，之后用户点「显示」就不再抢。
        #
        # 必须**确认登录成功**才藏。以前只看 ready：新用户登录超时后会话也算
        # 「就绪」，窗口当场被挪到屏幕外，人还没扫码就先看不见浏览器了。
        if (
            running
            and self._ready
            and not self._waiting_login
            and not self._verification
            and self._login_state == "ok"
            and not self._autohide_done
        ):
            self._autohide_done = True
            threading.Thread(target=self.hide_browser, daemon=True).start()
        # 浏览器被手动关掉时，会话进程还活着但已经没法干活了：这里探一下端口，
        # 明确告诉界面「浏览器没了」，别等用户点了加购才报错。
        if running and _window_pos is not None and not self._verification:
            try:
                port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
                gone = not _window_pos.debug_browser_alive(port, timeout=0.5)
            except Exception:  # noqa: BLE001
                gone = self._browser_gone
            if gone and not self._browser_gone:
                self._browser_gone = True
                self._append_log("[site] 调试浏览器已关闭（端口不通）；请重新启动会话")
            elif not gone and self._browser_gone:
                self._browser_gone = False
                self._append_log("[site] 调试浏览器已恢复")
        stalled = self._startup_stalled(running)
        if stalled and not self._stall_logged:
            self._stall_logged = True
            self._append_log(
                f"[site] 启动卡住了：{self._startup_timeout:g} 秒内既没就绪、也没进入登录等待。"
                "多半是 Playwright 驱动或浏览器被安全软件拦住 / 还卡在对话框上。"
                "点「结束会话」再「启动淘宝会话」；反复卡住就先把杀毒软件对这个目录的拦截关掉。"
            )
        with self._state_lock:
            return {
                "running": running,
                "ready": self._ready and running,
                "waiting_login": self._waiting_login and running,
                "login_state": self._login_state,
                "verification": self._verification,
                "browser_gone": self._browser_gone,
                "desynced": self._desynced,
                "stalled": stalled,
                "startup_timeout": self._startup_timeout,
                "task_id": self._task_id,
                "pid": proc.pid if running else None,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "repo": str(self._repo),
                "logs": list(self._logs)[-60:],
            }

    def _startup_stalled(self, running: bool) -> bool:
        """会话进程活着，却迟迟不给「就绪」或「等待登录」信号。

        正常路径 10 秒内必有一声（本机实测 7~15 秒）。超过阈值还一声不响，
        基本就是驱动没起来 / 浏览器被挡，而不是「再等等就好」。
        """
        if not running or self._ready or self._waiting_login:
            return False
        if self._verification or self._browser_gone:
            return False
        started = self._started_mono
        if started is None:
            return False
        return (time.monotonic() - started) >= self._startup_timeout

    # ---------------- 输出读取 ----------------
    def _read_stdout(self, proc):
        buf = ""
        decoder = json.JSONDecoder()
        try:
            for line in proc.stdout:
                buf += line
                while True:
                    s = buf.lstrip()
                    if not s:
                        buf = ""
                        break
                    try:
                        obj, end = decoder.raw_decode(s)
                    except json.JSONDecodeError:
                        break
                    buf = s[end:]
                    if isinstance(obj, dict):
                        self._responses.put(obj)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] stdout reader stopped: {exc}")

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                self._append_log(line)
                low = line.lower()
                with self._state_lock:
                    if "session ready, waiting for commands" in low:
                        self._ready = True
                        self._waiting_login = False
                        self._last_error = None
                    if "cdp 端点连不上" in line or line.startswith("RuntimeError:"):
                        self._last_error = line.strip()
                    if "waiting for human takeover" in low or "手动完成淘宝登录" in line:
                        self._waiting_login = True
                        if self._login_state != "ok":
                            self._login_state = "waiting"
                    if "login successful" in low or "登录成功" in line:
                        self._login_state = "ok"
                    if "check login status => logged in" in low:
                        # 本来就有登录态的机器：一开始就是 ok。留成 unknown 的话
                        # 「确认登录了才把窗口藏起来」那条判断永远不成立。
                        self._login_state = "ok"
                    if "login failed or timed out" in low or "登录失败" in line:
                        self._login_state = "failed"
                    if "verification required" in low:
                        self._verification = True
        except Exception:
            pass

    def _append_log(self, line: str):
        entry = f"{datetime.now():%H:%M:%S} {line}"
        with self._state_lock:
            self._logs.append(entry)
        if self._log_path is not None:
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    # ---------------- 命令 ----------------
    def send(self, cmd: dict, timeout: float | None = None) -> dict:
        with self._cmd_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise SessionError("淘宝会话未启动或已退出", "SESSION_NOT_STARTED")
            if self._desynced:
                raise SessionError("上一个命令超时，会话状态不确定；请结束会话后重新启动", "SESSION_DESYNCED")
            timeout = timeout or DEFAULT_TIMEOUTS.get(str(cmd.get("cmd")), 90)
            self._guard_begin()  # 这段区间里浏览器敢抢前台，守卫就把它压回去
            try:
                try:
                    proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                except Exception as exc:  # noqa: BLE001
                    raise SessionError(f"写入会话失败：{exc}", "SESSION_WRITE_FAILED") from exc

                try:
                    resp = self._responses.get(timeout=timeout)
                except queue.Empty:
                    self._desynced = True
                    raise SessionError(
                        f"命令 {cmd.get('cmd')} 超时（{timeout:g}s）。页面可能卡在登录/验证/加载。",
                        "COMMAND_TIMEOUT",
                    ) from None
            finally:
                self._guard_end()

            if resp.get("status") == "error":
                code = (resp.get("error") or {}).get("code")
                if code == "VERIFICATION_REQUIRED":
                    self._verification = True
            else:
                self._verification = False
            return resp

    # ---------------- 截图 ----------------
    def wait_verification(self, max_s: int | None = None, poll_s: float = 5.0) -> bool:
        """刷新自恢复失败后（页面刷完仍不出商品）才走这里：等人工完成验证。

        用 ping 当探针：会话进程在验证未解除时会一直回 VERIFICATION_REQUIRED，
        解除后第一条命令自动复位闸门 —— 所以这里只要反复 ping，成功即代表恢复。
        """
        deadline = time.monotonic() + (max_s if max_s is not None else VERIFY_WAIT_S)
        # 守卫先退场：用户自己点开窗口操作时，不会被「防抢焦点」压回去。
        guard = self._guard
        if guard is not None:
            guard.pause("等人工过验证")
        try:
            return self._wait_verification_loop(deadline, poll_s)
        finally:
            self.hide_browser()  # 用户手动打开过的话，结束（解除/超时/人停）后藏回屏幕外
            if guard is not None:
                guard.resume()

    @staticmethod
    def _move_debug_window(x: int | None = None, y: int | None = None) -> None:
        """老接口：调 window_pos.py 子进程挪窗口（保留给脚本/兼容用）。"""
        try:
            import subprocess
            args = [sys.executable, str(Path(__file__).parent / "window_pos.py")]
            if x is not None:
                args += [str(x), str(y)]
            subprocess.run(args, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            print(f"[batch] window_pos skipped: {exc}", flush=True)

    def _wait_verification_loop(self, deadline: float, poll_s: float) -> bool:
        last_log = 0.0
        total = max(0, int(deadline - time.monotonic()))
        # 不再自动弹窗（太突兀）：挂一个状态出去，前端弹站内提示，
        # 用户点「打开淘宝窗口」才真的把窗口移进来。
        with self._state_lock:
            self._batch["verification"] = {
                "waiting": True,
                "reason": "自动过滑块和刷新都没过，需要人工完成淘宝验证",
                "total_s": total,
                "remaining_s": total,
                "since": datetime.now().isoformat(timespec="seconds"),
            }
        self._append_log(
            f"[batch] 自动过滑块和刷新都没过 → 需要你手动完成验证（最多等 {total // 60} 分钟）"
        )
        try:
            while time.monotonic() < deadline:
                with self._state_lock:
                    if self._batch.get("stopped_reason"):
                        return False  # 人主动停了，别再等
                time.sleep(poll_s)
                try:
                    resp = self.send({"cmd": "ping"}, timeout=25)
                except SessionError as exc:
                    if exc.code in ("SESSION_NOT_STARTED", "SESSION_DESYNCED"):
                        return False
                    continue
                if resp.get("status") != "error":
                    self._append_log("[batch] 验证已解除，从断行继续")
                    return True
                now = time.monotonic()
                left = max(0, int(deadline - now))
                with self._state_lock:
                    v = self._batch.get("verification") or {}
                    v["remaining_s"] = left
                    self._batch["verification"] = v
                if now - last_log > 30:
                    last_log = now
                    with self._state_lock:
                        self._batch["phase"] = f"刷新后仍无商品，等人工验证（剩余 {left}s）"
                    print(f"[batch] 仍在等验证… 剩余 {left}s", flush=True)
            return False
        finally:
            with self._state_lock:
                self._batch["verification"] = None

    def persist_screenshot(self, path: str | None) -> str | None:
        if not path:
            return None
        src = Path(path)
        try:
            src = src.resolve()
            src.relative_to(self._artifacts)
        except (ValueError, OSError):
            return None
        if not src.is_file():
            return None
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{src.name}"
        dst = RUNS / name
        try:
            shutil.copy2(src, dst)
        except OSError:
            return None
        return "/runs/" + name

    def cleanup_screenshots(self, keep: int | None = None) -> dict:
        """每跑完一批清一次：只留这一批的证据图，其余删掉。

        这里踩过两个坑，合起来等于「清理从没生效过」：

        1. 只 glob `*.png`，而 `persist_screenshot` 存的是 `.jpg` —— 一张都没删掉过，
           实测 `runs/` 堆到 223 张 27 MB。
        2. 停会话时按「全清」执行。用户下一步要点的正是「删上一次加购」，
           靠的就是这批图核对买了什么；全清等于把核对依据一起删了。

        现在按**进度记录**判：进度里点名的截图（本批真正跑过的行）一张不动，
        其余（更早批次、试跑、临时采集图）全删。`screenshot_keep` 只额外保留
        最近 N 张用于排查，不是「保留全部」。

        会话还在跑的时候不删：正在写的文件删了会让界面上的图裂掉。
        """
        # 只看批量标志，不看进程对象：以前要求「进程活着且在跑批」才跳过，
        # 一旦 _proc 为空（换会话、进程对象丢了）就会在跑批途中删图，
        # 正在写的文件直接裂在界面上。跑批中一律不碰。
        with self._state_lock:
            if self._batch.get("running"):
                return {"deleted": 0, "freed_bytes": 0, "skipped": "批量运行中"}
        if keep is None:
            try:
                keep = int(load_settings().get("screenshot_keep") or 0)
            except (TypeError, ValueError):
                keep = 0
        keep = max(0, keep)

        keep_names = batch_screenshot_names()
        deleted = 0
        freed = 0
        kept = 0
        extra_kept = 0
        for folder in {self._artifacts, RUNS}:
            try:
                shots = [
                    p
                    for p in Path(folder).iterdir()
                    if p.is_file() and p.suffix.lower() in SHOT_EXTS
                ]
                shots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                continue
            for p in shots:
                if p.name in keep_names:
                    kept += 1
                    continue
                if extra_kept < keep:
                    extra_kept += 1
                    kept += 1
                    continue
                try:
                    size = p.stat().st_size
                    p.unlink()
                    deleted += 1
                    freed += size
                except OSError:
                    continue

        if deleted:
            mb = freed / 1024 / 1024
            self._append_log(
                f"[site] 清理截图：删 {deleted} 张（{mb:.1f} MB），保留本批证据 {kept} 张"
            )
        return {
            "deleted": deleted,
            "freed_bytes": freed,
            "kept": kept,
            "kept_batch": len(keep_names),
            "keep_extra": keep,
        }

    # ---------------- 业务封装 ----------------
    def search(self, line: dict, query: str | None = None, require_scored: bool = False) -> dict:
        """搜索并打分。

        require_scored=True（批量用）：一条搜索词虽然有结果，但候选全被硬门槛拒掉时，
        不要就此判整行失败 —— 换下一条搜索词再试。典型场景是「型号 + 封装」这种组合词
        在淘宝会召回到一堆近似型号，而只搜料号反而能命中（DMP4015SK3-13 DPAK → 0 命中，
        DMP4015SK3-13 → 有货）。
        """
        queries = [query] if query else list(line.get("query_plan") or [])
        if not queries:
            raise SessionError("该行没有可用搜索词", "NO_QUERY")
        line_no = line.get("line_no")
        attempts = []
        used = None
        items = []
        scored = None
        resp = None
        tried = queries[:4]
        for qi, q in enumerate(tried):
            try:
                # light：不截图、不取整页文本 —— 每行省 0.4–0.6s，119 行就是 1 分钟
                resp = self.send({"cmd": "search", "keyword": q, "light": True})
            except SessionError as exc:
                # 搜索页偶发超时也一样：换下一条词，而不是直接判这行没救
                if exc.code == "VERIFICATION_REQUIRED" or not _is_transient_error(exc):
                    raise
                attempts.append({"query": q, "error": {"code": exc.code, "message": str(exc)[:120]}})
                diagnostics.record("search_error", line_no=line_no, query=q, code=exc.code)
                continue
            if resp.get("status") == "error":
                err = resp.get("error") or {}
                if err.get("code") == "VERIFICATION_REQUIRED":
                    raise SessionError(err.get("message") or "需要人工完成验证", "VERIFICATION_REQUIRED", err)
                attempts.append({"query": q, "error": err})
                diagnostics.record("search_error", line_no=line_no, query=q,
                                   code=str(err.get("code") or "")[:40])
                continue
            got = (resp.get("data") or {}).get("items") or []
            diagnostics.record("search", line_no=line_no, query=q, items=len(got))
            if not got:
                attempts.append({"query": q, "items_count": 0})
                continue
            cands_try = [
                {
                    "title": _title_with_spec(line, it),
                    "spec_text": it.get("spec_text") or "",
                    "price": it.get("price_value"),
                    "sales": it.get("sales_count"),
                    "rating": it.get("rating"),
                    "shop_type": "",
                    "is_tmall": bool(it.get("is_tmall")),
                    "real_photo": False,
                    "index": it.get("index"),
                    "url": it.get("url"),
                }
                for it in got
            ]
            scored_try = engine.score_candidates(line, candidates=cands_try, execution=True)
            # 「可用」的口径必须和下游 _buy_one 的放行规则一致：
            # score > 0 只是及格线，淘汰级（decision=淘汰 且 <20 分）在下游同样被拒
            # （评分 11.1 分的候选一个都买不成）。以前这里只看 score>0，结果型号词
            # 召回一堆 11.1 分的近似型号也当「这条词成功了」，后面「2.54mm 排针 40P」
            # 这种能拿 78.7 分的通用词一次都没机会执行，整行白死。
            _need_kinds = _required_spec_kinds(line)

            def _cand_usable(c, _need=_need_kinds) -> bool:
                # _need 用默认参数绑定：这个函数在 for 循环里定义，直接闭包引用
                # _need_kinds 会变成晚绑定（ruff B023）；虽然当前只在当轮调用，
                # 但绑定住能防止以后改成「先收集再筛」时静默错位。
                sc = c.get("score") or 0
                if sc <= 0:
                    return False
                if str(c.get("decision") or "") == "淘汰" and sc < 20:
                    return False
                if _candidate_veto(line, c.get("title") or ""):
                    return False
                if _material_conflict(line, c.get("title") or ""):
                    return False
                # 硬规格（型号/针数）在标题里核实不到，下游 _buy_one 一定会拒。
                # 只看分数就 break 的话，词 1 会拿一堆「分数不低但核实不到针数」的
                # 候选把整行判死：PZ254-2-20-Z-8.5 → 搜回来全是「2.54MM排针」系列词条，
                # 标题只写间距不写 40P，最后一句话「没有识别到针数」失败，
                # 而后面「2.54mm 排针 40P」明明有 73 分、针数可核实的货。
                if _need - _spec_kinds(c):
                    return False
                return True

            usable = any(_cand_usable(c) for c in scored_try["candidates"])
            items, used, scored = got, q, scored_try
            if usable or not require_scored or qi == len(tried) - 1:
                break
            attempts.append(
                {"query": q, "items_count": len(got), "reason": "候选都不匹配，换下一条搜索词"}
            )

        if resp is None:
            raise SessionError("搜索没有返回结果", "SEARCH_NO_RESPONSE")
        if not items or scored is None:
            return {
                "ok": False,
                "code": "A01",
                "message": "搜索词都没有可用结果；不要放宽硬门槛，换词或人工处理。",
                "attempts": attempts,
                # 搜索走 light 模式不带图，失败时补拍一张，方便人看当时页面到底怎么了
                "screenshot": self.persist_screenshot(self._grab_shot()),
                "page_text": resp.get("page_text_summary"),
                "queries": queries,
                # 失败时把这一步的诊断摘要一起带回：全 0 结果 + 选择器错误
                # 会标 structure_suspect，提示「可能是页面改版/风控」而不是词错。
                "diagnostics": diagnostics.summarize(
                    diagnostics.recent(limit=80, line_no=line_no)
                ),
            }

        return {
            "ok": True,
            "query": used,
            "items_count": len(items),
            "candidates": scored["candidates"],
            "stats": scored["stats"],
            "verdict": scored["verdict"],
            "attempts": attempts,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
            "queries": queries,
        }

    def _grab_shot(self) -> str | None:
        """按需补拍一张当前视口截图（轻量模式下失败路径用）。"""
        try:
            resp = self.send({"cmd": "screenshot"})
            return (resp.get("screenshot") if isinstance(resp, dict) else None) or None
        except Exception:  # noqa: BLE001
            return None

    def open_item(self, line: dict, index: int) -> dict:
        # shot=False：开页截图对批量没价值（证据图在加购那一步），省 0.3–0.5s/行
        resp = self.send({"cmd": "open", "index": int(index), "shot": False})
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            raise SessionError(err.get("message") or "打开商品失败", err.get("code") or "OPEN_FAILED", err)
        data = resp.get("data") or {}
        item = data.get("item") or {}
        groups = data.get("sku_groups") or []
        suggestions, unresolved = suggest_sku(line, groups, item.get("title") or "")
        sku_texts = [s.get("value", "") for s in suggestions]
        pack, pack_src = _pack_from_texts(sku_texts, explicit_only=False, allow_generic=True)
        if not pack:
            pack, pack_src = _title_pack(item.get("title") or "")
        if not pack:
            pack, pack_src = _pack_from_texts([resp.get("page_text_summary") or ""], explicit_only=True)
        bom_qty = line.get("qty")
        suggested_qty, pack, actual_pcs = _plan_qty(bom_qty, pack)
        price = data.get("detail_price")
        unit_price = round(price / pack, 6) if (price and pack and pack > 1) else price
        return {
            "ok": True,
            "item": item,
            "detail_price": price,
            "sku_groups": groups,
            "no_sku": not groups,
            "suggestions": suggestions,
            "unresolved": unresolved,
            "pack_size": pack if pack and pack > 1 else None,
            "pack_source": pack_src,
            "bom_qty": bom_qty,
            "suggested_qty": suggested_qty,
            "actual_pcs": actual_pcs,
            "unit_price": unit_price,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    def add_to_cart(self, line: dict, index: int, selections: list, qty: int,
                    allow_empty: bool = False, auto_qty: bool = False,
                    pack_hint: int | None = None, price_hint: float | None = None) -> dict:
        skip_sku = not isinstance(selections, list) or not selections
        if skip_sku and not allow_empty:
            return {"ok": False, "code": "NO_SELECTIONS", "message": "没有 SKU 选择，拒绝加购（避免默认 SKU）"}

        _sku_ms = 0
        groups = []
        resp = {}
        final_price = None
        pack, pack_src = None, ""
        if skip_sku:
            # 单规格商品：页面根本没有 SKU 选择器，直接按数量加购（没有「默认 SKU」风险）
            pass
        else:
            _t_sku = time.monotonic()
            resp = self.send({"cmd": "select-sku", "selections": selections, "light": True})
            _sku_ms = int((time.monotonic() - _t_sku) * 1000)
            if resp.get("status") == "error":
                err = resp.get("error") or {}
                return {
                    "ok": False,
                    "code": err.get("code") or "SELECT_SKU_FAILED",
                    "message": err.get("message") or "选择 SKU 失败",
                    "detail": err,
                    "screenshot": self.persist_screenshot(resp.get("screenshot")),
                }

            data = resp.get("data") or {}
            groups = data.get("sku_groups") or []
            final_price = data.get("final_price")
            missing = groups_without_selection(groups)
            if missing:
                return {
                    "ok": False,
                    "code": "SKU_INCOMPLETE",
                    "message": "还有 SKU 组未选中，已停止加购。请在页面里补齐或手动选择。",
                    "missing": missing,
                    "groups": groups,
                    "screenshot": self.persist_screenshot(resp.get("screenshot")),
                }
            # 选定 SKU 之后才知道这份到底多少只：以页面已选值为准，重算加购份数
            sel_vals = [v.get("text", "") for g in groups for v in (g.get("values") or []) if v.get("selected")]
            pack, pack_src = _pack_from_texts(sel_vals, explicit_only=False, allow_generic=True)
            if not pack:
                pack, pack_src = _pack_from_texts([s.get("value", "") for s in selections], explicit_only=False, allow_generic=True)

        eff_pack = pack or pack_hint
        if auto_qty:
            qty, eff_pack, actual_pcs = _plan_qty(line.get("qty"), eff_pack)
        else:
            actual_pcs = int(qty) * int(eff_pack or 1)
            eff_pack = int(eff_pack or 1)
        qty = max(1, int(qty))

        # 选完 SKU 的价是「每份价」。和标题价差 5 倍以上，说明标题那 ¥1 是引流价
        # （常见于「多个规格一个链接」，贵的规格才是真实价），记下来给界面/报告看。
        price_shift = None
        if final_price is not None and price_hint:
            try:
                if float(final_price) >= float(price_hint) * 5:
                    price_shift = round(float(final_price) / float(price_hint), 1)
            except (TypeError, ValueError):
                price_shift = None

        # 三道钱闸门（每只单价 / 单行金额 / 超量）走公共实现，与演练共用同一套判断。
        # 单价可以用标题价兜底（选完 SKU 没回报价时）；金额只认选完 SKU 的实价 ——
        # 标题那个 ¥1 常常是引流价，按它算金额等于给整袋报价开后门。
        _guard = _money_guards(
            line,
            final_price if final_price is not None else price_hint,
            eff_pack,
            qty,
            actual_pcs,
            auto_qty=auto_qty,
            amount=final_price,
        )
        if _guard:
            if _guard.get("code") == "COST_TOO_HIGH":
                _guard["price_shift"] = price_shift
            _guard["screenshot"] = self.persist_screenshot(resp.get("screenshot"))
            return {"ok": False, **_guard}

        _t_add = time.monotonic()
        # light：证据图用 jpeg + 不拉整页文本，单行省 2–3s
        resp = self.send({"cmd": "cart-add", "qty": int(qty), "light": True})
        _add_ms = int((time.monotonic() - _t_add) * 1000)
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_ADD_FAILED",
                "message": err.get("message") or "加购失败",
                "detail": err,
                "screenshot": self.persist_screenshot(resp.get("screenshot") or err.get("screenshot")),
            }

        data = resp.get("data") or {}
        if not data.get("cart_added"):
            return {
                "ok": False,
                "code": "CART_UNCONFIRMED",
                "message": "已点击加入购物车，但未在页面/角标/购物车里确认到商品。请打开购物车人工核对，不要重复点击。",
                "screenshot": self.persist_screenshot(resp.get("screenshot")),
                "groups": groups,
            }
        # 单规格商品没有 SKU 可选，select-sku 拿不到价 —— 用详情页的价兜底，否则金额永远是空的
        if final_price is None and price_hint is not None:
            final_price = price_hint

        return {
            "ok": True,
            "cart_added": data.get("cart_added"),
            "confirmed": data.get("confirmed"),
            "cart_verified_via": data.get("cart_verified_via"),
            "qty": data.get("qty") or qty,
            "qty_sent": qty,
            "qty_confirmed": data.get("qty_confirmed"),
            "pack_size": eff_pack if eff_pack and eff_pack > 1 else None,
            "pack_source": pack_src,
            "actual_pcs": actual_pcs,
            "bom_qty": line.get("qty"),
            "final_price": final_price,
            "price_shift": price_shift,
            # 实付金额 = 页面每份单价 × 买的份数（不是单只价，单只价另算）
            "cost": round(float(final_price) * int(qty), 2) if final_price is not None else None,
            "unit_price": round(final_price / eff_pack, 6) if (final_price and eff_pack > 1) else final_price,
            "sku_verified": data.get("sku_verified"),
            "sku_selection_count": data.get("sku_selection_count"),
            "select_ms": _sku_ms,
            "add_ms": _add_ms,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
            "groups": groups,
        }

    def cart_view(self) -> dict:
        resp = self.send({"cmd": "cart-view"})
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_VIEW_FAILED",
                "message": err.get("message") or "读取购物车失败",
                "screenshot": self.persist_screenshot(err.get("screenshot") or resp.get("screenshot")),
            }
        data = resp.get("data") or {}
        return {
            "ok": True,
            "cart_item_count": data.get("cart_item_count"),
            "cart_empty": bool(data.get("cart_empty")),
            # I4：逐行明细一起回，结算前核对/定位要删的行全靠这个
            "rows": data.get("rows") or [],
            "rows_page": data.get("rows_page"),
            "rows_total": data.get("rows_total"),
            "pages_read": data.get("pages_read"),
            "rows_degraded": data.get("rows_degraded"),
            "rows_selector": data.get("rows_selector"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    # ---------------- 删除购物车 ----------------
    def cart_remove(self, keyword: str | None = None, remove_all: bool = False) -> dict:
        """Delete matching cart rows (or everything) — the only destructive command."""
        req: dict = {"cmd": "cart-remove"}
        if keyword:
            req["keyword"] = keyword
        if remove_all:
            req["all"] = True
        resp = self.send(req)
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_REMOVE_FAILED",
                "message": err.get("message") or "删除购物车条目失败",
                "matched": err.get("matched"),
                "screenshot": self.persist_screenshot(err.get("screenshot") or resp.get("screenshot")),
            }
        data = resp.get("data") or {}
        return {
            "ok": True,
            "removed": data.get("removed"),
            "before": data.get("before"),
            "after": data.get("after"),
            "matched": data.get("matched") or [],
            "confirm_clicked": data.get("confirm_clicked"),
            "verified": data.get("verified"),
            "pages_touched": data.get("pages_touched"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    def cart_remove_last_batch(self, dry: bool = False) -> dict:
        """只删「最近一次批量加购」的那些商品，用户自己加的保持不动。

        dry=True 时执行层只扫描并返回计划（会删哪几件、跳过哪几件），
        一个按钮都不点 —— 前端先用它给用户看清，再决定要不要真删。

        注意「最近一次」= 进度文件里那一批，也就是**刚跑完的这批**。
        用户嘴里的「上一批」通常指更早那份 BOM，两者不是一回事，
        所以计划里必须带上这批的开始/结束时间和行数，让人一眼认出删的是哪批。
        """
        prog = load_batch_progress() or {}
        targets = batch_removal_targets(prog)
        from_archive = False
        if not targets and not (prog.get("lines") or {}):
            # 进度被「重跑 / 重置」整个清过，商品可能还在车里 —— 用底档再认一次。
            # 只在这一批**根本没有记录**时才翻底档：进度里明明有这批（哪怕全失败/
            # 已删车）却去删更早那批，就成了「按错批次的名单删车」。
            prog = load_archived_progress() or {}
            targets = batch_removal_targets(prog)
            from_archive = bool(targets)
        meta = {
            "started_at": prog.get("started_at"),
            "finished_at": prog.get("finished_at"),
            "total": prog.get("total"),
            "fingerprint": prog.get("fingerprint") or "",
            "from_archive": from_archive,
        }
        if not targets:
            return {
                "ok": False,
                "code": "NO_BATCH_RECORD",
                "message": "没有可用的批量加购记录：上次批量没有成功加购的行",
                "batch": meta,
                "delete": [],
                "skip": [],
                "missing": [],
                "merged": [],
            }
        resp = self.send(
            {"cmd": "cart-remove", "items": targets, "dry": dry}, timeout=240
        )
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_REMOVE_FAILED",
                "message": err.get("message") or "删除购物车条目失败",
                "batch": meta,
                "screenshot": self.persist_screenshot(
                    err.get("screenshot") or resp.get("screenshot")
                ),
            }
        data = resp.get("data") or {}
        removed = data.get("removed")
        invalidated = {"updated": 0}
        if not dry and isinstance(removed, int) and removed > 0:
            # 车删了，进度必须跟着作废：否则「继续加购」会按 ok 全跳过。
            # all_ok 只在「目标覆盖了这批全部 ok 行、且全删掉了」时成立，
            # 有跳过/有没认出来的行就退回按标题逐个匹配，宁可少改不可多改。
            ok_lines = sum(
                1
                for v in (prog.get("lines") or {}).values()
                if (v or {}).get("status") == "ok"
            )
            invalidated = invalidate_removed_progress(
                data.get("removed_titles") or [],
                all_ok=removed >= len(targets) and len(targets) >= ok_lines,
            )
            if invalidated.get("updated"):
                self._mark_removed_in_memory()
        return {
            "ok": True,
            "dry": bool(data.get("dry")),
            "batch": meta,
            "targets": data.get("targets"),
            "delete": data.get("delete") or [],
            "skip": data.get("skip") or [],
            "missing": data.get("missing") or [],
            "merged": data.get("merged") or [],
            "removed": removed,
            "removed_titles": data.get("removed_titles") or [],
            "progress_invalidated": invalidated.get("updated") or 0,
            "before": data.get("before"),
            "after": data.get("after"),
            "verified": data.get("verified"),
            "cart_rows": data.get("cart_rows"),
            "cart_degraded": data.get("cart_degraded"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
        }

    def _mark_removed_in_memory(self) -> int:
        """把内存里这批的 ok 行同步改成 removed，页面才不会继续显示「已加购」。

        以进度文件为准逐个对：只删了部分行（其余被跳过）时，没删的行仍留在车里，
        界面不能把整批都写成「已删车」。
        """
        prog = load_batch_progress() or {}
        removed_ids = {
            str(k)
            for k, v in (prog.get("lines") or {}).items()
            if (v or {}).get("status") == "removed"
        }
        changed = 0
        with self._state_lock:
            for r in self._batch.get("results") or []:
                if (r or {}).get("status") != "ok":
                    continue
                if str(r.get("line_no")) not in removed_ids:
                    continue
                r["status"] = "removed"
                r["message"] = "已从购物车删除，需要重新加购"
                changed += 1
            if changed:
                self._batch["ok_count"] = 0
                self._batch["cart_invalidated"] = changed
        return changed

    def cart_remove_line(self, line_no) -> dict:
        """把「这一行买的东西」从购物车删掉，其它行不动。

        和整批删车共用执行层（cart-remove items）：只传这一个目标，按商品 id
        精确匹配；删完只把这一行改成 removed（待加购），不会连坐整批。
        """
        if self._batch.get("running"):
            raise SessionError("批量任务正在跑，等它跑完再删", "BATCH_RUNNING")
        if self._proc is None or self._proc.poll() is not None:
            raise SessionError(
                "淘宝会话未启动（或已退出），先点右上角「启动淘宝会话」", "NO_SESSION"
            )
        if self._browser_gone or self._browser_unreachable():
            raise SessionError("淘宝浏览器已关闭，先点「启动淘宝会话」重建会话", "BROWSER_GONE")

        picked = cart_target_for_line(load_batch_progress() or {}, line_no)
        if not picked["ok"]:
            raise SessionError(picked["message"], picked["code"])
        target = picked["target"]

        resp = self.send({"cmd": "cart-remove", "items": [target]}, timeout=240)
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            raise SessionError(
                err.get("message") or "删除购物车条目失败",
                err.get("code") or "CART_REMOVE_FAILED",
            )
        data = resp.get("data") or {}
        removed = int(data.get("removed") or 0)
        # 没删到不等于出错：可能用户已经手动删过了。无论哪种，进度都必须和
        # 购物车对齐 —— 否则「继续加购」还会按 ok 跳过这一行，显示已买实际没买。
        msg = (
            "已从购物车删除，需要重新加购"
            if removed
            else "购物车里没找到这一件（可能已手动删除），已改回待加购"
        )
        changed = mark_line_removed(line_no, msg)
        if changed:
            self._mark_removed_in_memory()
        self._append_log(
            f"[line] 第 {line_no} 行从购物车删车：removed={removed} "
            f"{data.get('message') or ''}"
        )
        return {
            "ok": True,
            "line_no": line_no,
            "removed": removed,
            "title": target.get("title") or "",
            "before": data.get("before"),
            "after": data.get("after"),
            "skip": data.get("skip") or [],
            "missing": data.get("missing") or [],
            "progress_invalidated": changed,
            "code": data.get("code"),
            "message": data.get("message") or msg,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
        }

    # ---------------- 批量加购 ----------------
    def batch_status(self) -> dict:
        with self._state_lock:
            data = json.loads(json.dumps(self._batch, ensure_ascii=False, default=str))
        # 金额汇总：只有拿到页面单价的行才计入，避免把 None 当 0 摊平均价
        rows = data.get("results") or []
        cost, n_cost, okn = 0.0, 0, 0
        for r in rows:
            if r.get("status") != "ok":
                continue
            okn += 1
            c = r.get("cost")
            if isinstance(c, (int, float)):
                cost += float(c)
                n_cost += 1
        data["cost_total"] = round(cost, 2)
        data["cost_lines"] = n_cost
        data["ok_count"] = okn
        data["cost_avg"] = round(cost / n_cost, 2) if n_cost else None
        # 服务重启后内存里的 batch 是空的，但进度文件还在 —— 从进度里把累计金额捞出来，
        # 免得重启一次「合计花费」就变 0。按文件 mtime 缓存，别每秒轮询都读盘。
        if not rows:
            try:
                mt = PROGRESS_FILE.stat().st_mtime
            except OSError:
                mt = None
            cache = getattr(self, "_cost_cache", None)
            if mt is not None and (cache is None or cache[0] != mt):
                prior = load_batch_progress().get("lines") or {}
                c, n, o = 0.0, 0, 0
                for v in prior.values():
                    if (v or {}).get("status") != "ok":
                        continue
                    o += 1
                    cv = (v or {}).get("cost")
                    if isinstance(cv, (int, float)):
                        c += float(cv)
                        n += 1
                cache = (mt, round(c, 2), n, o)
                self._cost_cache = cache
            if cache:
                data["cost_total"] = cache[1]
                data["cost_lines"] = cache[2]
            if cache and not okn:
                data["ok_count"] = cache[3]
            data["cost_from_progress"] = True
        # I7：明确告诉前端「有没有可续跑的进度」「是不是真的有线程在跑」
        data["has_progress"] = bool(data.get("results"))
        data["resumable"] = (not data.get("running")) and bool(data.get("results"))
        data["restored_from_disk"] = bool(data.get("restored"))
        return data

    def _batch_patch(self, **patch):
        with self._state_lock:
            self._batch.update(patch)

    def _batch_line_patch(self, index: int, **patch):
        with self._state_lock:
            if 0 <= index < len(self._batch["results"]):
                self._batch["results"][index].update(patch)

    def start_batch(
        self,
        lines: list,
        prefer_cheap: bool = True,
        max_candidates: int = 2,
        resume: bool = True,
        dry_run: bool = False,
        redo: bool = False,
        redo_lines: list | None = None,
    ) -> dict:
        """启动批量加购。

        resume=True（默认）时读 runs/batch-progress.json，把上次已经「已加入购物车」的行
        直接标成 ok 并跳过 —— 暂停/停止/重启后接着跑，不会从头重复加购。

        但「跳过」只在用户**没说要重跑**时才成立，所以有两个显式开关：

        - redo=True：整批重跑。忽略并清空旧进度，一行都不跳过。
        - redo_lines=[2,15]：只重跑这几行，其余行照旧续跑跳过。

        之所以要显式开关：以前 resume 是唯一行为，用户觉得某行买错了想重跑，
        点一下按钮，done_map 把 ok 行全跳过，瞬间「跑完」，一行没动 —— 用户
        以为重跑了，实际什么都没发生。所以：
        1) 全部可跑行都已定局时，resume 不再静默空跑，直接抛 BATCH_ALL_DONE；
        2) 重跑必须走 redo / redo_lines，界面上也要弹「会重复加购」的确认。
        """
        if not isinstance(lines, list) or not lines:
            raise SessionError("没有可执行的行", "BATCH_EMPTY")
        # 浏览器被关掉时跑批只会得到一堆「搜索词没有可用结果」的假失败，
        # 直接拦住并说清楚怎么办。
        if self._browser_gone or self._browser_unreachable():
            raise SessionError(
                "淘宝浏览器已关闭，先点「启动淘宝会话」重建会话（会自动重开浏览器）",
                "BROWSER_GONE",
            )
        fp = batch_fingerprint(lines)
        # 必须在覆盖 batch-lines.json 之前读：里面存的是「上一次开跑」的 BOM 指纹。
        # 只看进度文件不够 —— 用户可能已经手动点过「重置进度」，但界面内存里
        # 还留着上一批的 ok_count，照样会被骗进一次整批空跑。
        prev_lines_meta = load_batch_lines_meta()
        force = {str(x) for x in (redo_lines or []) if str(x).strip()}
        prior = load_batch_progress() if (resume or force) else {}
        stale_progress = False
        if redo:
            # 整批重跑：进度必须先清。_save_progress 是「旧文件 + 本轮」合并写，
            # 不清的话本轮没定局的行会把上一轮的 ok 继承回来，下次续跑又跳过。
            prior = {}
            clear_batch_progress()
            _log("[batch] 用户要求整批重跑，旧进度已清空")
        if resume and (prior.get("lines") or {}):
            belongs, why = progress_belongs_to(prior, lines)
            prev_fp = prev_lines_meta.get("fingerprint") or ""
            # 新进度文件自带明确指纹时以它为准；只有老进度（无 fingerprint）
            # 才需要拿 batch-lines 里「上次开跑的 BOM」当佐证。
            if not prior.get("fingerprint") and prev_fp and prev_fp != fp:
                belongs, why = False, "上次开跑的 BOM 指纹"
            if not belongs:
                stale_progress = True
                prior = {}
                clear_batch_progress()
                _log(f"[batch] 检测到 BOM 已更换（{why} 不一致），旧进度作废，本批从头跑")
        save_batch_lines(lines)
        done_map = done_map_from_progress(prior, redo_lines=redo_lines)
        if resume and not redo:
            # 全部可跑行都已定局 → 再跑一遍就是纯重复加购。以前这里静默全跳过，
            # 用户看到进度条瞬间满格、报告「已跑完」，以为重跑了。
            # 「不买」的行不算可跑：标记后这批只剩不买的行时，应该报空而不是全跳过。
            runnable = [
                ln for ln in lines if line_has_query(ln) and not ln.get("dont_buy")
            ]
            if runnable and all(str(ln.get("line_no")) in done_map for ln in runnable):
                raise SessionError(
                    f"这批 {len(runnable)} 行上次已全部跑完，直接再跑会重复加购。"
                    "要重跑请点「重跑问题行」或「重新跑全部」"
                    "（重跑前建议先删掉上一批加进购物车的商品）",
                    "BATCH_ALL_DONE",
                )

        results, resumed = [], 0
        for ln in lines:
            key = str(ln.get("line_no"))
            head = (ln.get("decision") or {}).get("headline") or ln.get("mpn") or ""
            prev = done_map.get(key)
            if prev:
                resumed += 1
                _uncertain = (prev.get("status") or "") == "uncertain"
                results.append(
                    {
                        "line_no": ln.get("line_no"),
                        "headline": head or prev.get("headline") or "",
                        "qty": ln.get("qty"),
                        "status": "uncertain" if _uncertain else "ok",
                        "message": (
                            "上次已点加购但页面没确认，跳过以免重复加购，请到购物车核对"
                            if _uncertain
                            else "上次已完成（续跑跳过）"
                        ),
                        "attempts": [],
                        "resumed": True,
                        "candidate": prev.get("candidate"),
                        "price": prev.get("price"),
                        "final_price": prev.get("final_price") if prev.get("final_price") is not None else prev.get("price"),
                        "cost": prev.get("cost"),
                        "unit_price": prev.get("unit_price"),
                        "pack_size": prev.get("pack_size"),
                        "actual_pcs": prev.get("actual_pcs"),
                        "screenshot": prev.get("screenshot"),
                        # 续跑行也要把「选了哪个 SKU / 靠什么确认加购」带回来，
                        # 否则界面上这条记录只剩金额，导出 CSV 的 SKU、确认方式两列是空的。
                        "sku": prev.get("sku") or [],
                        "cart_verified_via": prev.get("cart_verified_via"),
                        "query": prev.get("query"),
                    }
                )
            else:
                results.append(
                    {
                        "line_no": ln.get("line_no"),
                        "headline": head,
                        "qty": ln.get("qty"),
                        "status": "pending",
                        "message": "",
                        "attempts": [],
                    }
                )
        # 开跑前清一轮：只留进度里点名的证据图（上一批的），更早的、试跑的、手动采集的全删
        with self._state_lock:
            already = bool(self._batch.get("running"))
        if not already:
            self.cleanup_screenshots()
        with self._state_lock:
            if self._batch.get("running"):
                raise SessionError("批量加购任务已在运行，等它结束或先停止", "BATCH_RUNNING")
            self._batch = {
                "running": True,
                "total": len(lines),
                "done": resumed,
                "index": -1,
                "phase": (
                    "演练准备中（不点加购）"
                    if dry_run
                    else (
                        "准备中（新 BOM，旧进度已作废）"
                        if stale_progress
                        else (
                            "准备中（重新跑全部，旧进度已清空）"
                            if redo
                            else (
                                f"准备中（重跑 {len(force)} 行，其余续跑跳过）"
                                if force
                                else (f"准备中（续跑，跳过已完成 {resumed} 行）" if resumed else "准备中")
                            )
                        )
                    )
                ),
                "dry_run": bool(dry_run),
                "prefer_cheap": bool(prefer_cheap),
                "results": results,
                "resumed": resumed,
                "fingerprint": fp,
                "stale_progress_dropped": stale_progress,
                # 整批重跑时进度落盘不能继承旧行（_save_progress 是合并写）
                "drop_prior_progress": bool(redo),
                "redo": bool(redo),
                "redo_line_nos": sorted(int(x) for x in force if str(x).isdigit()),
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "stopped_reason": None,
            }
        threading.Thread(
            target=self._run_batch,
            args=(list(lines), bool(prefer_cheap), max(1, int(max_candidates)), bool(dry_run)),
            daemon=True,
        ).start()
        return self.batch_status()

    def stop_batch(self) -> dict:
        with self._state_lock:
            if self._batch.get("running"):
                self._batch["stopped_reason"] = "用户请求停止（当前行完成后生效）"
        self._save_progress()
        return self.batch_status()

    # ---------------- 单行执行 ----------------

    def run_line(self, line_no: int | str | None = None, index: int | None = None) -> dict:
        """只跑一行，与跑批同一套逻辑（搜索→评分→SKU→加购）。

        场景：整批里个别行失败（换词/临时验证/改完搜索词），整批重跑会把
        已成功的行再加一遍车。单行跑完只落这一行的进度，其余结论原样不动。
        """
        lines = load_batch_lines()
        if not lines:
            raise SessionError("还没有可跑的行：先投放并分析 BOM", "NO_LINES")
        if self._batch.get("running"):
            raise SessionError("批量任务正在跑，等它跑完再单独跑这一行", "BATCH_RUNNING")
        if self._proc is None or self._proc.poll() is not None:
            raise SessionError("淘宝会话未启动（或已退出），先点右上角「启动淘宝会话」", "NO_SESSION")
        if self._browser_gone or self._browser_unreachable():
            raise SessionError("淘宝浏览器已关闭，先点「启动淘宝会话」重建会话", "BROWSER_GONE")

        target = None
        if line_no is not None:
            for ln in lines:
                if str(ln.get("line_no")) == str(line_no):
                    target = ln
                    break
        elif index is not None and 0 <= int(index) < len(lines):
            target = lines[int(index)]
        if target is None:
            raise SessionError("找不到这一行（可能换过 BOM）", "NO_LINE")
        _skip = _line_skip_reason(target)
        if _skip:
            raise SessionError(f"这一行跑不了：{_skip}", "LINE_SKIPPED")

        self._append_log(f"[line] 单独执行第 {target.get('line_no')} 行（不跑整批）")
        _t0 = time.monotonic()
        try:
            res = self._add_line_to_cart(target, True, 2)
        except SessionError as exc:
            res = {
                "line_no": target.get("line_no"),
                "headline": (target.get("decision") or {}).get("headline") or target.get("mpn") or "",
                "qty": target.get("qty"),
                "attempts": [],
                "status": "failed",
                "message": str(exc),
                "code": exc.code,
            }
            errors.enrich_result(res, exc, stage="single-line", line_no=target.get("line_no"))
        except Exception as exc:  # noqa: BLE001
            # 单行执行也一样：未知异常要有分类和 traceback，别让前端只收到 500。
            errors.log_exception(
                exc, self._append_log, stage="single-line",
                line_no=target.get("line_no"), batch_id=self._batch.get("batch_id"),
            )
            res = {
                "line_no": target.get("line_no"),
                "headline": (target.get("decision") or {}).get("headline") or target.get("mpn") or "",
                "qty": target.get("qty"),
                "attempts": [],
                "status": "failed",
                "message": f"未预期错误：{exc}",
            }
            errors.enrich_result(res, exc, stage="single-line", line_no=target.get("line_no"))
        res["total_ms"] = int((time.monotonic() - _t0) * 1000)
        # 与跑批同一套口径：BOM 自身矛盾 → 不自动算成功；采购依据跟着结果落库
        if res.get("status") in ("ok", "failed") and res.get("attempts"):
            _rev = _review_reason(target)
            if _rev:
                res["status"] = "uncertain"
                res["needs_review"] = True
                res["message"] = _rev
        res["basis"] = _procurement_basis(target, res)
        self._append_log(
            f"[line] 第 {target.get('line_no')} 行 {res.get('status')} "
            f"total={res['total_ms'] / 1000:.1f}s {res.get('message') or ''}"
        )
        # 内存里的批次结果同步更新：页面不用重启就能看到这行的新结论
        with self._state_lock:
            hit = False
            for rec in self._batch.get("results") or []:
                if str(rec.get("line_no")) == str(target.get("line_no")):
                    rec.update(res)
                    hit = True
                    break
            if not hit:
                self._batch.setdefault("results", []).append(res)
                if not self._batch.get("total"):
                    self._batch["total"] = len(lines)
        self._save_single_result(res, total_lines=len(lines))
        self._sync_single_line_state(res, total_lines=len(lines))
        return res

    def _sync_single_line_state(self, res: dict, total_lines: int = 0) -> None:
        """单行跑完，把批次计数器一起改对（复测 P1）。

        以前只往 results 里塞一条，done 还是 0、phase 还是「未开始」：
        页面同一屏写着「第 1 行已加购」和 0/1 未开始，刷新后更没法判断进度。
        单行 ≠ 整批完成，所以：
        - done 按「已定局行数」算（与跑批同一口径），total 跟当前 BOM 行数对齐；
        - phase 明写「单行执行」，并置 single_line 标记给前端；
        - 只有所有行都定局才写 finished_at / 「已结束」，否则保持未完成态。
        """
        ln = res.get("line_no")
        status = str(res.get("status") or "")
        settled_set = set(SETTLED_STATUS)
        with self._state_lock:
            results = list(self._batch.get("results") or [])
            done = sum(1 for r in results if str(r.get("status") or "") in settled_set)
            total = max(int(total_lines or 0), int(self._batch.get("total") or 0), len(results))
            all_done = total > 0 and done >= total
            label = {
                "ok": "已加购",
                "failed": "失败",
                "uncertain": "待核对",
                "skipped": "跳过",
                "removed": "已删车",
            }.get(status, status or "已执行")
            self._batch["total"] = total
            self._batch["done"] = done
            self._batch["single_line"] = {
                "line_no": ln,
                "status": status,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            if all_done:
                self._batch["phase"] = f"已结束（单行重跑第 {ln} 行：{label}）"
                self._batch["finished_at"] = self._batch.get("finished_at") or datetime.now().isoformat(
                    timespec="seconds"
                )
            else:
                # 还剩行没跑完：不许说「已结束」，否则用户以为整批完成了
                self._batch["phase"] = (
                    f"单行执行：第 {ln} 行{label}（整批还差 {max(0, total - done)} 行）"
                )
                self._batch["finished_at"] = None
            self._batch["stopped_reason"] = None

    def _save_single_result(self, res: dict, total_lines: int = 0) -> None:
        """单行结果并入进度文件（只覆盖这一行）。"""
        if self._batch.get("dry_run"):
            return
        ln = res.get("line_no")
        if ln is None:
            return
        prior = load_batch_progress() or {}
        rows = dict(prior.get("lines") or {})
        rows[str(ln)] = _result_progress_payload(res)
        fp = (
            self._batch.get("fingerprint")
            or prior.get("fingerprint")
            or batch_fingerprint(load_batch_lines())
        )
        save_batch_progress(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "total": prior.get("total") or total_lines or len(rows),
                "lines": rows,
                "fingerprint": fp,
                "started_at": prior.get("started_at") or self._batch.get("started_at"),
                "finished_at": prior.get("finished_at") or self._batch.get("finished_at"),
            }
        )

    def _save_progress(self):
        """把已定局（ok/failed/uncertain）的行按 line_no 存盘，供下次续跑跳过。"""
        # 演练批次的结论**一律不落盘**。
        # dry-run 的 ok 意思是「选型/闸门都过了」，不是「购物车里有了」。
        # 落盘之后：页面会显示「已加购 16 行」，下次「继续加购」把它们当已买跳过，
        # 「删除上一批」还会拿这份名单去删车 —— 等于把演练当成了真单。
        # 2026-11-16 实测踩到：stop_batch / stop() 都会调这里，演练结果被写进
        # batch-progress.json，重启后页面挂着 16 行假的「已加购」。
        if self._batch.get("dry_run"):
            return
        prior_data = load_batch_progress() or {}
        # 整批重跑时不能继承旧行：否则本轮没跑到的行会把上一轮的 ok 带回来，
        # 下次「继续加购」又把它们当已完成跳过 —— 重跑等于没重跑。
        prior_lines = {} if self._batch.get("drop_prior_progress") else (prior_data.get("lines") or {})
        with self._state_lock:
            rows = {}
            for r in self._batch.get("results") or []:
                # skipped 也落盘：不买/无搜索词是**人工定局**，不写的话刷新后
                # 这两行又变回「没跑过」，用户以为漏了（14/28 行实测）。
                if r.get("status") not in SETTLED_STATUS:
                    continue
                ln = r.get("line_no")
                if ln is None:
                    continue
                payload = _result_progress_payload(r)
                if r.get("resumed"):
                    # 续跑跳过的行：结论是从进度文件里读出来的，别拿它覆盖上一轮的
                    # 真实记录（message 会变成「续跑跳过」、耗时全变 null）。
                    # 只有结论真的变了（收尾对账把 ok 降成 uncertain）才写回，
                    # 而且明细沿用上一轮，免得把 SKU/金额/截图抹掉。
                    prev = prior_lines.get(str(ln)) or {}
                    if (prev.get("status") or "") == payload["status"]:
                        continue
                    for key, val in prev.items():
                        if key in ("status", "message"):
                            continue
                        if val not in (None, "", [], {}):
                            payload[key] = val
                rows[str(ln)] = payload
            data = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "total": self._batch.get("total") or 0,
                "started_at": self._batch.get("started_at"),
                "finished_at": self._batch.get("finished_at"),
                "lines": rows,
            }
        # 合并写，不是覆盖写。
        # 曾经踩过：会话停止时 _reset_state 先把内存里的 batch 清空，
        # _run_batch 的 finally 随后把「空 rows」落盘，把辛苦跑出来的
        # 几十行进度全抹了 —— 下次「继续加购」就当成新任务从第 1 行重来，
        # 购物车直接翻倍。所以这里先把旧文件读出来合并。
        if not rows:
            return
        merged = dict(prior_lines)
        merged.update(rows)  # 本轮的结论覆盖旧结论（失败转成功要生效）
        save_batch_progress(
            {
                "updated_at": data["updated_at"],
                "total": data["total"] or prior_data.get("total") or 0,
                "lines": merged,
                # 绑定 BOM 身份：换表后这份进度必须整体作废，不能按行号套用
                "fingerprint": self._batch.get("fingerprint") or prior_data.get("fingerprint") or "",
                # I7：时间戳也落盘，重启后状态机能还原「这轮什么时候开始/结束」
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
            }
        )

    def _precheck_cart_limit(self, dry_run: bool = False) -> None:
        """开跑前看购物车件数：接近淘宝上限就别白跑了。

        实测上限约 200 行，满了之后每一行都会失败；与其跑 20 分钟全红，
        不如开跑 5 秒就看一眼（cart-view 一次）。
        """
        if dry_run:
            return
        try:
            cv = self.send({"cmd": "cart-view"}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            _log(f"[batch] cart precheck skipped: {exc}")
            return
        data = cv.get("data") or {}
        count = data.get("cart_item_count")
        with self._state_lock:
            self._batch["cart_count_before"] = count
        if not isinstance(count, int):
            return
        if count >= CART_LIMIT_STOP:
            with self._state_lock:
                self._batch["stopped_reason"] = (
                    f"购物车已有 {count} 件，接近淘宝上限（约 200 行），先清理再跑"
                )
            self._append_log(
                f"[batch] 购物车 {count} 件 ≥ 阈值 {CART_LIMIT_STOP}，不启动（先清理购物车）"
            )
            return
        if count >= CART_LIMIT_WARN:
            self._append_log(
                f"[batch] 购物车已有 {count} 件，接近上限（约 200 行），建议先清理"
            )

    def _run_batch(self, lines: list, prefer_cheap: bool, max_candidates: int, dry_run: bool = False):
        ran = 0
        _unc_streak = 0  # 连续 uncertain 计数：软限流的早期信号
        _slow_streak = 0  # 连续「加购超过 5s」计数：限流前兆
        # 开跑先把调试浏览器藏到屏幕外：逐行搜索/点加购时 Chromium 会抢前台，
        # 用户正在用的软件被盖住（实测反馈）。藏起来后连"盖住"的机会都没有，
        # 加上 send() 期间的前台守卫，抢了也会在 0.2s 内还回去。
        try:
            self.hide_browser()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[batch] 隐藏浏览器窗口失败（不影响跑批）：{exc}")
        try:
            # 开跑前预检购物车件数：淘宝上限约 200 行，满了一行都加不进去。
            # 这里直接看一次，比跑 20 分钟全失败再回来查划算。
            self._precheck_cart_limit(dry_run=dry_run)
            if self._batch.get("stopped_reason"):
                return
            for i, line in enumerate(lines):
                _skip_why = ""
                # 浏览器被关掉时，每一行都会在搜索环节拿到「没有可用结果」——
                # 19 行能一路跑完，最后报「17 行搜不到」，看着像选型失败，
                # 实际是环境没了。这种情况必须整批中止并说清楚，别让人去改规则。
                # 只有「会话本来在跑」才探端口：没起会话的场合（单测、离线复算）
                # 端口本来就不通，按「浏览器没了」处理会误伤。
                _sess_running = self._proc is not None and self._proc.poll() is None
                if self._browser_gone or (_sess_running and self._browser_unreachable()):
                    with self._state_lock:
                        self._batch["stopped_reason"] = (
                            "调试浏览器已关闭，本批中止（重新「启动淘宝会话」后点「继续加购」接着跑）"
                        )
                    self._append_log(
                        "[batch] 调试浏览器已关闭，中止本批 —— 继续跑只会把每行记成「搜不到」"
                    )
                    break
                with self._state_lock:
                    if self._batch.get("stopped_reason"):
                        break
                    if self._batch["results"][i].get("resumed"):
                        # 上次已经加过的行：直接跳过，不重复下单
                        continue
                if not (
                    line.get("query_plan")
                    or (line.get("decision") or {}).get("search_queries")
                ) or line.get("dont_buy"):
                    # 残缺行没有搜索词可搜，白等一轮再记「失败」；「不买」是
                    # 人工的采购决策 —— 两种都直接标「跳过」并写清原因。
                    _skip_why = _line_skip_reason(line)
                    self._batch["results"][i].update(
                        status="skipped", message=f"跳过：{_skip_why}"
                    )
                else:
                        self._batch["index"] = i
                        self._batch["phase"] = f"第 {i + 1}/{len(lines)} 行"
                        self._batch["results"][i]["status"] = "running"
                if _skip_why:
                    self._append_log(
                        f"[batch] line #{line.get('line_no')} 跳过：{_skip_why}"
                    )
                    continue
                if ran:
                    # 行间留一点间隔：连续狂翻商品页最容易触发滑块验证。
                    # 淘宝的限速看着是「短窗口内导航次数」——实测 1.0s/行 跑十行左右
                    # 就会弹验证。所以光调行间隔不够，还得每 N 行整段冷却一次。
                    time.sleep(LINE_GAP_S)
                    if ran % COOLDOWN_EVERY == 0:
                        self._append_log(
                            f"[batch] 风控冷却 {COOLDOWN_SEC}s（已跑 {ran} 行）"
                        )
                        self._batch["phase"] = f"风控冷却 {COOLDOWN_SEC}s（已跑 {ran} 行）"
                        _cd = COOLDOWN_SEC
                        while _cd > 0 and not self._batch.get("stopped_reason"):
                            time.sleep(min(5, _cd))
                            _cd -= 5
                ran += 1
                _verify_retries = 0
                _verify_human_retries = 0
                _stop_after = False
                while True:
                    try:
                        _t0 = time.monotonic()
                        res = self._add_line_to_cart(line, prefer_cheap, max_candidates, dry_run=dry_run)
                        res["total_ms"] = int((time.monotonic() - _t0) * 1000)
                        # BOM 自身矛盾（名称列 vs 料号）且名称值不是标准档：不自动算成功。
                        # 这类行「选型能跑通」，但买下去等于把 BOM 的错抄进购物车。
                        # 没买到时同样要改口径：真实原因是「5kΩ 不是标准档、商家根本没有」，
                        # 而不是「候选功率太大」——后者会把人引去手动加一个错的值。
                        if res.get("status") in ("ok", "failed") and res.get("attempts"):
                            _rev = _review_reason(line)
                            if _rev:
                                res["status"] = "uncertain"
                                res["needs_review"] = True
                                res["message"] = _rev
                        self._append_log(
                            f"[batch] line #{line.get('line_no')} {res.get('status')} "
                            f"total={res['total_ms'] / 1000:.1f}s "
                            f"search={(res.get('search_ms') or 0) / 1000:.1f}s "
                            f"open={(res.get('open_ms') or 0) / 1000:.1f}s "
                            f"sku={(res.get('select_ms') or 0) / 1000:.1f}s "
                            f"add={(res.get('add_ms') or 0) / 1000:.1f}s"
                        )
                        break
                    except SessionError as exc:
                        res = {
                            "line_no": line.get("line_no"),
                            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
                            "qty": line.get("qty"),
                            "attempts": [],
                            "status": "failed",
                            "message": str(exc),
                            "code": exc.code,
                        }
                        # 分类：告诉用户是该重试、该去点验证码，还是程序有问题
                        errors.enrich_result(
                            res, exc, stage="batch-line", line_no=line.get("line_no")
                        )
                        if exc.code == "VERIFICATION_REQUIRED":
                            # 新规则（2026-11-08）：验证先自己扛。
                            # 执行层在下一次 search 前会刷新页面自恢复，所以这里**立刻重试**
                            # 同一行，既不等 30 分钟，也不惊动人；
                            # 只有自动重试用完、刷新后仍然出不来商品，才升级为人工验证。
                            # 已经点过「加入购物车」的行绝不能重跑，否则重复下单。
                            _added = bool((exc.detail or {}).get("cart_added"))
                            _step = verification_next_step(_added, _verify_retries, _verify_human_retries)
                            if _step == "auto":
                                _verify_retries += 1
                                self._append_log(
                                    f"[batch] 第 {line.get('line_no')} 行撞验证 → 自动过滑块/刷新后重试"
                                    f"（{_verify_retries}/{VERIFY_AUTO_RETRY}）"
                                )
                                time.sleep(VERIFY_RETRY_GAP_S)
                                continue
                            if _step == "human" and self.wait_verification():
                                _verify_human_retries += 1
                                _verify_retries += 1
                                self._append_log(
                                    f"[batch] 第 {line.get('line_no')} 行刷新后仍无商品，人工验证已解除，重试"
                                    f"（人工第 {_verify_human_retries} 次）"
                                )
                                continue
                            with self._state_lock:
                                self._batch["stopped_reason"] = "自动过滑块和刷新后仍无商品，需要人工完成淘宝验证，批量任务已暂停"
                            self._batch_line_patch(i, **res)
                            _stop_after = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        res = {
                            "line_no": line.get("line_no"),
                            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
                            "qty": line.get("qty"),
                            "attempts": [],
                            "status": "failed",
                            "message": f"未预期错误：{exc}",
                        }
                        # 未预期异常不再「悄悄算失败」：分类 + 留 traceback，
                        # 日志里能用 code=UNEXPECTED 直接捞出来交给开发。
                        errors.enrich_result(
                            res, exc, stage="batch-line", line_no=line.get("line_no")
                        )
                        errors.log_exception(
                            exc, self._append_log,
                            stage="batch-line", line_no=line.get("line_no"),
                            batch_id=self._batch.get("batch_id"),
                        )
                        break
                if _stop_after:
                    break
                # 采购依据（编号→型号→标题是否命中）跟着结果落库：买完要能追溯
                # 「凭什么是这一件」，演练也写，方便真跑前先看依据对不对。
                res["basis"] = _procurement_basis(line, res)
                self._batch_line_patch(i, **res)
                # 自适应降温：连续 ≥2 行「点了但没回执」→ 疑似被限流，立刻拉长
                # 冷却（60s×连续数，封顶 300s）。ok 正常则清零。跑批实测：从第 4
                # 行开始 uncertain 爬升，第 21 行就是 tmd 硬拦截 —— 早降温能保住
                # 整轮不被掐断。
                if res.get("status") == "uncertain":
                    _unc_streak += 1
                    if _unc_streak >= UNCERTAIN_STREAK_TRIGGER:
                        _cool = min(UNCERTAIN_COOL_MAX_S, UNCERTAIN_COOL_BASE_S * _unc_streak)
                        self._append_log(
                            f"[batch] 连续 {_unc_streak} 行无加购回执 → 疑似限流，降温 {_cool}s"
                        )
                        with self._state_lock:
                            self._batch["phase"] = (
                                f"疑似限流降温 {_cool}s（连续 {_unc_streak} 行无回执）"
                            )
                        _cd = _cool
                        while _cd > 0 and not self._batch.get("stopped_reason"):
                            time.sleep(min(5, _cd))
                            _cd -= 5
                else:
                    _unc_streak = 0
                # 加购接口连续变慢（>5 s）也是限流前兆：实测 1 s → 8.5 s 之后
                # 就开始「点了没回执」。提前降温，别等失败了才反应。
                _slow_streak = _slow_streak + 1 if (res.get("add_ms") or 0) > 5000 else 0
                if _slow_streak >= 2:
                    _cool = min(UNCERTAIN_COOL_MAX_S, UNCERTAIN_COOL_BASE_S * 2)
                    self._append_log(
                        f"[batch] 加购连续 {_slow_streak} 行超过 5s → 疑似限流前兆，降温 {_cool}s"
                    )
                    with self._state_lock:
                        self._batch["phase"] = f"加购变慢，预热降温 {_cool}s"
                    _cd = _cool
                    while _cd > 0 and not self._batch.get("stopped_reason"):
                        time.sleep(min(5, _cd))
                        _cd -= 5
                    _slow_streak = 0
                with self._state_lock:
                    self._batch["done"] = sum(
                        1
                        for r in self._batch["results"]
                        # skipped 也算「处理过了」：无搜索词的残缺行和用户中途
                        # 停止后剩下的行都是 skipped，不算进去的话进度条永远差几行。
                        if r["status"] in SETTLED_STATUS
                    )
                # 每行都落盘：随时暂停/关机都不丢进度，下次接着跑
                # 演练模式不写进度：否则假的「ok 无金额」会覆盖真实进度
                if not dry_run:
                    self._save_progress()
        finally:
            with self._state_lock:
                self._batch["running"] = False
                self._batch["index"] = -1
                self._batch["phase"] = "已结束"
                self._batch["finished_at"] = datetime.now().isoformat(timespec="seconds")
                if self._batch.get("stopped_reason"):
                    for r in self._batch["results"]:
                        if r["status"] == "pending":
                            r["status"] = "skipped"
                            r["message"] = self._batch["stopped_reason"]
            if not dry_run:
                self._save_progress()
            # 收尾对账（P0-1）：把 ok 行逐条跟购物车实际行比对。ok 判定再严，
            # 也可能有「网络侧 200 但淘宝侧没入车」的漏网行 —— 只有购物车里
            # 真找得到这个商品才算数。对不上就降级 uncertain，报告里看得见。
            # 演练模式不做对账：dry-run 本来就不点加购，购物车当然找不到，
            # 一比对就把演练的 ok 全降级成 uncertain（2026-11-09 实测踩到）。
            try:
                if not dry_run:
                    cv = self.send({"cmd": "cart-view"}, timeout=90)
                    cdata = cv.get("data") or {}
                    with self._state_lock:
                        self._batch["cart_count"] = cdata.get("cart_item_count")
                    _log(f"[batch] cart count after run: {cdata.get('cart_item_count')}")
                    self._reconcile_cart(cdata)
                    if not dry_run:
                        self._save_progress()  # 降级结果也要落盘，续跑时才不会误当 ok 跳过
                    # 兜底：一行没跑、所有「续跑跳过」的行又都没在购物车里找到，
                    # 说明进度与购物车严重错配（换 BOM / 购物车被清空过）。
                    # 留着这份进度，下次点「继续加购」还是整批空跑，直接清掉。
                    if should_clear_stale_progress(self._batch, ran):
                        clear_batch_progress()
                        msg = (
                            "本批没有实际执行任何行：上一批进度里的商品在购物车里都找不到，"
                            "已清空旧进度，请重新点「一键全部加购」从头跑。"
                        )
                        with self._state_lock:
                            self._batch["warning"] = msg
                        _log("[batch] 整批未执行且对账全降级 → 已清空错配进度")
            except Exception as exc:  # noqa: BLE001
                _log(f"[batch] cart count skipped: {exc}")
            if dry_run:
                _log("[batch] dry-run 结束：未加购，进度文件未改动")
            # 本轮速度复盘：单独存一份，跑 10 遍才有得比。
            try:
                self._save_run_metrics()
            except Exception as exc:  # noqa: BLE001
                _log(f"[batch] metrics skipped: {exc}")
            # 跑完（正常结束 / 被停止 / 撞验证暂停）统一清截图，别把磁盘堆满
            self.cleanup_screenshots()

    @staticmethod
    def _norm_title(t: str) -> str:
        """对账用标题归一：去空白/全半角差异，取前 16 位做「在不在车里」的模糊匹配。"""
        t = re.sub(r"\s+", "", str(t or "")).lower()
        return t[:16]

    def _cart_contains(self, title: str) -> tuple[bool | None, str]:
        """购物车里有没有这个商品。

        返回 (True/False/None, 原因)。None = **判断不了**（明细降级、购物车读成空、
        会话出错）。这一档很重要：上层拿到 False 会「再点一次加购」，而读车失败时
        重试等于重复下单 —— 所以读不到就必须给 None，不能给 False。
        """
        if not str(title or "").strip():
            return None, "标题为空"
        try:
            cv = self.send({"cmd": "cart-view"}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            return None, f"查购物车失败：{str(exc)[:40]}"
        data = cv.get("data") or {}
        if data.get("rows_degraded"):
            return None, "购物车明细降级，无法确认"
        rows = data.get("rows") or []
        if not rows:
            return None, "购物车里一行都没读到（可能没加上，也可能页面没渲染完）"
        for r in rows:
            if _titles_match(title, r.get("title")):
                return True, ""
        return False, ""

    def _reconcile_cart(self, cart_data: dict) -> None:
        """批量收尾对账：ok 行的商品要能在购物车里找到，找不到才降级 uncertain。

        两条经验（2026-11-12 现场）：
        1. 匹配不能只看「标题去空白取前 16 字」——标题词序一变就全不匹配，
           于是整车 18 行一起被降级成「待核对」，等于什么都没确认。改用规格原子比对。
        2. 整批都不匹配时，更可能是**读车失败**（车被清空、页面没渲染、抓到空标题），
           不是 18 行同时加购失败。这时不能逐行降级，只能保留结论 + 打「未核实」标记，
           让人去看一眼；否则「买成了」和「没买成」显示成一个样。
        """
        rows = cart_data.get("rows") or []
        degraded = bool(cart_data.get("rows_degraded"))
        cart_titles = [r.get("title") for r in rows if str(r.get("title") or "").strip()]

        # 读车结果本身可不可信（不可信时一律不下结论）
        anomaly_reason = ""
        cart_count = int(cart_data.get("cart_item_count") or 0)
        if not rows:
            anomaly_reason = (
                "购物车读成空（0 行）"
                if not cart_count
                else f"购物车显示 {cart_count} 件但一行都没读到"
            )
        elif not cart_titles:
            anomaly_reason = f"购物车 {len(rows)} 行全都没有标题"
        elif cart_count and len(rows) < cart_count * RECONCILE_ROW_RATIO:
            # 2026-11-18：购物车是无限滚动，只读到第一页 29/68 行时，
            # 后面 39 件的商品会被误判「没加购」。读到的行远少于显示件数
            # = 没滚到底/没加载全，这时逐行降级是错的，只能标「未核实」。
            anomaly_reason = (
                f"购物车显示 {cart_count} 件但只读到 {len(rows)} 行"
                "（滚动加载不全，本批不逐行降级）"
            )

        matched, missed = [], []
        uncertain_found = []
        with self._state_lock:
            results = self._batch.get("results") or []
            for r in results:
                status = r.get("status")
                if status not in ("ok", "uncertain"):
                    continue
                title = (r.get("candidate") or {}).get("title")
                if not str(title or "").strip():
                    continue
                hit = bool(cart_titles) and any(_titles_match(title, t) for t in cart_titles)
                if status == "uncertain":
                    # 这类行是「点了加购但没抓到回执」，查车只为补证据，不改状态
                    if hit:
                        r["cart_found"] = True
                        note = "收尾对账：购物车里找到该商品（加购实际已生效）"
                        if note not in (r.get("message") or ""):
                            r["message"] = f"{r.get('message') or ''}；{note}".strip("；")
                        uncertain_found.append(r.get("line_no"))
                    continue
                (matched if hit else missed).append(r)

            checked = len(matched) + len(missed)
            # 整批都不匹配 / 匹配率过低 → 判定读车异常，不逐行降级
            if not anomaly_reason and checked >= RECONCILE_MIN_CHECK:
                ratio = len(matched) / checked if checked else 0.0
                if not matched:
                    anomaly_reason = f"{checked} 行全部没匹配到（疑似读车失败，不是 18 行同时失败）"
                elif checked >= RECONCILE_MIN_CHECK + 3 and ratio < RECONCILE_MIN_RATIO:
                    anomaly_reason = f"只有 {len(matched)}/{checked} 行匹配上（匹配率 {ratio:.0%}，疑似标题口径不符）"

            downgraded = []
            if anomaly_reason:
                # 保留 ok，但明确标「未核实」：不假装确认，也不冤枉人
                for r in missed:
                    r["cart_unverified"] = True
                    note = f"收尾对账未完成（{anomaly_reason}），本行保留「已加购」但未核实，请人工核对"
                    if note not in (r.get("message") or ""):
                        r["message"] = f"{r.get('message') or ''}；{note}".strip("；")
            elif not degraded:
                for r in missed:
                    r["status"] = "uncertain"
                    r["message"] = "收尾对账：购物车里没找到该商品（可能加购未生效），请人工核对"
                    downgraded.append(r.get("line_no"))
            self._batch["reconcile"] = {
                "checked": checked,
                "matched": len(matched),
                "downgraded": downgraded,
                "uncertain_found": uncertain_found,
                "cart_rows": len(rows),
                "cart_degraded": degraded,
                "cart_count": cart_data.get("cart_item_count"),
                "anomaly": bool(anomaly_reason),
                "anomaly_reason": anomaly_reason,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        if anomaly_reason:
            _log(
                f"[batch] reconcile: 读车疑似异常（{anomaly_reason}）→ 不降级，"
                f"{len(missed)} 行标为「未核实」，请人工核对购物车"
            )
        elif degraded:
            _log(f"[batch] reconcile skipped: cart rows degraded（{len(rows)} 行）")
        elif downgraded:
            _log(f"[batch] reconcile: {checked} ok 行里 {len(downgraded)} 行在购物车找不到，降级 uncertain: {downgraded}")
        else:
            _log(f"[batch] reconcile: {checked} ok 行全部在购物车核对到")
        if uncertain_found:
            _log(f"[batch] reconcile: {len(uncertain_found)} 个 uncertain 行确认已在购物车: {uncertain_found}")

    def _save_run_metrics(self):
        """把本轮每行耗时落盘到 runs/metrics-<时间>.json，供多轮对比。"""
        with self._state_lock:
            rows = []
            for r in self._batch.get("results") or []:
                if r.get("status") not in ("ok", "failed", "uncertain"):
                    continue
                rows.append(
                    {
                        "line_no": r.get("line_no"),
                        "status": r.get("status"),
                        "total_ms": r.get("total_ms"),
                        "search_ms": r.get("search_ms"),
                        "open_ms": r.get("open_ms"),
                        "select_ms": r.get("select_ms"),
                        "add_ms": r.get("add_ms"),
                        "attempts_n": len(r.get("attempts") or []),
                    }
                )
            meta = {
                "started_at": self._batch.get("started_at"),
                "finished_at": self._batch.get("finished_at"),
                "cart_count": self._batch.get("cart_count"),
                "max_candidates": self._batch.get("max_candidates"),
            }
        totals = [x["total_ms"] for x in rows if x.get("total_ms")]
        payload = dict(meta)
        payload["count"] = len(rows)
        payload["sum_total_ms"] = sum(totals)
        payload["mean_total_ms"] = int(sum(totals) / len(totals)) if totals else None
        payload["lines"] = rows
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = RUNS / f"metrics-{ts}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(
            f"[batch] metrics -> {path.name} "
            f"lines={len(rows)} mean={ (payload['mean_total_ms'] or 0) / 1000:.1f}s "
            f"total={(payload['sum_total_ms'] or 0) / 1000:.0f}s"
        )

    def _add_line_to_cart(self, line: dict, prefer_cheap: bool, max_candidates: int, dry_run: bool = False) -> dict:
        """首选词候选都失败后，继续试 query_plan 里后面的搜索词。

        实测 1210 100µF：型号词只召回一个「标价非实价」的链接，SKU 无法确认；
        后面的「1210 100uF 6.3V 贴片电容」其实有一堆正常货。search() 只在
        「没有可用候选」时换词，不会在「打开后全部选型失败」时换词，这里补上
        第二层。只在 failed 时换；一旦 ok/uncertain 立刻停，绝不重复加购。
        """
        queries = [
            str(q).strip()
            for q in (
                line.get("query_plan")
                or (line.get("decision") or {}).get("search_queries")
                or []
            )
            if str(q).strip()
        ]
        first = self._add_line_to_cart_once(line, prefer_cheap, max_candidates, dry_run=dry_run)
        if (
            first.get("status") != "failed"
            or len(queries) < 2
            or not (first.get("search") or {}).get("ok")
        ):
            return first
        used = str((first.get("search") or {}).get("query") or "")
        start = queries.index(used) + 1 if used in queries else 1
        if start >= len(queries):
            return first
        all_attempts = list(first.get("attempts") or [])
        totals = {
            k: first.get(k) or 0
            for k in ("search_ms", "open_ms", "select_ms", "add_ms")
        }
        fallback_meta = []
        last = first
        for q in queries[start:]:
            variant = dict(line)
            variant["query_plan"] = [q]
            decision = dict(line.get("decision") or {})
            decision["search_queries"] = [q]
            variant["decision"] = decision
            last = self._add_line_to_cart_once(
                variant, prefer_cheap, max_candidates, dry_run=dry_run
            )
            all_attempts.extend(last.get("attempts") or [])
            for k in totals:
                totals[k] += last.get(k) or 0
            fallback_meta.append(
                {
                    "query": q,
                    "status": last.get("status"),
                    "message": last.get("message"),
                }
            )
            if last.get("status") != "failed":
                last["attempts"] = all_attempts
                last["fallback_queries"] = fallback_meta
                for k, v in totals.items():
                    last[k] = v
                return last
        first["attempts"] = all_attempts
        first["fallback_queries"] = fallback_meta
        first["search"] = last.get("search") or first.get("search")
        for k, v in totals.items():
            first[k] = v
        if last.get("message"):
            first["message"] = last.get("message")
        return first

    def _add_line_to_cart_once(self, line: dict, prefer_cheap: bool, max_candidates: int, dry_run: bool = False) -> dict:
        base = {
            "line_no": line.get("line_no"),
            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
            "qty": line.get("qty"),
            "attempts": [],
        }
        _t_search = time.monotonic()
        # require_scored：这条词有结果但候选全被门槛拒掉时，换下一条词再试
        search = self.search(line, require_scored=True)
        base["search_ms"] = int((time.monotonic() - _t_search) * 1000)
        base["search"] = {
            "ok": search.get("ok"),
            "query": search.get("query"),
            "items_count": search.get("items_count"),
        }
        if not search.get("ok"):
            base.update(status="failed", message=search.get("message") or "搜索没有结果", screenshot=search.get("screenshot"))
            # 诊断摘要：全 0 结果 / 选择器失效会标 structure_suspect，
            # 这时「词不对」和「页面改版」能分开，不用一上来就改规则。
            _diag = search.get("diagnostics") or {}
            if _diag:
                base["diagnostics"] = _diag
            if _diag.get("structure_suspect"):
                base["structure_suspect"] = True
                base["message"] = (
                    f"{base['message']}（{_diag.get('reason')}；疑似页面结构变化或风控，"
                    "先看 /api/diagnostics 里这一步的记录）"
                )
            return base

        cands, dropped = [], []
        for c in (search.get("candidates") or []):
            if (c.get("score") or 0) <= 0:
                continue
            why = _candidate_veto(line, c.get("title"))
            if why:
                # 标题层面的"料不对"：保险丝行拿连接器、针座行拿带线母头。
                # 这类候选打开也是白花 15 s，还可能被页面默认值蒙混过关。
                dropped.append({"index": c.get("index"), "title": c.get("title"), "reason": why})
                continue
            c["mpn_hit"] = _title_has_mpn(line, c.get("title"))
            cands.append(c)
        if dropped:
            base["rejected"] = dropped[:10]
        # 按「买够这一行要花多少钱」比，而不是单只价：
        # 只要 1 个电阻时，100 只/包 ¥3.5 的单只价最低，但要多花 3.4 还多拿 99 个。
        need = max(1, int(line.get("qty") or 1))
        for c in cands:
            pack, _src = _title_pack(c.get("title") or "")
            pack = pack if pack and pack > 1 else None
            c["pack_size"] = pack
            price = c.get("price")
            c["unit_price"] = round(price / pack, 6) if (pack and price is not None) else price
            if c.get("cost") is None and price is not None:
                if pack:
                    buy, _per, actual = engine.plan_qty(need, pack)
                    c["buy_qty"], c["actual_pcs"] = buy, actual
                    c["over_ratio"] = round(actual / need, 2)
                    c["cost"] = round(price * buy, 4)
                else:
                    c["buy_qty"] = c["actual_pcs"] = need
                    c["over_ratio"] = 1.0
                    c["cost"] = round(price * need, 4)
        if prefer_cheap:
            cands.sort(
                key=lambda c: (
                    not c.get("mpn_hit", True),
                    c.get("cost") is None,
                    c.get("cost") or 0.0,
                    c.get("over_ratio") or 1.0,
                )
            )
        else:
            cands.sort(key=lambda c: (not c.get("mpn_hit", True), -(c.get("score") or 0)))
        base["price_from"] = cands[0].get("price") if cands else None
        if not cands:
            base.update(status="failed", message="没有通过硬门槛的候选")
            return base

        last_message = ""
        # 被动件（电阻/电容）常遇到「一个链接只挂了部分阻值」，SKU 里根本没有 BOM 要的那个值，
        # 这时只能换店。阻值档位多的品类多试两家，别因为前 3 家都没这一档就整行失败。
        _limit = max(int(max_candidates), 5) if (line.get("category") == "passive") else max_candidates
        _try_list = cands[:_limit]
        # 兜底候选的规格参照：BOM 自己要求的非数值规格 + 评分最高那条命中的规格项。
        _need_kinds = _required_spec_kinds(line)
        _value_kinds = _required_value_kinds(line)
        _ref_kinds: set = set(_need_kinds)
        if cands:
            _best_cand = max(cands, key=lambda c: (c.get("score") or 0))
            _ref_kinds |= _spec_kinds(_best_cand)
        for _ti, cand in enumerate(_try_list):
            attempt = {
                "index": cand.get("index"),
                "title": cand.get("title"),
                "price": cand.get("price"),
                "unit_price": cand.get("unit_price"),
                "pack_size": cand.get("pack_size"),
                "score": cand.get("score"),
                "ok": False,
                "message": "",
            }
            base["attempts"].append(attempt)
            # 材料/结构先过一遍：标题里就能看出「绕线」「3KV」的，连开页都省了。
            _mat_now = _material_conflict(line, cand.get("title") or "")
            if _mat_now:
                attempt["message"] = _mat_now + "，不自动加购"
                attempt["guard"] = "MATERIAL_CONFLICT"
                last_message = attempt["message"]
                continue
            # 淘汰级候选（评分 <60）不自动加购：买错的代价远高于买不到。
            # 例外是多规格商品 —— 它的规格由 SKU 选中值核实（见下面的 no_sku 分支），
            # 只要 SKU 能对上具体值就仍然可用；只有「单规格、标题也核实不到」才拒绝。
            if str(cand.get("decision") or "") == "淘汰" and (cand.get("score") or 0) < 20:
                attempt["message"] = f"评分 {cand.get('score')} 分（淘汰级），不自动加购"
                last_message = attempt["message"]
                continue
            # 「封装」「功率」都不算必须项：
            # · 直插电阻/电容的标题普遍写「1/4W 金属膜电阻」，没人写「插件」；
            # · 功率是「≥ 就够」的数值项，由 SKU 组或打分处理，标题不写很正常。
            #   硬要求命中等于把整类商品拒掉（实测 470Ω ×42 的五家候选全判
            #   「封装核实不到」，5kΩ 判「功率核实不到」）。
            _have_kinds = _spec_kinds(cand)
            _miss_spec = _need_kinds - _have_kinds
            if _ti > 0:
                _strict = (_ref_kinds - _value_kinds - {"封装", "功率"}) - _have_kinds
                _miss_spec = _miss_spec | _strict
            if _miss_spec:
                attempt["message"] = (
                    "候选标题核实不到关键规格（" + "、".join(sorted(_miss_spec)) + "），不自动加购"
                )
                last_message = attempt["message"]
                continue
            # 功率方向是「≥ 才够」：标题明确写 1/8W，BOM 要 1/4W，阻值/封装再对也不能买。
            # 放在规格缺失之后判，报错优先说「读不到阻值」这种更根本的问题。
            _pw_bad = _under_power(line, cand.get("title") or "")
            if _pw_bad:
                attempt["message"] = _pw_bad + "，不自动加购"
                last_message = attempt["message"]
                continue
            _t_open = time.monotonic()
            opened = None
            # 打开商品时常有偶发故障（店铺页慢、图片卡住、导航超时）。
            # 以前一次超时就把整行判死，白等一轮还得人工补 —— 先原地重试一次，
            # 还不行再换下一个候选，最后才整行失败。
            for _retry in range(2):
                try:
                    opened = self.open_item(line, cand["index"])
                    break
                except SessionError as exc:
                    if exc.code == "VERIFICATION_REQUIRED":
                        raise
                    if _retry == 0 and _is_transient_error(exc):
                        base.setdefault("retries", []).append(
                            {"index": cand.get("index"), "reason": str(exc)[:100]}
                        )
                        time.sleep(1.5)
                        continue
                    attempt["message"] = f"打开商品失败：{str(exc)[:140]}"
                    last_message = attempt["message"]
                    break
                except Exception as exc:  # noqa: BLE001
                    attempt["message"] = f"打开商品异常：{str(exc)[:140]}"
                    last_message = attempt["message"]
                    break
            base["open_ms"] = (base.get("open_ms") or 0) + int((time.monotonic() - _t_open) * 1000)
            attempt["open_ms"] = int((time.monotonic() - _t_open) * 1000)
            if opened is None:
                continue
            diagnostics.record(
                "open", line_no=line.get("line_no"), title=cand.get("title"),
                index=cand.get("index"),
                sku_groups=len(opened.get("sku_groups") or []),
                no_sku=bool(opened.get("no_sku")),
            )
            # SKU 组的全量档位留痕：真机跑一次就把「这家店里到底有没有这一档」记下来，
            # 后面做候选回放/选型回放时不用再开浏览器（回放最大的盲区就在 SKU 层）。
            attempt["sku_options"] = condense_sku_groups(opened.get("sku_groups"))
            # 单规格商品没有 SKU 可核对，标题是唯一的规格来源：标题里读不到 BOM 要的
            # 那个值，就不加购。实测 5kΩ 搜出来的整页压敏电阻就是从这个口子进去的。
            if opened.get("no_sku"):
                _miss_v = _value_kinds - _title_value_kinds(line, cand.get("title"))
                if _miss_v:
                    attempt["message"] = (
                        "单规格商品，标题核实不到"
                        + "、".join(sorted(_miss_v))
                        + "，不自动加购"
                    )
                    last_message = attempt["message"]
                    continue
                if str(cand.get("decision") or "") == "淘汰" and (cand.get("score") or 0) < 60:
                    attempt["message"] = (
                        f"评分 {cand.get('score')} 分（淘汰级）且为单规格商品，不自动加购"
                    )
                    last_message = attempt["message"]
                    continue
            # 包装信息没解析出来、标题价却高得离谱：基本就是「整袋 2000 只」的报价。
            # 精确判断放在选完 SKU 之后（那时才知道每份多少只），这里先挡明显的。
            _unit_now = opened.get("unit_price")
            _ceil_now = _unit_price_ceiling(line)
            if (
                _unit_now
                and _ceil_now
                and not opened.get("pack_size")
                and float(_unit_now) > float(_ceil_now) * 3
            ):
                attempt["message"] = (
                    f"折合每只 ¥{float(_unit_now):g}（包装未标明），"
                    f"疑似整袋报价，换下一家"
                )
                last_message = attempt["message"]
                continue
            unresolved = opened.get("unresolved") or []
            sels = opened.get("suggestions") or []
            no_sku = bool(opened.get("no_sku"))
            manual = [s for s in sels if not s.get("auto")]
            # 一次开页就把结论留下来：这家店里到底有没有这一档、系统选了哪个值。
            # 以后回放/复盘不用再开浏览器（SKU 层是标题闸门唯一的盲区）。
            record_sku_observation(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "dry": bool(dry_run),
                    "line_no": line.get("line_no"),
                    "headline": line.get("mpn") or (line.get("desc") or "")[:40],
                    "category": line.get("category"),
                    "title": cand.get("title"),
                    "url": cand.get("url"),
                    "price": cand.get("price"),
                    "no_sku": no_sku,
                    "sku_options": attempt.get("sku_options") or [],
                    "suggestions": sels,
                    "unresolved": unresolved,
                    "line": line,
                }
            )

            # 包规经常藏在 SKU 里（选了「100只」才知道），标题看不出来。
            # 打开后发现超量太狠（只要 1 个却要买 100 个），只要后面还有候选
            # 就先跳过这家 —— 这就是「换一家店买」。最后一个候选无论多超量都认，
            # 否则整行会买不到。
            _pack_now = opened.get("pack_size") or 0
            _need_now = max(1, int(line.get("qty") or 1))
            _actual_now = opened.get("actual_pcs") or (opened.get("suggested_qty") or 0) * _pack_now
            if _pack_now > 1 and _actual_now:
                _over_now = _actual_now / _need_now
                # 散料本来就按 50/100 只一包卖，BOM 要 1–4 只时超量必然 20–100 倍，
                # 以前 `over>=8` 恒真 → 每行连开 4 家、每家 15–20 s，最后第 5 家
                # 收的还是同样的 100 只/包（实测 #74 拒了 20/100/100/50 四家）。
                # 现在只有当后面**确实有更小包装、且不更贵**的候选时才换店。
                if _over_now >= 8 and _ti < len(_try_list) - 1:
                    _nxt = _try_list[_ti + 1]
                    _nxt_pack = _nxt.get("pack_size") or 0
                    _cur_cost = (opened.get("suggested_qty") or 1) * (opened.get("detail_price") or 0)
                    _nxt_cost = _nxt.get("cost")
                    _worth = bool(_nxt_pack and _nxt_pack < _pack_now)
                    # 下一家一看就是错货（2KV 高压瓷片、绕线电阻）就别为了小包装换过去：
                    # 换过去也是被材料闸门拒，白跑一趟还把唯一对的货丢了
                    # （实测 #4 10nF：第 4 家 50V 瓷片 1000 只/包，被换成第 5 家 2KV，最后整行失败）。
                    if _material_conflict(line, _nxt.get("title") or ""):
                        _worth = False
                    if _worth and _nxt_cost is not None and _cur_cost:
                        # 小包装贵一点点可以接受（少浪费 80 只），贵太多就不值当
                        _worth = _nxt_cost <= max(_cur_cost * 1.3, _cur_cost + 1.0)
                    if _worth:
                        attempt["message"] = f"整包 {_pack_now} 只/份（超量 {_over_now:.0f} 倍），下一家包装更小，换"
                        last_message = attempt["message"]
                        continue

            if not no_sku and (unresolved or not sels or manual):
                why = "；".join(f"{u.get('label', '')}:{u.get('reason', '')}" for u in unresolved)
                if manual:
                    why = "；".join(f"{s.get('label')}={s.get('value')} 未确认" for s in manual) + ("；" + why if why else "")
                attempt["message"] = "SKU 无法确认 —— " + (why or "没有可用的 SKU 建议")
                last_message = attempt["message"]
                continue

            # 多规格商品：选中的 SKU 值必须能证明 BOM 要的那个值。
            # 实测「95µF」搜到微波炉高压电容（0.85/0.9/0.95/1.0UF 同一链接多规格），
            # 系统给选了「维修八件套」—— 那是配件包，不是电容规格，容值差 100 倍。
            # SKU 冲突判定看不出来：选项里根本没有「95µF」，所以不构成「全部冲突」，
            # 只能在这里用「选中的值能不能读出规格」兜住。
            if not no_sku and sels:
                _sel_txt = " ".join(str(s.get("value") or "") for s in sels).strip()
                # 标题没写、SKU 才写的也要拦（「颜色分类=绕线1W」这类）。
                _mat_sku = _material_conflict(line, _sel_txt)
                if _mat_sku:
                    attempt["message"] = _mat_sku + "，不自动加购"
                    attempt["guard"] = "MATERIAL_CONFLICT"
                    last_message = attempt["message"]
                    continue
                # 型号必须精确对上：同系列不同号就是降规格（SS26 顶 SS34）。
                # 这条与「有没有数值规格」无关，二极管/连接器也走这里。
                _bad_pn = _sku_model_conflict(line, _sel_txt)
                if _bad_pn:
                    attempt["message"] = (
                        f"选中的 SKU 是 {_bad_pn}，与 BOM 型号 {line.get('mpn')} 同系列但不同号"
                        f"（等同降规格），不自动加购"
                    )
                    last_message = attempt["message"]
                    continue
                if _value_kinds:
                    _prove = _sel_txt + " " + str(cand.get("title") or "")
                    _miss_now = _value_kinds - _title_value_kinds(line, _prove)
                    if _miss_now:
                        attempt["message"] = (
                            "选中的 SKU（" + (_sel_txt[:20] or "空") + "）核实不到"
                            + "、".join(sorted(_miss_now))
                            + "，不自动加购"
                        )
                        last_message = attempt["message"]
                        continue

            qty = opened.get("suggested_qty") or line.get("qty") or 1
            selections = [{"label": s["label"], "value": s["value"]} for s in sels]
            if dry_run:
                # 演练：选型全流程照跑（搜索/开页/SKU 判定/金额估算），只是不点「加入购物车」。
                # 用于多轮回归测速度，不给购物车堆重复商品、不污染真实进度。
                _p = opened.get("detail_price")
                _ps = opened.get("pack_size") or 1
                _pcs = opened.get("actual_pcs") or (opened.get("suggested_qty") or 1) * _ps
                # 钱闸门在演练里也要过：否则演练说「这行能买」，真跑被单价/金额闸门拦下，
                # 演练就只是跑了个热闹（实测 19 行演练全绿、真跑 5 行被拦）。
                # 详情页没回报价时用标题价兜底 —— 真跑那边（见 _guard）本来就是这么做的，
                # 不兜底就会出现「演练 ok、真跑被 ¥90 拦下」的两副面孔。
                _guard_price = _p if _p is not None else cand.get("price")
                _mg = _money_guards(
                    line, _guard_price, _ps, opened.get("suggested_qty") or 1, _pcs
                )
                if _mg:
                    attempt["message"] = _mg["message"]
                    attempt["guard"] = _mg.get("code")
                    last_message = attempt["message"]
                    continue
                attempt["ok"] = True
                attempt["dry"] = True
                base.update(
                    status="ok",
                    message="演练：未点加购（仅校验选型）",
                    candidate={
                        "index": cand.get("index"),
                        "title": cand.get("title"),
                        "price": cand.get("price"),
                        "url": cand.get("url"),
                        "score": cand.get("score"),
                    },
                    sku=selections,
                    qty=qty,
                    final_price=_p,
                    cost=(_p * (opened.get("suggested_qty") or 1)) if _p else None,
                    pack_size=opened.get("pack_size"),
                    actual_pcs=_pcs,
                    dry_run=True,
                    sku_options=attempt.get("sku_options") or [],
                )
                return base
            # 加购最多尝试两次：第一次「点了没回执」时，先查购物车——
            #   · 车里有 → 其实成功了（记 ok，证据=购物车）
            #   · 车里没有 → 再点一次是安全的（不会重复下单）
            # 这是把「偶尔加不上」从 uncertain 变成「自动补一次」的关键，
            # 2026-11-09 加入。购物车明细读不到（降级）时保持原样，不乱试。
            added = None
            for _try_add in range(2):
                added = self.add_to_cart(
                    line, cand["index"], selections, qty, allow_empty=no_sku,
                    auto_qty=True, pack_hint=opened.get("pack_size"),
                    price_hint=opened.get("detail_price"),
                )
                if added.get("ok"):
                    break
                if added.get("code") != "CART_UNCONFIRMED":
                    break
                _found, _why = self._cart_contains(cand.get("title"))
                if _found is True:
                    added = dict(added)
                    added.update(
                        ok=True,
                        cart_verified_via="cart_check",
                        message="加购无回执，但购物车里确认到该商品（已生效）",
                    )
                    break
                if _found is False and _try_add == 0:
                    attempt["message"] = "加购无回执；购物车确认没有该商品 → 重试一次"
                    _log(f"[batch] line #{line.get('line_no')} 加购无回执且车里没有，重试一次")
                    continue
                if _why:
                    attempt["message"] = f"{attempt.get('message') or ''}（{_why}）"
                break
            qty = added.get("qty") or qty
            diagnostics.record(
                "cart_add", line_no=line.get("line_no"),
                ok=bool(added.get("ok")),
                code=str(added.get("code") or "")[:40],
                via=str(added.get("cart_verified_via") or "")[:40],
            )
            attempt["message"] = added.get("message") or ("已加入购物车" if added.get("ok") else "加购失败")
            if not added.get("ok"):
                last_message = attempt["message"]
                if added.get("code") == "CART_UNCONFIRMED":
                    # 已经点过「加入购物车」，再换候选会重复加购 —— 这行到此为止。
                    # 但不要中断整批：淘宝购物车页常被风控挡成 Oops，确认不到不算失败。
                    base.update(
                        status="uncertain",
                        message="已点击加购但页面没给确认信号（淘宝购物车页可能被风控挡），请到购物车核对",
                        candidate={
                            "index": cand.get("index"),
                            "title": cand.get("title"),
                            "price": cand.get("price"),
                            "url": cand.get("url"),
                            "score": cand.get("score"),
                        },
                        sku=sels,
                        qty=qty,
                        screenshot=added.get("screenshot"),
                    )
                    break
                if added.get("code") in ("COST_TOO_HIGH", "OVERBUY_TOO_MUCH", "UNIT_PRICE_ANOMALY"):
                    # 这家买够这一行太贵/单价明显是整包价：还有候选就换下一家，
                    # 最后一个候选交人工判断
                    attempt["cost"] = added.get("est_cost")
                    attempt["pack_size"] = added.get("pack_size")
                    attempt["unit_price"] = added.get("unit_price")
                    if _ti < len(_try_list) - 1:
                        attempt["message"] = (added.get("message") or "") + "，换下一家"
                        last_message = attempt["message"]
                        continue
                    base.update(
                        status="uncertain",
                        message=(added.get("message") or "金额超过单行上限") + "，可到页面人工确认是否购买",
                        candidate={
                            "index": cand.get("index"),
                            "title": cand.get("title"),
                            "price": cand.get("price"),
                            "url": cand.get("url"),
                            "score": cand.get("score"),
                        },
                        sku=sels,
                        qty=qty,
                        final_price=added.get("final_price"),
                        cost=added.get("est_cost"),
                        pack_size=added.get("pack_size"),
                        actual_pcs=added.get("actual_pcs"),
                        screenshot=added.get("screenshot"),
                    )
                    return base
                continue

            attempt["ok"] = True
            # 金额：final_price 是「选完 SKU 后页面显示的每份单价」，cost 由 add_to_cart 算好
            _final = added.get("final_price")
            _sent = added.get("qty_sent") or qty
            _cost = added.get("cost")
            base.update(
                status="ok",
                message="已加入购物车",
                candidate={
                    "index": cand.get("index"),
                    "title": cand.get("title"),
                    "price": cand.get("price"),
                    "url": cand.get("url"),
                    "score": cand.get("score"),
                },
                detail_price=opened.get("detail_price"),
                qty=qty,
                qty_sent=_sent,
                final_price=_final,
                cost=_cost,
                pack_size=added.get("pack_size") or opened.get("pack_size"),
                pack_source=added.get("pack_source") or opened.get("pack_source"),
                actual_pcs=added.get("actual_pcs") or opened.get("actual_pcs"),
                unit_price=added.get("unit_price") or opened.get("unit_price"),
                bom_qty=opened.get("bom_qty"),
                sku=sels,
                sku_options=attempt.get("sku_options") or [],
                screenshot=added.get("screenshot"),
                cart_verified_via=added.get("cart_verified_via"),
                select_ms=added.get("select_ms"),
                add_ms=added.get("add_ms"),
            )
            return base

        if base.get("status") != "uncertain":
            base.update(status="failed", message=last_message or "候选都无法加购")
        return base


RUNNER = SessionRunner()


# --------------------------------------------------------------------------
# SKU 匹配
# --------------------------------------------------------------------------

# 这些 SKU 值根本不能直接下单（要询价/改价），命中就直接判定该候选不可买


# 整包报价的包装数解析统一放在 engine（比价也要用同一套规则），这里只做别名
_pack_from_texts = engine.pack_from_texts
_title_pack = engine.title_pack
_pack_size = engine.pack_size
_plan_qty = engine.plan_qty

# 单行金额上限：只有选完 SKU 才知道「一份」到底多少钱。
# 阻容感一行正常几毛到几十元，200 元已经明显不对（典型是整盘/整卷报价）；
# 芯片/功率件单价本来高，默认给到 5000，等于不拦。
_DEFAULT_LINE_CAP = {"passive": 200.0, "connector": 300.0, "other": 200.0, "default": 5000.0}


def _line_cost_cap(category) -> float:
    cat = str(category or "other")
    cfg = load_settings().get("max_line_cost") or {}
    val = cfg.get(cat, cfg.get("default"))
    try:
        return float(val)
    except (TypeError, ValueError):
        return _DEFAULT_LINE_CAP.get(cat, _DEFAULT_LINE_CAP["default"])


# 每只（每份折合单只）的价格天花板。淘宝常把「整袋 2000 只」的价挂在标题上，
# 不带包装信息就成了「单只 ¥94 的拨动开关」—— 只看单行总金额（passive 上限 200）
# 根本拦不住。按品类给常见价上限，超了就是包装没看清，宁可不买。
_UNIT_PRICE_CEIL = {
    "resistor": 2.0,
    "capacitor": 6.0,
    "inductor": 10.0,
    "diode": 5.0,
    "led": 3.0,
    "bead": 5.0,
    "fuse": 20.0,
}
_SWITCH_CEIL = 15.0
_CONNECTOR_CEIL = 30.0


def _unit_price_ceiling(line: dict) -> float | None:
    """这一行「每只」价格的合理上限；认不出品类就返回 None（不拦）。"""
    sub = str(line.get("subtype") or "")
    if sub in _UNIT_PRICE_CEIL:
        return _UNIT_PRICE_CEIL[sub]
    row = line.get("row") or {}
    blob = " ".join(
        [str(line.get(k) or "") for k in ("mpn", "desc", "params", "params_text", "package")]
        + [str(row.get("package") or ""), str(row.get("desc") or "")]
    ).lower()
    # 开关在引擎里落在 other/connector，靠封装（SW-TH_…）和位号（SW1）才认得出
    if re.match(r"\s*SW\d", str(line.get("refs") or ""), re.I) or re.search(
        r"\bsw[-_]|switch|开关|按键|轻触|拨动", blob
    ):
        return _SWITCH_CEIL
    if str(line.get("category") or "") == "connector" or re.search(
        r"conn|连接器|插座|针座|端子", blob
    ):
        return _CONNECTOR_CEIL
    return None


def _money_guards(
    line: dict,
    price,
    pack,
    qty,
    actual_pcs,
    auto_qty: bool = True,
    amount=None,
) -> dict | None:
    """三道钱闸门：每只单价、单行金额、超量。

    真实加购和演练必须走**同一套**判断。之前演练在 add_to_cart 之前就 return 了，
    钱闸门全被绕过 —— 演练报告「19 行全通」，真跑却有 5 行被拦，等于白演练。

    price 用于「每只单价」（标题价兜底也够用）；amount 用于「这一行要花多少钱」，
    默认同 price。两者分开是因为标题价可能只是引流价，金额只认选完 SKU 的实价。

    返回 None 表示通过；否则返回 {code, message, ...}，调用方补截图后原样返回。
    """
    eff_pack = int(pack or 1) or 1
    unit = None
    try:
        if price is not None:
            unit = float(price) / float(eff_pack)
    except (TypeError, ValueError, ZeroDivisionError):
        unit = None
    ceil_ = _unit_price_ceiling(line)
    if unit and ceil_ and unit > float(ceil_):
        return {
            "code": "UNIT_PRICE_ANOMALY",
            "message": (
                f"折合每只 ¥{unit:g}，超过该品类常见价 ¥{ceil_:g}，"
                "疑似整袋/整盘报价，已放弃自动加购"
            ),
            "unit_price": round(unit, 6),
            "unit_cap": ceil_,
            "pack_size": eff_pack if eff_pack > 1 else None,
            "actual_pcs": actual_pcs,
        }

    est_cost = None
    _amt = price if amount is None else amount
    try:
        if _amt is not None:
            est_cost = round(float(_amt) * max(1, int(qty)), 2)
    except (TypeError, ValueError):
        est_cost = None
    cap = _line_cost_cap(line.get("category"))
    if est_cost is not None and cap and est_cost > cap:
        return {
            "code": "COST_TOO_HIGH",
            "message": (
                f"这一行要花 ¥{est_cost:g}（{max(1, int(qty))} 份 × ¥{float(_amt):g}／份），"
                f"超过单行上限 ¥{cap:g}，已放弃自动加购"
            ),
            "est_cost": est_cost,
            "cap": cap,
            "qty": max(1, int(qty)),
            "final_price": float(_amt),
            "pack_size": eff_pack if eff_pack > 1 else None,
            "actual_pcs": actual_pcs,
            "unit_price": round(unit, 6) if unit is not None else None,
        }

    if (
        auto_qty
        and est_cost is not None
        and actual_pcs
        and line.get("qty")
        and est_cost >= 50
        and float(actual_pcs) / max(1, int(line.get("qty"))) >= 50
    ):
        over = float(actual_pcs) / max(1, int(line.get("qty")))
        return {
            "code": "OVERBUY_TOO_MUCH",
            "message": (
                f"要 {int(line.get('qty'))} 只却到手 {int(actual_pcs)} 只（超量 {over:.0f} 倍），"
                f"合计 ¥{est_cost:g}，已放弃自动加购"
            ),
            "over_ratio": round(over, 1),
            "est_cost": est_cost,
            "qty": max(1, int(qty)),
            "final_price": float(price),
            "pack_size": eff_pack if eff_pack > 1 else None,
            "actual_pcs": actual_pcs,
        }
    return None


# 兜底候选必须命中的关键规格项。换一家店可以，换错规格不行 ——
# 实测最优候选「加购按钮找不到」后，兜底买了 1/8W 的 1K 顶替 1/4W，
# 评分掉到 65，界面还写「已加入购物车」。






# ---------------- 材料 / 结构闸门 ----------------
# 阻值、容值对上，不代表能焊上。实测同一份 BOM：
#   #14 470Ω ±1% 1/4W 金属膜 → 买了「绕线 1W」（绕线有感、体积大 3 倍，插不进）
#   #4  10nF 50V 脚距 2.54   → 买了「3KV 高压瓷片」（脚距 5.08，焊盘对不上）
#   #7  100nF 50V 脚距 2.54  → 买了「独石 P=5.08」
# 值都是对的，参数栏也都能「核实」，所以原来的值闸门全放行。这里补三类硬件事实：
# 电阻类型（金属膜/绕线/碳膜）、功率上限、耐压上限、脚距。
























# --------------------------------------------------------------------------
# 形态闸门：数值全对、但「不是同一种东西」
#
# 2026-11-18 全链路审查发现的盲区：阻值/容值/耐压/针数都对得上，仍可能买回
# 贴片当插件、有源晶振当无源晶体、共模电感当单绕组电感、IC 当核心板。
# 这些都不是「降规格」，是**根本装不上/接不通**，必须在下单前拦住。
# --------------------------------------------------------------------------































































def _is_transient_error(exc) -> bool:
    """打开商品失败是不是「偶发」的（页面慢/网络抖/导航超时）。

    偶发错误值得原地重试一次；验证拦截、商品下架这类重试也没用，直接换候选。
    """
    code = str(getattr(exc, "code", "") or "")
    if code in ("VERIFICATION_REQUIRED", "NO_QUERY", "BATCH_RUNNING", "BATCH_EMPTY"):
        return False
    msg = str(exc or "")
    if code in ("OPEN_FAILED", "OPEN_TIMEOUT", "SEARCH_NO_RESPONSE"):
        return True
    return any(
        k in msg
        for k in ("Timeout", "timeout", "超时", "net::ERR", "Target closed", "Navigation",
                  "ECONNRESET", "断开的连接", "ERR_CONNECTION")
    )


EXPORT_COLUMNS = (
    "行号",
    "状态",
    "需求",
    "买到的商品",
    "SKU",
    "数量",
    "实付金额",
    "确认方式",
    "耗时(s)",
    "备注",
    # 失败行要能一眼看出「是数据问题、可重试，还是要人工过验证」
    "错误码",
    "处理建议",
)


def batch_export_rows(results: list[dict]) -> list[list[str]]:
    """把批量逐行结果整理成可直接落 CSV 的二维表（首行是表头）。

    跑完一轮要人工核对 100+ 行时，屏幕上滚太痛苦 —— 导出后可以用 Excel
    跟 BOM 并排 diff。金额/耗时都转成字符串，避免 csv 写入出现类型差异。
    """
    rows: list[list[str]] = [list(EXPORT_COLUMNS)]
    for r in results or []:
        if r.get("status") not in ("ok", "failed", "uncertain", "skipped"):
            continue
        sku = " / ".join(
            f"{s.get('label')}={s.get('value')}" for s in (r.get("sku") or []) if s.get("value")
        )
        cost = r.get("cost")
        # 确认方式：加购回执来源；对账没做成时写「未核实」，别让空值看起来像「已确认」
        verified = str(r.get("cart_verified_via") or "")
        if not verified and r.get("cart_unverified"):
            verified = "未核实（对账未做成）"
        rows.append(
            [
                str(r.get("line_no") or ""),
                str(r.get("status") or ""),
                str(r.get("headline") or ""),
                str((r.get("candidate") or {}).get("title") or ""),
                sku,
                str(r.get("qty") or ""),
                "" if cost is None else f"{float(cost):.2f}",
                verified,
                f"{(r.get('total_ms') or 0) / 1000:.1f}",
                " ".join(str(r.get("message") or "").split()),
                str(r.get("error_code") or ""),
                errors.hint_for_result(r),
            ]
        )
    return rows


































_FORM_HEADER_WORDS = ("针座", "直针", "弯针", "排针", "插座", "母座", "端子台", "卧贴", "立贴", "编带")




























# 同族可直接互换的写法（不同族一律否决：KF2510 插不进 XH 座）
_CONN_FAMILY_ALIAS = {"KF128/2EDG": {"KF128/2EDG"}}
