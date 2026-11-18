#!/usr/bin/env python3
"""诊断流水回归（diagnostics.py）。

守住：
  - 事件按天落 JSONL，能按行号/事件名筛；
  - 「所有搜索词都 0 结果」或「选择器失败」要判成结构可疑，而不是词错；
  - 写盘失败不影响主流程（不抛异常）；
  - 过期文件会被清掉，敏感字段不进文件。
"""

import os
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="diag-test-"))
os.environ["TAOBAO_RUNS_DIR"] = str(_TMP)

import diagnostics  # noqa: E402  （必须在设置 env 之后）

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("== 1. 写与读 ==")
r = diagnostics.record("search", line_no=1, query="10kΩ 0603", items=16)
check("写入返回 dict", isinstance(r, dict) and r.get("event") == "search", r)
check("文件按天命名", diagnostics.current_file().name.startswith("diagnostics-"), diagnostics.current_file())
diagnostics.record("search", line_no=1, query="10kΩ 0402", items=0)
diagnostics.record("open", line_no=1, title="0603WAF1002T5E", sku_groups=2)
diagnostics.record("cart_add", line_no=2, ok=True, via="xhr")

_one = diagnostics.recent(line_no=1)
check("按行号筛出 3 条", len(_one) == 3, [x["event"] for x in _one])
_two = diagnostics.recent(line_no=2)
check("别的行不串", len(_two) == 1 and _two[0]["event"] == "cart_add", _two)
_searches = diagnostics.recent(event="search")
check("按事件名筛", len(_searches) == 2, _searches)

print("== 2. 摘要：结构可疑 ==")
_sum = diagnostics.summarize(_one)
check("统计搜索次数", _sum["searches"] == 2, _sum)
check("统计 0 结果", _sum["zero_result_searches"] == 1, _sum)
check("半数 0 结果不算结构可疑", _sum["structure_suspect"] is False, _sum)

diagnostics.record("search", line_no=3, query="a", items=0)
diagnostics.record("search", line_no=3, query="b", items=0)
_all_zero = diagnostics.summarize(diagnostics.recent(line_no=3))
check("全部 0 结果 → 结构可疑", _all_zero["structure_suspect"] is True, _all_zero)
check("写明原因", "0 结果" in _all_zero["reason"], _all_zero)

diagnostics.record("search_error", line_no=4, code="SELECTOR_MISS")
_sel = diagnostics.summarize(diagnostics.recent(line_no=4))
check("选择器失败 → 结构可疑", _sel["structure_suspect"] is True, _sel)
check("错误码计数", _sel["codes"].get("SELECTOR_MISS") == 1, _sel)

print("== 3. 敏感字段与长文本 ==")
diagnostics.record("cart_add", line_no=5, ok=False, cookie="SESSION=abc", page_text="整页HTML" * 100)
_row = diagnostics.recent(line_no=5)[0]
check("cookie 不入文件", "cookie" not in _row, _row)
check("页面全文不入文件", "page_text" not in _row, _row)
check("长字符串被截断", len(str(_row.get("ok"))) < 20, _row)

print("== 4. 写盘失败不抛 ==")
_old = os.environ.get("TAOBAO_RUNS_DIR")
os.environ["TAOBAO_RUNS_DIR"] = "\\\\?\\invalid:\\no\\such\\dir"
try:
    diagnostics.record("search", line_no=9, items=1)
    check("坏路径不抛异常", True)
except Exception as exc:  # noqa: BLE001
    check("坏路径不抛异常", False, exc)
finally:
    os.environ["TAOBAO_RUNS_DIR"] = _old or str(_TMP)

print("== 5. 过期清理 ==")
_old_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 40 * 86400))
_old_file = _TMP / f"diagnostics-{_old_day}.jsonl"
_old_file.write_text('{"event":"search"}\n', encoding="utf-8")
diagnostics._prune(diagnostics._day())
check("超过保留期的诊断文件被删", not _old_file.exists(), _old_file)
_today = diagnostics.current_file()
check("当天文件保留", _today.exists(), _today)

print("== 6. 坏行不影响读取 ==")
with _today.open("a", encoding="utf-8") as fh:
    fh.write("not json\n")
check("坏行被跳过", len(diagnostics.recent()) >= 4)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
