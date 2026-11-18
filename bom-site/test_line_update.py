#!/usr/bin/env python3
"""人工放行 / 改搜索词的落盘测试（/api/lines/update 的底层 update_saved_line）。

背景：网页上把「待确认」放行、把搜索词改对之后，只改前端内存是不够的 ——
刷新/新标签页从 last-analysis.json 恢复，服务重启后「接着跑」从
batch-lines.json 恢复。两处不落盘，用户明明确认过的行还会弹待确认。
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile

# RUNS 在 import 时就定死，必须先把隔离目录设好
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bom-line-update-"))
os.environ["TAOBAO_RUNS_DIR"] = str(_TMP)

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


import taobao_runner as tr  # noqa: E402  （必须在设好 env 之后）

LA = _TMP / "last-analysis.json"
BL = _TMP / "batch-lines.json"


def _seed():
    lines = [
        {"line_no": 1, "mpn": "RC0603FR-0710KL", "status": "ready",
         "query_plan": ["10kΩ 0603 电阻"], "blocking": [], "missing": [],
         "decision": {"search_queries": ["10kΩ 0603 电阻"], "headline": "0402 10kΩ"}},
        {"line_no": 2, "mpn": "OT322550MJBA4SL", "status": "needs_user",
         "query_plan": ["OT322550MJBA4SL 晶振"], "blocking": ["缺少关键参数"],
         "missing": ["阻值/容值/感值"],
         "decision": {"search_queries": ["OT322550MJBA4SL 晶振"]}},
    ]
    LA.write_text(json.dumps({"result": {"lines": lines}}, ensure_ascii=False), encoding="utf-8")
    BL.write_text(json.dumps({"lines": [dict(l) for l in lines]}, ensure_ascii=False), encoding="utf-8")
    return lines


print("== 确认放行：status/blocking/missing 同步两份文件 ==")
_seed()
r = tr.update_saved_line(1, {"line_no": 2, "status": "ready", "blocking": [], "missing": [], "user_approved": True})
check("接口返回 ok", r.get("ok") is True, r)
la = json.loads(LA.read_text(encoding="utf-8"))["result"]["lines"]
bl = json.loads(BL.read_text(encoding="utf-8"))["lines"]
check("last-analysis 已放行", la[1]["status"] == "ready" and not la[1]["blocking"], la[1])
check("last-analysis 带人工确认标记", la[1].get("user_approved") is True)
check("batch-lines 已放行", bl[1]["status"] == "ready" and not bl[1]["blocking"], bl[1])
check("另一行不受影响", la[0]["status"] == "ready" and la[0].get("user_approved") is None)

print("== 改搜索词：query_plan 与 decision.search_queries 一起变 ==")
r = tr.update_saved_line(1, {"line_no": 2, "query_plan": ["50MHz 贴片晶振", "  OT322550MJBA4SL  晶振  ", ""]})
la = json.loads(LA.read_text(encoding="utf-8"))["result"]["lines"]
check("query_plan 已写回", la[1]["query_plan"] == ["50MHz 贴片晶振", "OT322550MJBA4SL 晶振"], la[1]["query_plan"])
check("decision 同步", la[1]["decision"]["search_queries"] == la[1]["query_plan"])
check("空白词被剔掉", "" not in la[1]["query_plan"])

print("== 防呆 ==")
_seed()
r = tr.update_saved_line(0, {"line_no": 999, "status": "ready"})  # 行号对不上 → 拒改
la = json.loads(LA.read_text(encoding="utf-8"))["result"]["lines"]
check("行号对不上不改", la[0]["status"] == "ready" and la[0].get("user_approved") is None)
check("返回 ok=False", r.get("ok") is False, r)
try:
    tr.update_saved_line(1, {"line_no": 2, "query_plan": []})
    check("空搜索词被拒绝", False)
except ValueError:
    check("空搜索词被拒绝", True)
try:
    tr.update_saved_line(1, {"line_no": 2, "query_plan": "50MHz"})
    check("非数组搜索词被拒绝", False)
except ValueError:
    check("非数组搜索词被拒绝", True)
try:
    tr.update_saved_line(1, {"line_no": 2, "status": "critical"})
    check("非法 status 被拒绝", False)
except ValueError:
    check("非法 status 被拒绝", True)
try:
    tr.update_saved_line(1, {"line_no": 2, "headline": "改不动"})
    check("白名单外字段被拒绝", False)
except ValueError:
    check("白名单外字段被拒绝", True)

print("== 标记不买：dont_buy 落盘，另一行不受影响 ==")
_seed()
r = tr.update_saved_line(0, {"line_no": 1, "dont_buy": True})
la = json.loads(LA.read_text(encoding="utf-8"))["result"]["lines"]
bl = json.loads(BL.read_text(encoding="utf-8"))["lines"]
check("last-analysis 标记不买", la[0].get("dont_buy") is True, la[0])
check("batch-lines 标记不买", bl[0].get("dont_buy") is True)
check("待确认行不受影响", la[1].get("dont_buy") is None)
r = tr.update_saved_line(0, {"line_no": 1, "dont_buy": False})
la = json.loads(LA.read_text(encoding="utf-8"))["result"]["lines"]
check("恢复采购清掉标记", la[0].get("dont_buy") is False)

print("== 文件缺失不炸 ==")
LA.unlink()
BL.unlink()
r = tr.update_saved_line(0, {"line_no": 1, "status": "ready"})
check("两份文件都不在时返回 ok=False", r.get("ok") is False, r)

shutil.rmtree(_TMP, ignore_errors=True)
print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
