#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单行执行的防呆与落盘测试（POST /api/taobao/run-line 的底层 run_line）。

背景：整批里个别行失败（换词/临时验证）时，用户不该为了 1 行重跑整批 ——
那会把已成功的行再加一遍车。单行执行必须：
- 只在会话就绪、没有批量在跑时才能跑；
- 「不买」「无搜索词」的行明确拒绝并说原因；
- 结果只覆盖这一行的进度，其余行的结论原样保留。
"""

import json
import os
import pathlib
import tempfile
import types

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bom-single-run-"))
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
        print(f"  FAIL {name}" + (f"  ({extra})" if extra else ""))


import taobao_runner as tr  # noqa: E402

LINES = [
    {
        "line_no": 1,
        "mpn": "0402WGF1001TCE",
        "desc": "1kΩ",
        "package": "0402 (1005)",
        "qty": 8,
        "row": {"params": ""},
        "decision": {"headline": "0402 1kΩ ±1% 贴片电阻 ×8", "search_queries": ["1kΩ 电阻"]},
        "query_plan": ["1kΩ 电阻"],
        "status": "ready",
    },
    {
        "line_no": 2,
        "mpn": "125X300",
        "desc": "125 X 300",
        "package": "",
        "qty": 4,
        "row": {"params": ""},
        "decision": {"headline": "125 X 300 ×4", "search_queries": []},
        "query_plan": [],
        "dont_buy": True,
        "status": "needs_user",
    },
]


def make_runner():
    r = tr.SessionRunner()
    r._proc = types.SimpleNamespace(poll=lambda: None)  # 假装会话进程活着
    r._browser_unreachable = lambda: False
    r._browser_gone = False
    return r


def err_code(fn):
    try:
        fn()
        return None
    except tr.SessionError as exc:
        return exc.code, str(exc)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc)


print("== 前置条件拦截 ==")
_bak_load = tr.load_batch_lines
tr.load_batch_lines = lambda: []
try:
    code = err_code(lambda: make_runner().run_line(line_no=1))
    check("没有行 → NO_LINES", code and code[0] == "NO_LINES", code)
finally:
    tr.load_batch_lines = _bak_load

tr.load_batch_lines = lambda: LINES
try:
    r = make_runner()
    r._batch["running"] = True
    code = err_code(lambda: r.run_line(line_no=1))
    check("批量在跑 → BATCH_RUNNING", code and code[0] == "BATCH_RUNNING", code)

    r = make_runner()
    r._proc = None
    code = err_code(lambda: r.run_line(line_no=1))
    check("会话没起 → NO_SESSION", code and code[0] == "NO_SESSION", code)

    code = err_code(lambda: make_runner().run_line(line_no=99))
    check("行号不存在 → NO_LINE", code and code[0] == "NO_LINE", code)

    code = err_code(lambda: make_runner().run_line(line_no=2))
    check("标了不买 → LINE_SKIPPED 且说明原因", code and code[0] == "LINE_SKIPPED" and "不买" in code[1], code)
finally:
    tr.load_batch_lines = _bak_load

print("== 成功：只落这一行，其它行原样保留 ==")
tr.save_batch_progress(
    {
        "updated_at": "2026-11-17T12:58:29",
        "total": 2,
        "lines": {"2": {"status": "ok", "headline": "125 X 300 ×4", "message": "旧结论"}},
        "fingerprint": tr.batch_fingerprint(LINES),
        "started_at": "2026-11-17T12:53:21",
        "finished_at": "2026-11-17T12:58:28",
    }
)
tr.load_batch_lines = lambda: LINES
try:
    r = make_runner()
    r._batch["results"] = [{"line_no": 1, "status": "failed", "attempts": []}, {"line_no": 2, "status": "ok"}]
    r._add_line_to_cart = lambda line, prefer_cheap, max_candidates, dry_run=False: {
        "line_no": line.get("line_no"),
        "headline": line["decision"]["headline"],
        "qty": line.get("qty"),
        "status": "ok",
        "message": "已加入购物车",
        "final_price": 3.5,
        "cost": 28.0,
        "sku": [{"label": "阻值", "value": "1kΩ"}],
        "query": "1kΩ 电阻",
        "attempts": [{"ok": True}],
    }
    res = r.run_line(line_no=1)
    check("返回 ok", res.get("status") == "ok", res.get("status"))
    check("结果带采购依据", isinstance(res.get("basis"), dict), type(res.get("basis")))
    check("内存批次结果同步更新", r._batch["results"][0].get("status") == "ok", r._batch["results"][0])
    prog = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("进度里第 1 行变成 ok", prog["lines"]["1"]["status"] == "ok", prog["lines"]["1"])
    check("金额/SKU 落盘", prog["lines"]["1"].get("final_price") == 3.5 and prog["lines"]["1"].get("sku"), prog["lines"]["1"])
    check("第 2 行旧结论没被抹掉", prog["lines"]["2"]["message"] == "旧结论", prog["lines"]["2"])
    check("total 保持 2", prog.get("total") == 2, prog.get("total"))
    check("指纹保留", prog.get("fingerprint") == tr.batch_fingerprint(LINES), prog.get("fingerprint"))

    print("== 失败：也落盘一条可解释的结论 ==")
    r2 = make_runner()
    r2._batch["results"] = []
    r2._add_line_to_cart = lambda line, prefer_cheap, max_candidates, dry_run=False: {
        "line_no": line.get("line_no"),
        "headline": line["decision"]["headline"],
        "qty": line.get("qty"),
        "status": "failed",
        "message": "搜索词都没有可用结果",
        "attempts": [],
    }
    res2 = r2.run_line(line_no=1)
    check("返回 failed", res2.get("status") == "failed", res2.get("status"))
    prog2 = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("失败原因落盘", "没有可用结果" in (prog2["lines"]["1"].get("message") or ""), prog2["lines"]["1"])

    print("== 异常：SessionError 转成 failed 结果而不是抛给前端 ==")
    r3 = make_runner()
    r3._batch["results"] = []

    def _boom(line, prefer_cheap, max_candidates, dry_run=False):
        raise tr.SessionError("页面被刷新了", "SEARCH_NO_RESPONSE")

    r3._add_line_to_cart = _boom
    res3 = r3.run_line(line_no=1)
    check("异常也是 failed 结果", res3.get("status") == "failed" and res3.get("code") == "SEARCH_NO_RESPONSE", res3)

    print("== skipped（不买/无搜索词）也落盘：刷新后不显示成「没跑过」 ==")
    r4 = make_runner()
    r4._batch = {
        "fingerprint": tr.batch_fingerprint(LINES),
        "total": 2,
        "started_at": "t0",
        "finished_at": None,
        "dry_run": False,
        "results": [
            {"line_no": 2, "status": "skipped", "headline": "125 X 300", "message": "跳过：人工标记不买", "attempts": []}
        ],
    }
    r4._save_progress()
    prog3 = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("skipped 落盘", prog3["lines"].get("2", {}).get("status") == "skipped", prog3["lines"].get("2"))
    check("跳过原因保留", "不买" in (prog3["lines"].get("2", {}).get("message") or ""), prog3["lines"].get("2"))
finally:
    tr.load_batch_lines = _bak_load

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
