"""「只删上次加购的那几件」的单元测试。

覆盖两层：
  taobao._plan_cart_removal          执行层：哪几行该删、哪几行该跳过（纯函数）
  taobao_runner.batch_removal_targets runner 层：从进度记录里挑出目标（纯函数）

重点不是「能删」，而是「不会误删用户自己加的东西」。
"""
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
SKILL = BASE.parent / "Taobao-Search-Skill" / "scripts"
sys.path.insert(0, str(SKILL))

import taobao  # noqa: E402
import taobao_runner as tr  # noqa: E402
import engine  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def row(idx, title, qty, item_id):
    return {
        "index": idx,
        "title": title,
        "qty": qty,
        "url": f"https://item.taobao.com/item.htm?id={item_id}" if item_id else "",
    }


print("== _plan_cart_removal：按商品 id 精确匹配 ==")
rows = [
    row(0, "0603 10KΩ ±1% 贴片电阻 100只", 2, "111"),
    row(1, "0603 10KΩ ±1% 贴片电阻 100只 另一家", 1, "222"),
    row(2, "STM32F103C8T6 LQFP48", 4, "333"),
]
plan = taobao._plan_cart_removal(
    rows,
    [
        {"item_id": "111", "title": "0603 电阻", "qty": 2},
        {"item_id": "333", "title": "STM32", "qty": 4},
    ],
)
check("命中两件", len(plan["delete"]) == 2, plan)
check("同款不同卖家（222）没被误删", all(d["item_id"] != "222" for d in plan["delete"]), plan)
check("没有跳过项", plan["skip"] == [], plan["skip"])
check("没有找不到的", plan["missing"] == [], plan["missing"])

print()
print("== 数量守卫：车里比你加的多，说明你也加过 ==")
rows2 = [
    row(0, "0603 10KΩ 电阻", 3, "111"),   # 工具加了 2，车里有 3 → 用户也加过
    row(1, "0603 1KΩ 电阻", 1, "222"),    # 工具加了 1，车里 1 → 安全删
]
plan2 = taobao._plan_cart_removal(
    rows2,
    [
        {"item_id": "111", "title": "10K", "qty": 2},
        {"item_id": "222", "title": "1K", "qty": 1},
    ],
)
check("多出来的那行进 skip", len(plan2["skip"]) == 1, plan2)
check("skip 的原因写明了数量", "3 件" in plan2["skip"][0]["reason"], plan2["skip"])
check("数量一致的那行照删", [d["item_id"] for d in plan2["delete"]] == ["222"], plan2["delete"])

print()
print("== 老记录没有 url：退回标题匹配 ==")
rows3 = [row(0, "0603 10KΩ ±1% 贴片电阻 100只", 2, "")]
plan3 = taobao._plan_cart_removal(rows3, [{"item_id": "", "title": "0603 10KΩ ±1% 贴片电阻 100只", "qty": 2}])
check("标题完全一致能匹配", len(plan3["delete"]) == 1, plan3)

plan3b = taobao._plan_cart_removal(rows3, [{"item_id": "", "title": "0603 10KΩ ±1% 贴片电阻", "qty": 2}])
check("标题被淘宝改写（多后缀）也能匹配", len(plan3b["delete"]) == 1, plan3b)

print()
print("== 找不到就如实报告，不静默 ==")
plan4 = taobao._plan_cart_removal([row(0, "别的东西", 1, "999")], [{"item_id": "111", "title": "没了", "qty": 1}])
check("找不到进 missing", len(plan4["missing"]) == 1, plan4)
check("不会顺手删别的", plan4["delete"] == [], plan4)

print()
print("== 两个目标不会抢同一行 ==")
rows5 = [row(0, "唯一一行", 1, "111")]
plan5 = taobao._plan_cart_removal(
    rows5,
    [
        {"item_id": "", "title": "唯一一行", "qty": 1},
        {"item_id": "", "title": "唯一一行", "qty": 1},
    ],
)
check("只分配一次", len(plan5["delete"]) == 1, plan5)
check("另一个记为「已合并」而不是丢件", len(plan5["merged"]) == 1 and plan5["missing"] == [], plan5)

print()
print("== 空输入不炸 ==")
check("空 rows", taobao._plan_cart_removal([], [{"item_id": "1", "qty": 1}])["delete"] == [])
check("空 targets", taobao._plan_cart_removal([row(0, "x", 1, "1")], [])["delete"] == [])

print()
print("== batch_removal_targets：只认 ok 行 ==")
prog = {
    "lines": {
        "3": {"status": "ok", "candidate": {"title": "A", "url": "https://item.taobao.com/item.htm?id=111"}, "qty": 2},
        "1": {"status": "ok", "candidate": {"title": "B"}, "qty": 1},
        "2": {"status": "uncertain", "candidate": {"title": "C", "url": "https://item.taobao.com/item.htm?id=333"}, "qty": 1},
        "4": {"status": "failed", "candidate": {"title": "D"}, "qty": 1},
        "5": {"status": "ok", "candidate": {}, "qty": 1},
    }
}
targets = tr.batch_removal_targets(prog)
check("只挑出 2 条 ok", len(targets) == 2, targets)
check("uncertain 不参与删除", all(t["item_id"] != "333" for t in targets), targets)
check("按行号升序", [t["line_no"] for t in targets] == ["1", "3"], targets)
check("有 url 的解析出 item_id", any(t["item_id"] == "111" for t in targets), targets)
check("没 url 的留空 id 走标题", any(t["item_id"] == "" and t["title"] == "B" for t in targets), targets)
check("候选全空的跳过", all(t["title"] or t["item_id"] for t in targets), targets)

print()
print("== _item_id_of ==")
check("标准 item.htm", tr._item_id_of("https://item.taobao.com/item.htm?id=987&spm=x") == "987")
check("天猫 detail", tr._item_id_of("https://detail.tmall.com/item.htm?itemId=654") == "654")
check("无 id 返回空", tr._item_id_of("https://cart.taobao.com/cart.htm") == "")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
