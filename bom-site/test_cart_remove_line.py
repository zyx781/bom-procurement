#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行级删车测试（POST /api/taobao/cart-remove-line → SessionRunner.cart_remove_line）。

背景：整批删车（「删上次加购的」）只能整批操作。买错一件时用户要的是
「只把这行的商品从购物车删掉」—— 别的行不动，购物车里自己加的商品不动，
删完这一行改回待加购，可以单独重跑。

覆盖：
- cart_target_for_line：认哪条记录、没记录/状态不对怎么拒绝、qty 传 0 的原因；
- cart_remove_line：只改这一行的进度、内存同步、没删到也要对齐进度；
- 前置条件：批量在跑 / 会话没起 / 没有记录都明确拒绝。
"""

import json
import os
import pathlib
import tempfile
import types

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bom-cart-line-"))
os.environ["TAOBAO_RUNS_DIR"] = str(_TMP)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  ({extra})" if extra else ""))


import taobao_runner as tr  # noqa: E402


def ok_rec(line_no, title, url, qty=2):
    return {
        "line_no": line_no,
        "status": "ok",
        "headline": title,
        "qty": qty,
        "cost": 1.5,
        "candidate": {"title": title, "url": url, "price": 0.75},
    }


PROG = {
    "fingerprint": "fp",
    "total": 3,
    "started_at": "2026-11-17T17:00:00",
    "lines": {
        "1": ok_rec(1, "0603 10KΩ ±1% 贴片电阻 100只", "https://item.taobao.com/item.htm?id=111"),
        "2": {
            "status": "ok",
            "headline": "PZ254 排针",
            "qty": 2,
            "candidate": {"title": "PZ254V-12-40P 双排针 2*20P"},  # 老记录没有 url
        },
        "3": {"status": "failed", "message": "没有可用结果", "candidate": {"title": "X"}},
    },
}


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


def progress():
    return json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))


print("== 认目标：只认这一行的加购记录 ==")
picked = tr.cart_target_for_line(PROG, 1)
check("ok 行能取材", picked["ok"], picked)
check("按商品 id 精确匹配", picked["target"]["item_id"] == "111", picked["target"])
check("qty 传 0（不做整批的数量守卫）", picked["target"]["qty"] == 0, picked["target"])
check("带上行号，便于日志", picked["target"]["line_no"] == "1", picked["target"])

picked2 = tr.cart_target_for_line(PROG, 2)
check("老记录没 url 也能靠标题删", picked2["ok"] and picked2["target"]["title"].startswith("PZ254V"), picked2)

picked3 = tr.cart_target_for_line(PROG, 99)
check("没有记录 → NO_LINE_RECORD", picked3["code"] == "NO_LINE_RECORD", picked3)

picked4 = tr.cart_target_for_line(PROG, 3)
check("失败行 → NOT_IN_CART", picked4["code"] == "NOT_IN_CART", picked4)

removed_rec = {"lines": {"5": {"status": "removed", "candidate": {"title": "x"}}}}
check("已删车的行不再重复删", tr.cart_target_for_line(removed_rec, 5)["code"] == "NOT_IN_CART")

print()
print("== 前置条件 ==")
_bak_load = tr.load_batch_progress
tr.load_batch_progress = lambda: dict(PROG)
try:
    r = make_runner()
    r._batch["running"] = True
    code = err_code(lambda: r.cart_remove_line(1))
    check("批量在跑 → BATCH_RUNNING", code and code[0] == "BATCH_RUNNING", code)

    r = make_runner()
    r._proc = None
    code = err_code(lambda: r.cart_remove_line(1))
    check("会话没起 → NO_SESSION", code and code[0] == "NO_SESSION", code)

    code = err_code(lambda: make_runner().cart_remove_line(99))
    check("没有记录 → NO_LINE_RECORD", code and code[0] == "NO_LINE_RECORD", code)
finally:
    tr.load_batch_progress = _bak_load

print()
print("== 成功：只删这一件，进度只改这一行 ==")
tr.save_batch_progress(json.loads(json.dumps(PROG)))
sent = []


def _send_ok(cmd, timeout=None):
    sent.append(cmd)
    return {
        "status": "success",
        "data": {
            "removed": 1,
            "before": 6,
            "after": 5,
            "removed_titles": ["0603 10KΩ ±1% 贴片电阻 100只"],
            "message": "删除 1 件",
        },
    }


r = make_runner()
r.send = _send_ok
r._batch["results"] = [
    {"line_no": 1, "status": "ok"},
    {"line_no": 2, "status": "ok"},
]
res = r.cart_remove_line(1)
check("返回 removed=1", res.get("removed") == 1, res)
check("车内数量前后都回传", res.get("before") == 6 and res.get("after") == 5, res)
check("只发了一个删除目标", len(sent) == 1 and len(sent[0].get("items") or []) == 1, sent)
check("目标用商品 id", (sent[0]["items"][0] or {}).get("item_id") == "111", sent)
prog = progress()
check("第 1 行变 removed", prog["lines"]["1"]["status"] == "removed", prog["lines"]["1"])
check("原因写清楚要重新加购", "重新加购" in (prog["lines"]["1"].get("message") or ""), prog["lines"]["1"])
check("第 2 行结论原样不动", prog["lines"]["2"]["status"] == "ok", prog["lines"]["2"])
check("第 3 行失败记录原样不动", prog["lines"]["3"]["status"] == "failed", prog["lines"]["3"])
check("内存里只把第 1 行改成已删车", r._batch["results"][0]["status"] == "removed", r._batch["results"][0])
check("内存里第 2 行仍是已加购", r._batch["results"][1]["status"] == "ok", r._batch["results"][1])

print()
print("== 车里已经没这件：不报错，但进度必须对齐 ==")
tr.save_batch_progress(json.loads(json.dumps(PROG)))
r2 = make_runner()
r2.send = lambda cmd, timeout=None: {
    "status": "success",
    "data": {"removed": 0, "before": 4, "after": 4, "missing": [{"item_id": "111"}], "message": "找不到"},
}
res2 = r2.cart_remove_line(1)
check("没删到也返回 ok", res2.get("ok") is True, res2)
check("removed=0", res2.get("removed") == 0, res2)
prog2 = progress()
check("进度仍改成 removed（否则续跑会跳过这行）", prog2["lines"]["1"]["status"] == "removed", prog2["lines"]["1"])
check("原因说明没找到", "没找到" in (prog2["lines"]["1"].get("message") or ""), prog2["lines"]["1"])

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
