#!/usr/bin/env python3
"""执行诊断流水：把每一步「搜到几条、选中哪个、卡在哪」按天记成 JSONL。

外部网站改版时，症状往往只是「这一行失败了」，但原因可能完全不同：

    搜索 0 条   → 选择器/风控/词不对
    SKU 选不中  → 选项组改了，或这家店根本不卖这个规格
    加购没回执  → 按钮改了 / 限流 / 真被拒

光看 `status=failed` 无法区分。这里把**结构化信号**（不是截图）追加到
`runs/diagnostics-YYYY-MM-DD.jsonl`，一行一个事件，跑完/复现时按行号捞出来看：

    python -c "import diagnostics, json; print(json.dumps(diagnostics.recent(line_no=7), ensure_ascii=False, indent=1))"

设计约束：
  - 只追加、不修改历史；单文件按天切，超过保留期自动删（跟 BOM_KEEP_DAYS 一致）。
  - 写盘失败绝不能影响采购主流程 —— 所有异常在 record() 里吞掉。
  - 不记录页面全文、cookie、token；只留计数、阶段、耗时、错误码。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
KEEP_DAYS = max(1, int(os.environ.get("BOM_KEEP_DAYS", "14") or 14))
_MAX_BYTES = 4 * 1024 * 1024  # 单日文件上限，防一个死循环把盘写满


def runs_dir() -> Path:
    """诊断跟着 runs/ 走：测试用 TAOBAO_RUNS_DIR 隔离时自动落在临时目录。"""
    path = Path(os.environ.get("TAOBAO_RUNS_DIR") or (BASE / "runs")).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def current_file(day: str | None = None) -> Path:
    return runs_dir() / f"diagnostics-{day or _day()}.jsonl"


def _prune(day: str) -> None:
    """删掉超过保留期的诊断文件（一天扫一次就够，靠文件名判断）。"""
    cutoff = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)
    try:
        for p in runs_dir().glob("diagnostics-*.jsonl"):
            try:
                stem_day = p.stem.split("-", 1)[1]
                if datetime.strptime(stem_day, "%Y-%m-%d") < cutoff:
                    p.unlink(missing_ok=True)
            except (ValueError, OSError):
                continue
    except OSError:
        pass


def record(event: str, *, line_no: int | str | None = None,
           batch_id: str | None = None, **fields) -> dict | None:
    """追加一条诊断事件。返回写下的 dict（测试用）；失败返回 None，绝不抛。"""
    if not event:
        return None
    row: dict = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": str(event),
    }
    if line_no is not None:
        row["line_no"] = int(line_no) if str(line_no).isdigit() else str(line_no)
    if batch_id:
        row["batch_id"] = str(batch_id)
    for k, v in fields.items():
        if v is None or k in ("cookie", "token", "page_text"):
            continue
        # 只收标量/短列表，别把整页候选塞进来
        if isinstance(v, (str, int, float, bool)):
            row[k] = v[:200] if isinstance(v, str) else v
        elif isinstance(v, (list, tuple)) and len(v) <= 12 and all(
            isinstance(x, (str, int, float, bool)) for x in v
        ):
            row[k] = list(v)
    try:
        path = current_file()
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            return None
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not getattr(record, "_pruned", None):
            _prune(_day())
            record._pruned = True  # type: ignore[attr-defined]
        return row
    except OSError:
        return None


def recent(limit: int = 200, *, line_no: int | str | None = None,
           event: str | None = None, day: str | None = None) -> list[dict]:
    """读最近的诊断事件（新的在后）。读不到就是空列表。"""
    path = current_file(day)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if line_no is not None and str(row.get("line_no")) != str(line_no):
                    continue
                if event and row.get("event") != event:
                    continue
                out.append(row)
    except OSError:
        return []
    return out[-limit:]


def summarize(records: list[dict]) -> dict:
    """把一批事件压成「哪一步不对」的摘要，给页面/命令行看。"""
    counts: dict[str, int] = {}
    searches = 0
    zero = 0
    selector_miss = 0
    codes: dict[str, int] = {}
    for row in records or []:
        ev = str(row.get("event") or "")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "search":
            searches += 1
            if int(row.get("items") or 0) == 0:
                zero += 1
        code = row.get("code")
        if isinstance(code, str) and code:
            codes[code] = codes.get(code, 0) + 1
            if code in ("SELECTOR_MISS", "OPEN_FAILED", "SEARCH_FAILED"):
                selector_miss += 1
    # 「全零结果」+ 页面错误码并存时，结构变化/风控的嫌疑最大
    suspect = bool(searches and zero == searches) or selector_miss > 0
    reasons = []
    if searches and zero == searches:
        reasons.append(f"{searches} 条搜索词全部 0 结果")
    if selector_miss:
        reasons.append(f"{selector_miss} 次选择器/开页失败")
    return {
        "events": sum(counts.values()),
        "counts": counts,
        "codes": codes,
        "searches": searches,
        "zero_result_searches": zero,
        "selector_miss": selector_miss,
        "structure_suspect": suspect,
        "reason": "；".join(reasons),
    }
