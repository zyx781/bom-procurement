#!/usr/bin/env python3
"""同店合并（省运费）：电阻电容这种几分钱的东西不该一行一个店。

用户诉求（2026-11-20）：「买物品贵在运费，电阻电容要到同一家店去买」。

规则边界（这套测试就是边界）：
1. 被动件选店时，「本批已经买过的店」优先 —— 运费按店算，不按行算；
2. 但只在「便宜得不多」时优先：同店贵一大截，照样选便宜的（合并是为了省钱，
   不是为了合并）；
3. 分数优先模式（prefer_cheap=False）只把同店当平分时的加时赛，不压分数；
4. 店名读不到 = 不参与合并，行为跟改之前完全一样（安全降级）；
5. 汇总要能说清「几行 / 几家店 / 省了几次运费」。
"""

import sys

import engine
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
        print(f"FAIL  {name}" + (f"  [{extra}]" if extra else ""))


def eq(name, got, want):
    check(name, got == want, f"got={got!r} want={want!r}")


LINE_R = {"line_no": 3, "category": "passive", "qty": 10, "desc": "10kΩ 0402"}
LINE_IC = {"line_no": 9, "category": "ic", "qty": 1, "mpn": "STM32F103C8T6"}


def cand(shop, cost, score=80, mpn_hit=True, over=1.0, index=0):
    return {
        "title": f"{shop}-10kΩ",
        "shop": shop,
        "cost": cost,
        "score": score,
        "mpn_hit": mpn_hit,
        "over_ratio": over,
        "index": index,
    }


print("== 1. 店名归一 ==")
eq("去空白", tr._shop_key(" 深圳 华强北 电子 "), "深圳华强北电子")
eq("空值", tr._shop_key(None), "")
eq("超长截断", len(tr._shop_key("店" * 60)), 24)
check("像店名的认", tr._shop_name_ok("华强北电子旗舰店"))
check("带价格的不认", not tr._shop_name_ok("¥1.20 起"))
check("纯数字不认", not tr._shop_name_ok("1234567"))

print("== 2. 第一批：还没买过任何店，按便宜排 ==")
out = tr.same_shop_rank(
    LINE_R,
    [cand("A店", 1.2, index=1), cand("B店", 0.8, index=2)],
    shop_used={},
    freight=6.0,
)
eq("第一次还是选便宜的", out[0]["shop"], "B店")
eq("候选一个都不丢", len(out), 2)

print("== 3. 第二批：A 店已经买过 → 同店优先 ==")
out = tr.same_shop_rank(
    LINE_R,
    [cand("A店", 1.4, index=1), cand("B店", 0.9, index=2)],
    shop_used={"A店": 3},
    freight=6.0,
)
eq("同店贵一点也选同店（省一次运费）", out[0]["shop"], "A店")
eq("便宜的那家还在候选里", [c["shop"] for c in out], ["A店", "B店"])

print("== 4. 同店贵太多就不合并 ==")
out = tr.same_shop_rank(
    LINE_R,
    [cand("A店", 50.0, index=1), cand("B店", 1.0, index=2)],
    shop_used={"A店": 3},
    freight=6.0,
)
eq("贵 30 多块就买便宜的", out[0]["shop"], "B店")

print("== 5. 店名读不到 = 不参与合并（安全降级） ==")
out = tr.same_shop_rank(
    LINE_R,
    [cand("", 1.4, index=1), cand("", 0.9, index=2)],
    shop_used={"A店": 1},
    freight=6.0,
)
eq("没店名时还是按价格", [c["shop"] for c in out], ["", ""])
eq("便宜的在前面", out[0]["cost"], 0.9)

print("== 6. 只对被动件做（IC 不为了 6 块换店） ==")
out = tr.same_shop_rank(
    LINE_IC,
    [cand("A店", 30.0, index=1), cand("B店", 1.0, index=2)],
    shop_used={"A店": 5},
    freight=6.0,
)
eq("IC 行不打乱原顺序", [c["shop"] for c in out], ["A店", "B店"])

print("== 7. 运费为 0 / 关掉开关 → 完全等价于原来的排序 ==")
_in = [cand("A店", 1.4, index=1), cand("B店", 0.9, index=2)]
out = tr.same_shop_rank(
    LINE_R, list(_in), shop_used={"A店": 1}, freight=0.0,
)
eq("运费 0 时顺序原样不动", [c["shop"] for c in out], ["A店", "B店"])

_real_settings = tr.load_settings
tr.load_settings = lambda: {**_real_settings(), "same_shop_grouping": False,
                            "shipping_cost_per_shop": 6}
out = tr.same_shop_rank(
    LINE_R, list(_in), shop_used={"A店": 1}, freight=6.0,
)
eq("开关关掉就不重排", [c["shop"] for c in out], ["A店", "B店"])
tr.load_settings = _real_settings

print("== 8. 分数优先模式：同店只当平分时的加时赛 ==")
out = tr.same_shop_rank(
    LINE_R,
    [cand("A店", 1.0, score=60, index=1), cand("B店", 1.0, score=90, index=2)],
    shop_used={"A店": 9}, freight=6.0, prefer_cheap=False,
)
eq("高分仍然优先", out[0]["shop"], "B店")
out = tr.same_shop_rank(
    LINE_R,
    [cand("A店", 1.0, score=90, index=1), cand("B店", 1.0, score=90, index=2)],
    shop_used={"A店": 9}, freight=6.0, prefer_cheap=False,
)
eq("同分时同店优先", out[0]["shop"], "A店")

print("== 9. 候选上带 shop 字段（引擎要透传） ==")
_cands = [{"title": "0402 10kΩ 1%", "price": 0.8, "sales": 100, "shop": "华强北电子旗舰店",
           "free_shipping": False, "index": 0}]
scored = engine.score_candidates(LINE_R, candidates=_cands, execution=True)
eq("score_candidates 透传 shop", (scored["candidates"][0] or {}).get("shop"), "华强北电子旗舰店")
eq("score_candidates 透传包邮标记", bool(scored["candidates"][0].get("free_shipping")), False)

print("== 10. 批次汇总：几行 / 几家店 / 省几次运费 ==")
r = tr.SessionRunner()
r._batch["results"] = [
    {"line_no": 1, "status": "ok", "cost": 1.2, "shop": "A店"},
    {"line_no": 2, "status": "ok", "cost": 0.8, "shop": "A店"},
    {"line_no": 3, "status": "ok", "cost": 2.0, "shop": "A店"},
    {"line_no": 4, "status": "ok", "cost": 3.0, "shop": "B店"},
    {"line_no": 5, "status": "ok", "cost": 5.0},  # 没读到店名
    {"line_no": 6, "status": "failed", "cost": 9.9, "shop": "C店"},  # 没买成不算
]
summ = r._shop_summary()
eq("店数", summ["shop_count"], 2)
eq("行数", summ["line_count"], 4)
eq("没店名的行单独计数", summ["unknown"], 1)
eq("按行数排序", [x["shop"] for x in summ["used"]], ["A店", "B店"])
eq("每店金额合并", [x["cost"] for x in summ["used"]], [4.0, 3.0])
_freight = summ["freight_per_shop"]
eq("合并后运费 = 店数 × 单价", summ["freight_est"], round(2 * _freight, 2))
eq("不合并的运费 = 行数 × 单价", summ["freight_if_split"], round(4 * _freight, 2))
eq("省下的 = 差额", summ["freight_saved"], round(2 * _freight, 2))

print("== 11. 前端把同店结果说出来 ==")
from pathlib import Path

_app = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
check("批次收尾提到店铺数", "shops.shop_count" in _app)
check("说到同店合并省了多少", "freight_saved" in _app)
check("没读到店名要明说", "同店合并这次没生效" in _app)
check("候选行显示店名", "c.shop ? esc(c.shop)" in _app)
check("行详情显示店铺", "rec.shop || rec.candidate.shop" in _app)

print("== 12. 搜索卡片有店名才认（取不到就不参与合并） ==")
try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "Taobao-Search-Skill" / "scripts"))
    import browser_adapter as ba

    fx = ba.BrowserAdapter.__new__(ba.BrowserAdapter)
    eq("优先用卡片节点上的店名", fx._extract_shop("华强北电子旗舰店", "标题 价格"),
       "华强北电子旗舰店")
    eq("节点没读到就从卡片文本里找", fx._extract_shop("", "0402电阻 ¥1.20 深圳华强北电子旗舰店 广东深圳"),
       "华强北电子旗舰店")
    eq("标题黏在前面时也认同一家",
       fx._extract_shop("", "0402电阻1%100只深圳华强北电子旗舰店 广东深圳"),
       "华强北电子旗舰店")
    eq("卡片里也没有就空着", fx._extract_shop("", "0402电阻 ¥1.20 广东深圳"), "")
    eq("价格串不会被当店名", fx._extract_shop("¥1.20", "0402电阻"), "")
except ImportError:
    print("  -- 跳过：本地没装 playwright，跳过浏览器侧解析")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
