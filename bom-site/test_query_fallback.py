#!/usr/bin/env python3
"""搜索词回退测试：首选词候选全失败时，继续试 query_plan 后面的词。

真实现场（2026-11-18 第 24 行 1210 100µF）：
- 型号词 `C3225X7T0J107M250AB 瓷片电容` 只召回一个“标价非实价”的链接，
  打开后 SKU 无法确认；其余候选是电阻/晶振/排针噪声。
- 下一条词 `1210 100uF 6.3V 贴片电容` 其实有一堆正常货，但 search() 认为
  第一条词“有可用候选”，不会换词，整行就失败了。

这里用假 `_add_line_to_cart_once` 守住回退边界：
- 只在 failed 时换词；ok/uncertain 立刻停，避免重复加购；
- 每条词只带自己的 query_plan，不污染原行；
- 换词后的 attempts/耗时合并，报告能看到两轮都试了什么。
"""

import sys

import taobao_runner as tr

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


LINE = {
    "line_no": 24,
    "qty": 2,
    "query_plan": ["型号词", "值词", "通用词"],
    "decision": {"headline": "1210 100µF 贴片电容 ×2", "search_queries": ["型号词", "值词", "通用词"]},
}


def make_runner(results, calls):
    r = tr.SessionRunner()
    seq = list(results)

    def fake(line, prefer_cheap, max_candidates, dry_run=False):
        q = (line.get("query_plan") or [""])[0]
        calls.append({"q": q, "plan": list(line.get("query_plan") or []), "dry": dry_run})
        return dict(seq.pop(0))

    r._add_line_to_cart_once = fake
    return r


def failed(q, message="SKU 无法确认"):
    return {
        "status": "failed",
        "message": message,
        "search": {"ok": True, "query": q, "items_count": 10},
        "attempts": [{"title": f"{q}-candidate"}],
        "search_ms": 10,
        "open_ms": 20,
        "select_ms": 3,
        "add_ms": 0,
    }


def ok(q):
    return {
        "status": "ok",
        "message": "已加入购物车",
        "search": {"ok": True, "query": q, "items_count": 20},
        "attempts": [{"title": f"{q}-candidate"}],
        "search_ms": 11,
        "open_ms": 21,
        "select_ms": 4,
        "add_ms": 5,
    }


print("== 第一条失败 → 第二条成功 ==")
calls = []
r = make_runner([failed("型号词"), ok("值词"), ok("通用词")], calls)
out = r._add_line_to_cart(dict(LINE), prefer_cheap=True, max_candidates=3)
check("最终成功", out.get("status") == "ok", out)
check("只试到第二条就停", [c["q"] for c in calls] == ["型号词", "值词"], calls)
check("回退时只带一条搜索词", all(len(c["plan"]) == 1 for c in calls[1:]), calls)
check("两轮 attempts 都保留", len(out.get("attempts") or []) == 2, out.get("attempts"))
check("记录回退词", [x.get("query") for x in (out.get("fallback_queries") or [])] == ["值词"],
      out.get("fallback_queries"))
check("耗时累加", out.get("search_ms") == 21 and out.get("open_ms") == 41, out)
check("原行 query_plan 不被改写", LINE["query_plan"] == ["型号词", "值词", "通用词"], LINE)

print("== 全部失败 → 失败结论保留最后原因 ==")
calls = []
r = make_runner([failed("型号词", "第一家只询价"), failed("值词", "第二家没有这一档"),
                 failed("通用词", "第三家也没货")], calls)
out = r._add_line_to_cart(dict(LINE), prefer_cheap=True, max_candidates=3)
check("最终失败", out.get("status") == "failed", out)
check("三条词都试过", [c["q"] for c in calls] == ["型号词", "值词", "通用词"], calls)
check("原因用最后一条", out.get("message") == "第三家也没货", out.get("message"))
check("三条候选都留痕", len(out.get("attempts") or []) == 3, out.get("attempts"))
check("回退记录两条", len(out.get("fallback_queries") or []) == 2, out.get("fallback_queries"))

print("== 第二条 uncertain → 不试第三条（避免重复加购） ==")
calls = []
uncertain = {
    "status": "uncertain",
    "message": "已点击加购但页面没给确认信号",
    "search": {"ok": True, "query": "值词", "items_count": 20},
    "attempts": [{"title": "值词-candidate"}],
}
r = make_runner([failed("型号词"), uncertain, ok("通用词")], calls)
out = r._add_line_to_cart(dict(LINE), prefer_cheap=True, max_candidates=3)
check("停在 uncertain", out.get("status") == "uncertain", out)
check("没有继续第三条词", [c["q"] for c in calls] == ["型号词", "值词"], calls)

print("== 第一条搜索本身失败 → 不重复试（search 已试完 query_plan） ==")
calls = []
first_search_fail = failed("型号词", "搜索没有结果")
first_search_fail["search"] = {"ok": False, "query": None, "items_count": 0}
r = make_runner([first_search_fail], calls)
out = r._add_line_to_cart(dict(LINE), prefer_cheap=True, max_candidates=3)
check("保持 failed", out.get("status") == "failed", out)
check("只调用一次", len(calls) == 1, calls)

print("== 单条搜索词 → 不进入回退 ==")
calls = []
r = make_runner([failed("唯一词")], calls)
out = r._add_line_to_cart(
    {"line_no": 1, "qty": 1, "query_plan": ["唯一词"], "decision": {}},
    prefer_cheap=True,
    max_candidates=3,
)
check("保持 failed", out.get("status") == "failed", out)
check("只调用一次", len(calls) == 1, calls)

print("== 3MH 电感在页面闸门可核实 ==")
ind = {"params": {"inductance_h": 0.003}}
check("3MH = 3mH", tr._title_value_kinds(ind, "3MH 0.5线 10个") == {"感值"})
check("系列标题里也有 3mH", tr._title_value_kinds(ind, "1MH 2.2MH 3MH 4.7MH 直插电感") == {"感值"})
check("3000mH = 3H ≠ 3mH", tr._title_value_kinds(ind, "PK1216-3H(3000mH)") == set())

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
