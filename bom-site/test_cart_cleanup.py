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
print("== 删车之后，进度必须跟着作废 ==")
# 2026-11-16 真机踩到：新 BOM 跑完 17 行（购物车 0 → 17），再点「删上次加购的」，
# 车删回 0，进度却还是 17 行 ok。接着点「继续加购」会全部跳过 —— 页面说已加购 17 行，
# 购物车里一件没有。删掉的商品，进度里就不能再算「已加购」。
import json  # noqa: E402
import tempfile  # noqa: E402

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="cartfix-"))
_saved = tr.PROGRESS_FILE
tr.PROGRESS_FILE = _tmp / "batch-progress.json"
try:
    tr.save_batch_progress(
        {
            "updated_at": "2026-11-16T20:03:15",
            "total": 3,
            "lines": {
                "1": {
                    "status": "ok",
                    "qty": 1,
                    "candidate": {
                        "title": "TYPE-C 16PIN USB连接器 Type-C母座 卧贴16P",
                        "url": "https://item.taobao.com/item.htm?id=111",
                    },
                },
                "2": {
                    "status": "ok",
                    "qty": 1,
                    "candidate": {
                        "title": "STM32F103C8T6 LQFP-48 ARM Cortex-M3 32位微控",
                        "url": "https://item.taobao.com/item.htm?id=222",
                    },
                },
                "3": {"status": "failed", "message": "没找到", "candidate": {}},
            },
        }
    )
    out = tr.invalidate_removed_progress(["TYPE-C 16PIN USB连接器 Type-C母座 卧贴16P"])
    after = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("删掉的那行改成 removed", after["lines"]["1"]["status"] == "removed", after["lines"]["1"])
    check("留在车里的行还是 ok", after["lines"]["2"]["status"] == "ok", after["lines"]["2"])
    check("failed 行不动", after["lines"]["3"]["status"] == "failed", after["lines"]["3"])
    check("计数只算真正改到的", out["updated"] == 1, out)
    check("留了删除时间", bool(after["lines"]["1"].get("removed_at")), after["lines"]["1"])

    skip = tr.done_map_from_progress(after)
    check("removed 行不会被续跑跳过", "1" not in skip, skip)
    check("还在车里的行照常跳过", "2" in skip, skip)

    # 执行层回的标题可能被截断，前缀互含要认得出是同一件
    tr.save_batch_progress(
        {
            "total": 1,
            "lines": {
                "1": {
                    "status": "ok",
                    "candidate": {"title": "原装贴片电阻0402 5.1kΩ ±1% 62.5mW 0402WGF5101TCE 100只"},
                }
            },
        }
    )
    tr.invalidate_removed_progress(["原装贴片电阻0402 5.1kΩ ±1% 62.5mW"])
    after2 = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("标题截断也能认出", after2["lines"]["1"]["status"] == "removed", after2["lines"]["1"])

    # 执行层没回标题时的兜底：目标全删掉了，这批 ok 行就都不在车里了
    tr.save_batch_progress(
        {"total": 1, "lines": {"1": {"status": "ok", "candidate": {"title": "无标题记录"}}}}
    )
    out3 = tr.invalidate_removed_progress([], all_ok=True)
    after3 = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
    check("没标题时按 all_ok 兜底", out3["updated"] == 1, out3)
    check("兜底也改成 removed", after3["lines"]["1"]["status"] == "removed", after3["lines"]["1"])

    tr.save_batch_progress({"total": 1, "lines": {"1": {"status": "ok", "candidate": {"title": "X"}}}})
    out4 = tr.invalidate_removed_progress(["完全不相干的东西啊啊啊啊"])
    check("对不上就不动（宁少改不多改）", out4["updated"] == 0, out4)
finally:
    tr.PROGRESS_FILE = _saved

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
