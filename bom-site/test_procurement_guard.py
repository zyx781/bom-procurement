#!/usr/bin/env python3
"""采购正确性回归：设计值优先级、单价天花板、候选降级拦截、连接器家族。

这一组用例全部来自 2026-11-11 真机跑批时买错的现场：
  · 5kΩ / 470Ω / 2.2K 三行挂着同一个 1K 型号，全被买成 1kΩ；
  · ¥94 买回一整袋 2000 只拨动开关，BOM 只要 1 只；
  · 最优候选「加购按钮找不到」后，兜底买了 1/8W 的 1K 顶替 1/4W；
  · KH-TYPE-C-2P 买成 VH3.96 二针座，评分还是 100。
"""

import pathlib
import sys

import engine
import taobao_runner as tr

BASE = pathlib.Path(__file__).parent
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


RES_PKG = "RES-TH_BD2.3-L6.5-P10.50-D0.5"


def res_line(no, desc, mpn="MF1/4W-1KΩ±1%T52", qty=3):
    return dict(line_no=no, mpn=mpn, desc=desc, package=RES_PKG, qty=qty, refs="R1")


print("== 1. 名称列是设计值，优先于复用的旧型号 ==")
# 必须在整表上看：470Ω / 2.2K / 5kΩ / 4K7 四行的 Manufacturer Part 全是
# MF1/4W-1KΩ±1%T52（复制没改）。单行分析区分不出「型号是这颗料的」还是
# 「复制错的旧值」，也就说不出该信名称列还是信型号列。
_reuse_rows = [res_line(no, desc) for no, desc in ((2, "5kΩ"), (14, "470"), (16, "2.2K"), (20, "4K7"))]
_by_no = {ln["line_no"]: ln for ln in engine.analyze(_reuse_rows)["lines"]}
for no, desc, want in ((2, "5kΩ", 5000.0), (14, "470", 470.0), (16, "2.2K", 2200.0), (20, "4K7", 4700.0)):
    b = _by_no[no]
    got = b["params"].get("resistance_ohm")
    check(f"{desc} → {want:g}Ω", got == want, got)
    check(f"{desc} 冲突写进风险", bool(b.get("value_conflict")), b.get("value_conflict"))
    _want_txt = engine.fmt_resistance(want)
    check(f"{desc} 搜索词跟设计值走", _want_txt in str(b["query_plan"][0]), b["query_plan"][:1])

b = engine.analyze_line(res_line(21, "1K", mpn="RC0603FR-071KL"), 21)
check("名称为 1K、型号也是 1K → 不报冲突", b.get("value_conflict") is None, b.get("value_conflict"))
check("名称为 1K 时仍取 1000Ω", b["params"].get("resistance_ohm") == 1000.0, b["params"])

b = engine.analyze_line(dict(line_no=4, mpn="CC1H103ZA1ED3F4D1100", desc="10nF",
                             package="CAP-TH_L5.0-W2.5-P2.50-D0.5", qty=1, refs="C1"), 4)
check("10nF 容值仍是 10nF", abs(b["params"].get("capacitance_f", 0) - 1e-8) < 1e-12, b["params"])

print("== 1b. 名称列 vs 立创编号对不上、型号又没被复用 → 一行都不许买 ==")
# 现场来自 2026-11-16 的电源板真 BOM：
#   #6 名称 5.6Ω，编号 C325562 是 5.6kΩ（差 1000 倍）
#   #17 名称 22uF，同型号商品 C5137467 是 100nF（差 220 倍）
# 以前这两行只在风险里写一句「不自动采购」，搜索词照给，照样一路买到购物车。
_hard_cases = [
    ("5.6kΩ 编号 vs 5.6Ω 名称",
     dict(line_no=6, mpn="RMC04025.6K5%N", desc="5.6Ω", package="R0402", qty=2, refs="R5,R10"),
     {"code": "C325562", "model": "RMC04025.6K5%N", "matched_by": "code", "package": "0402",
      "params": {"resistance_ohm": 5600.0, "power_w": 0.0625}, "attrs": {"阻值": "5.6kΩ"}}),
    ("100nF 型号 vs 22uF 名称",
     dict(line_no=17, mpn="FCC0805B104K500DT", desc="22uF", package="C0805", qty=10, refs="C18"),
     {"code": "C5137467", "model": "FCC0805B104K500DT", "matched_by": "model", "package": "0805",
      "params": {"capacitance_f": 100e-9, "voltage_v": 50.0}, "attrs": {"容值": "100nF"}}),
]
for name, row, lcsc_info in _hard_cases:
    h = engine.analyze_line(dict(row, lcsc="", lcsc_info=lcsc_info), row["line_no"])
    check(f"{name}：不生成搜索词", h["query_plan"] == [], h["query_plan"])
    check(f"{name}：状态转人工", h["status"] == "needs_user", h["status"])
    check(f"{name}：blocking 说清冲突", any("对不上" in x or "冲突" in x for x in h["blocking"]), h["blocking"])
    check(f"{name}：仍留 V01 证据", any(r["code"] == "V01" for r in h["risks"]), h["risks"])

print("== 2. 纯数值解析只对电阻猜裸数字 ==")
check("470 解析成 470Ω", engine._design_value_from_text("470", "resistor") == {"resistance_ohm": 470.0})
check("4K7 解析成 4700Ω", engine._design_value_from_text("4K7", "resistor") == {"resistance_ohm": 4700.0})
check("电容裸数字不猜", engine._design_value_from_text("470", "capacitor") == {})
check("带单位照常解析", engine._design_value_from_text("10nF", "capacitor") == {"capacitance_f": 1e-8})

print("== 3. 每只单价天花板 ==")
cases = [
    ("拨动开关", dict(category="other", subtype="", mpn="SK12D07VG5", desc="SK12D07VG5",
                     package="SW-TH_SK12D07VG5-1"), 15.0),
    ("电阻", dict(category="passive", subtype="resistor", mpn="MF1/4W-1K", desc="470",
                package=RES_PKG), 2.0),
    ("二极管", dict(category="passive", subtype="diode", mpn="1N4148TR", desc="1N4148TR",
                 package="DO-35"), 5.0),
    ("Type-C 连接器", dict(category="connector", subtype="", mpn="KH-TYPE-C-2P",
                        desc="KH-TYPE-C-2P", package="USB-SMD_KH-TYPE-C-2P"), 30.0),
    ("芯片不设限", dict(category="ic", subtype="", mpn="NE555P", desc="NE555P", package="DIP-8"), None),
]
for name, line, want in cases:
    got = tr._unit_price_ceiling(line)
    check(f"{name} 上限 {want}", got == want, got)

runner = tr.SessionRunner(repo=BASE, artifacts=BASE / "tests" / "artifacts")
_switch = dict(line_no=19, category="other", subtype="", mpn="SK12D07VG5", desc="SK12D07VG5",
               package="SW-TH_SK12D07VG5-1", qty=1)
res = runner.add_to_cart(_switch, 0, [], 1, allow_empty=True, auto_qty=True, price_hint=94.0)
check("¥94 的开关被单价闸门拦下", res.get("code") == "UNIT_PRICE_ANOMALY", res)
check("单价闸门给出每只价", res.get("unit_price") == 94.0, res.get("unit_price"))
res2 = None
_bak_send = runner.send
try:
    runner.send = lambda payload: {
        "status": "ok",
        "data": {"cart_added": True, "confirmed": True, "qty": 1, "cart_verified_via": "xhr"},
    }
    res2 = runner.add_to_cart(_switch, 0, [], 1, allow_empty=True, auto_qty=True, price_hint=0.6)
finally:
    runner.send = _bak_send
check("¥0.6 的开关放行（不会被误杀）", res2.get("code") != "UNIT_PRICE_ANOMALY", res2.get("code"))

print("== 4. 连接器系列：TYPE-C 不能买成 VH ==")
c_line = engine.analyze_line(dict(line_no=6, mpn="KH-TYPE-C-2P", desc="KH-TYPE-C-2P",
                                  package="USB-SMD_KH-TYPE-C-2P", qty=1, refs="J1"), 6)
check("BOM 家族认成 TYPE-C", tr._conn_family(tr._line_blob(c_line)) == "TYPE-C")
check("VH3.96 被否决", "连接器系列不符" in (tr._candidate_veto(c_line, "VH3.96mm间距卧贴 立贴2P 贴片SMD插座") or ""))
check("TYPE-C 母座放行", tr._candidate_veto(c_line, "TYPE-C 16P 母座 贴片 USB-C") is None)

print("== 5. 淘汰级候选不自动加购 ==")
_guard_line = dict(line_no=901, category="passive", subtype="resistor", mpn="1N4148TR",
                   desc="1N4148TR", package="DO-35", qty=4, query_plan=["1N4148TR"])
_bak = (runner.search, runner.open_item, runner.add_to_cart)


def _fake_search(cands):
    return lambda line, query=None, require_scored=False: {
        "ok": True, "query": "x", "items_count": len(cands), "candidates": cands,
    }


def _fake_open(line, idx, *a, **k):
    return {
        "ok": True, "item": {"title": f"item{idx}"}, "detail_price": 1.0,
        "pack_size": None, "suggested_qty": 1, "actual_pcs": 1, "unit_price": 1.0,
        "sku_groups": [], "suggestions": [], "unresolved": [], "no_sku": True,
    }


try:
    runner.open_item = _fake_open
    runner.add_to_cart = lambda *a, **k: {"ok": True, "message": "已加入购物车",
                                          "final_price": 1.0, "qty_sent": 1, "cost": 1.0,
                                          "pack_size": None, "actual_pcs": 1, "unit_price": 1.0}
    runner.search = _fake_search([
        {"index": 0, "title": "1N4148TR 开关二极管 DO-35", "price": 1.0, "score": 34.9,
         "decision": "淘汰", "hits": ["封装 DO-35"], "misses": ["容值"]},
    ])
    out = runner._add_line_to_cart(dict(_guard_line), prefer_cheap=True, max_candidates=3)
    check("淘汰级候选不加购", out.get("status") == "failed", out.get("status"))
    check("给出淘汰原因", "淘汰级" in (out.get("message") or ""), out.get("message"))

    print("== 6. 兜底候选少关键规格项就不买 ==")
    _calls = []
    runner.open_item = lambda line, idx, *a, **k: (_calls.append(idx), _fake_open(line, idx))[1]
    runner.add_to_cart = lambda *a, **k: {"ok": False, "code": "CART_ADD_FAILED",
                                          "message": "Add-to-cart button not found"}
    runner.search = _fake_search([
        {"index": 0, "title": "插件 470Ω ±1% 1/4W 金属膜电阻", "price": 1.0, "score": 97.9,
         "decision": "主选", "hits": ["封装 插件", "阻值 470Ω", "功率 ≥ 0.25W"]},
        {"index": 1, "title": "1K 1千欧 1/8W金属膜电阻 1% 五色环 插件", "price": 1.0,
         "score": 65.4, "decision": "备选", "hits": ["封装 插件"]},
    ])
    out = runner._add_line_to_cart(dict(_guard_line), prefer_cheap=True, max_candidates=3)
    check("规格不符的兜底没被打开", _calls == [0], _calls)
    check("整行判失败而不是买错", out.get("status") == "failed", out.get("status"))
    check("写明缺哪个规格项", "阻值" in (out.get("message") or ""), out.get("message"))

    print("== 7. 单价异常换下一家 ==")
    runner.open_item = _fake_open
    _n = {"i": 0}

    def _add_by_index(*a, **k):
        _n["i"] += 1
        if _n["i"] == 1:
            return {"ok": False, "code": "UNIT_PRICE_ANOMALY", "message": "折合每只 ¥94，疑似整袋报价",
                    "unit_price": 94.0, "pack_size": None}
        return {"ok": True, "message": "已加入购物车", "final_price": 0.6, "qty_sent": 1,
                "cost": 0.6, "pack_size": None, "actual_pcs": 1, "unit_price": 0.6}

    runner.add_to_cart = _add_by_index
    runner.search = _fake_search([
        {"index": 0, "title": "SK12D07VG5 拨动开关 2000只/袋", "price": 94.0, "score": 100.0,
         "decision": "主选", "hits": ["封装 插件"]},
        {"index": 1, "title": "SK12D07VG5 拨动开关 单只", "price": 0.6, "score": 96.0,
         "decision": "主选", "hits": ["封装 插件"]},
    ])
    out = runner._add_line_to_cart(dict(_switch), prefer_cheap=True, max_candidates=3)
    check("换到下一家并成功", out.get("status") == "ok", out.get("status"))
    check("最终单价 0.6", out.get("unit_price") == 0.6, out.get("unit_price"))
finally:
    runner.search, runner.open_item, runner.add_to_cart = _bak

print("== 8. 三道钱闸门：演练与真跑共用同一套 ==")
_res = dict(line_no=801, category="passive", subtype="resistor", mpn="R", desc="470Ω",
            package="插件", qty=42, params={"resistance_ohm": 470.0})
check("每只 ¥94 被单价闸拦", (tr._money_guards(_res, 94.0, 1, 1, 1) or {}).get("code") == "UNIT_PRICE_ANOMALY")
check("¥2.48/10 只折合 0.248 放行", tr._money_guards(_res, 2.48, 10, 1, 10) is None)
check("单行金额超上限被拦",
      (tr._money_guards(_res, 1.9, 1, 200, 200) or {}).get("code") == "COST_TOO_HIGH")
check("金额只认实价（标题价不能替它开后门）",
      (tr._money_guards(_res, 90.0, 1, 1, 1, amount=90.0) or {}).get("code") == "UNIT_PRICE_ANOMALY")
_sw_line = dict(line_no=802, category="other", refs="SW1", subtype="", mpn="SK12D07VG5",
                desc="拨动开关", package="SW-TH", qty=1, params={})
check("开关单价天花板 15", tr._unit_price_ceiling(_sw_line) == 15.0)
check("¥90 整袋拨动开关被拦",
      (tr._money_guards(_sw_line, 90.0, 1, 1, 1) or {}).get("code") == "UNIT_PRICE_ANOMALY")
check("电阻单价天花板 2", tr._unit_price_ceiling(_res) == 2.0)

print("== 9. SKU 选中值：同系列不同号＝降规格 ==")
_ss = dict(line_no=803, mpn="SS34", desc="SS34", params={})
check("SS26 顶 SS34 被拒", tr._sku_model_conflict(_ss, "SR260贴片SMA丝印SS26（20只）") == "SS26")
check("丝印 SS34 的等效件放行", tr._sku_model_conflict(_ss, "1N5822贴片SMA丝印SS34（10只）") == "")
check("纯 SS34 放行", tr._sku_model_conflict(_ss, "SS34（20只）") == "")
_ic = dict(line_no=804, mpn="NE555P", desc="NE555P", params={})
check("NE555 ⊂ NE555P 不算冲突", tr._sku_model_conflict(_ic, "国产NE555 直插DIP-8") == "")
_conn = dict(line_no=805, mpn="KH-TYPE-C-2P", desc="", params={})
check("带连字符的型号不误判", tr._sku_model_conflict(_conn, "KH-TYPE-C-2P（2个）") == "")

print("== 10. 值核实：SKU 写法要认得出来 ==")
check("5KΩ 认得出", tr._title_value_kinds({"params": {"resistance_ohm": 5000.0}}, "5KΩ") == {"阻值"})
check("2K2 认得出", tr._title_value_kinds({"params": {"resistance_ohm": 2200.0}}, "2K2") == {"阻值"})
check("1/4W 470R 认得出",
      tr._title_value_kinds({"params": {"resistance_ohm": 470.0}}, "1/4W 470R(100只)") == {"阻值"})
check("多值标题不误认 5K",
      tr._title_value_kinds({"params": {"resistance_ohm": 5000.0}}, "4.7K 5.1K 5.6K") != {"阻值"})
check("维修套件认不出 95µF",
      tr._title_value_kinds({"params": {"capacitance_f": 95e-6}}, "维修八件套 0.85/0.9/0.95/1.0UF") != {"容值"})

print("== 11. 材料/结构闸门：值对不等于装得上 ==")
# 2026-11-16 演练现场：470Ω 买成「绕线 1W」、10nF 买成「3KV 高压瓷片」、
# 100nF 买成「P=5.08 独石」——值闸门全放行，因为阻值/容值确实对得上。
_res14 = dict(line_no=901, category="passive", subtype="resistor", mpn="MF1/4W-1KΩ±1%T52",
              desc="470", package="插件", qty=42, params={"resistance_ohm": 470.0, "power_w": 0.25},
              row={"lcsc_info": {"attrs": {"电阻类型": "金属膜电阻", "阻值": "1kΩ"}}})
check("绕线 1W 顶金属膜 1/4W 被拒",
      "绕线" in tr._material_conflict(_res14, "100R-470R 1W/2W/3W插件绕线电阻器"))
check("水泥电阻同样被拒",
      "绕线" in tr._material_conflict(_res14, "470Ω 5W 水泥电阻"))
check("金属膜 2W 放行（功率可放大，体积还能焊）",
      tr._material_conflict(_res14, "2W金属膜电阻1% 470R") == "")
check("1/4W 金属膜正常放行",
      tr._material_conflict(_res14, "1/4W 470Ω 金属膜电阻 1%") == "")
check("功率离谱（5W）被拒",
      "体积" in tr._material_conflict(_res14, "470Ω 5W 金属膜电阻"))

_cap4 = dict(line_no=902, category="passive", subtype="capacitor", mpn="CC1H103", desc="10nF",
             package="插件", qty=1, params={"capacitance_f": 1e-8, "voltage_v": 50.0},
             row={"lcsc_info": {"attrs": {"额定电压": "50V", "脚间距": "2.54mm"}}})
check("3KV 高压瓷片顶 50V 被拒",
      "耐压" in tr._material_conflict(_cap4, "LY E3KV 103M 3KV103 10nF 3000V 高压瓷片电容"))
check("50V 瓷片放行",
      tr._material_conflict(_cap4, "10nF 50V 瓷片电容 P=2.54mm") == "")
check("100V 放行（2 倍以内）",
      tr._material_conflict(_cap4, "10nF 100V 瓷片电容") == "")
_cap7 = dict(line_no=903, category="passive", subtype="capacitor", mpn="CC1H103", desc="100nF",
             package="插件", qty=14, params={"capacitance_f": 1e-7, "voltage_v": 50.0},
             row={"lcsc_info": {"attrs": {"额定电压": "50V", "脚间距": "2.54mm"}}})
check("脚距 5.08 顶 2.54 被拒",
      "脚间距" in tr._material_conflict(_cap7, "独石电容 插件,P=5.08mm 100nF 50V"))
check("不写脚距不误杀",
      tr._material_conflict(_cap7, "100nF 50V 瓷片电容") == "")

print("== 12. BOM 自身矛盾：非标准值不自动买 ==")
check("5kΩ 不在 E24", tr._is_standard_value(5000.0) is False)
check("470Ω 是标准档", tr._is_standard_value(470.0) is True)
check("2.2kΩ 是标准档", tr._is_standard_value(2200.0) is True)
check("100nF 是标准档", tr._is_standard_value(1e-7) is True)
check("判不了（空值）不误伤", tr._is_standard_value(None) is True)
_conflict_line = dict(line_no=904, category="passive", subtype="resistor", mpn="MF1/4W-1KΩ±1%T52",
                      desc="5K", qty=3, params={"resistance_ohm": 5000.0, "power_w": 0.25},
                      value_conflict="名称列写 5K，型号对应 1kΩ")
check("5kΩ 冲突行要人工核对", "E24" in tr._review_reason(_conflict_line))
_ok_conflict = dict(_conflict_line, desc="470", params={"resistance_ohm": 470.0})
check("470Ω 冲突行可按名称列采购", tr._review_reason(_ok_conflict) == "")
check("无冲突行不干涉", tr._review_reason(dict(_conflict_line, value_conflict="")) == "")

print("== 13. 电阻类型进搜索词 ==")
_q = engine._build_queries("passive", "插件", {"resistance_ohm": 470.0, "power_w": 0.25},
                           "MF1/4W-1KΩ±1%T52", "470", "", "resistor", res_type="金属膜电阻")
check("搜索词带金属膜", any("金属膜" in x for x in _q), _q[:4])
_q2 = engine._build_queries("passive", "插件", {"resistance_ohm": 470.0},
                            "R", "470", "", "resistor")
check("没核准类型就不乱加词", not any("金属膜" in x for x in _q2), _q2[:3])

print("== 14. 电容族系：电解 ≠ 瓷片 ≠ 启动电容 ==")
# 95µF 搜出 CBB65 空调压缩机启动电容：容值一样，但不是同一个元件
_elec = dict(line_no=905, category="passive", subtype="capacitor", mpn="PA100V100M10x12",
             desc="95uF", qty=1, params={"capacitance_f": 95e-6},
             row={"package": "CAP-TH_BD10.0-P5.00-D0.6-FD_2"})
_ceramic = dict(line_no=906, category="passive", subtype="capacitor", mpn="CC1H103ZA1ED3F4D1100",
                desc="10nF", qty=1,
                params={"capacitance_f": 10e-9, "voltage_v": 50.0},
                row={"package": "CAP-TH_L5.0-W2.5-P2.50-D0.5",
                     "lcsc_info": {"attrs": {"温度系数": "Y5V", "额定电压": "50V"}}})
_polar = dict(line_no=907, category="passive", subtype="capacitor", mpn="ERG50V22M5X11",
              desc="22uF", qty=1, params={"capacitance_f": 22e-6},
              row={"package": "CAP-TH_BD5.0-P2.00", "lcsc_info": {"attrs": {"极性类型": "有极性"}}})
check("圆柱封装判电解", tr._bom_cap_family(_elec) == "电解")
check("方形封装判瓷片", tr._bom_cap_family(_ceramic) == "瓷片")
check("立创极性判电解", tr._bom_cap_family(_polar) == "电解")
check("电解行拒绝 CBB65 启动电容",
      "CBB" in tr._material_conflict(_elec, "苏一CBB65A防爆空调压缩机启动电容器25/30/40/50/60/80UF450V"))
check("电解行拒绝薄膜电容", tr._material_conflict(_elec, "薄膜电容 95UF 450V") != "")
check("电解行放过电解电容", tr._material_conflict(_elec, "95UF 100V 铝电解电容 10x12") == "")
check("瓷片行拒绝电解电容", tr._material_conflict(_ceramic, "10nF 50V 铝电解电容") != "")
check("瓷片行放过瓷片电容",
      tr._material_conflict(_ceramic, "50V插件瓷片瓷介电容 101 102 103 104 10NF") == "")

print("== 15. 电容搜索词带耐压 ==")
_qc = engine._build_queries("passive", "插件", {"capacitance_f": 10e-9, "voltage_v": 50.0},
                            "CC1H103", "10nF", "", "capacitor", cap_kind="瓷片")
check("搜索词带瓷片", any("瓷片" in x and "10nF" in x for x in _qc), _qc[:3])
check("瓷片词同时带 50V", any("瓷片" in x and "50V" in x for x in _qc), _qc[:3])

print("== 16. 立创编号锚定：按编号确定具体那一颗 ==")
# 编号是物料的唯一标识：查到型号就该拿型号去搜，而不是只按「10nF 瓷片」碰运气
_lc = {
    "code": "C2832505",
    "model": "CC1H103ZA1ED3F4D1100",
    "matched_by": "code",
    "attrs": {"容值": "10nF", "额定电压": "50V", "温度系数": "Y5V", "脚间距": "2.54mm"},
    "params": {"capacitance_f": 10e-9, "voltage_v": 50.0},
    "desc": "10nF -20%~+80% 50V Ceramic Capacitor Through Hole P=2.54mm",
    "catalog": "Ceramic Capacitors",
}
_row = {
    "line_no": 1, "mpn": "CC1H103ZA1ED3F4D1100", "desc": "10nF",
    "package": "CAP-TH_L5.0-W2.5-P2.50", "qty": 1, "refs": "C13", "lcsc_info": _lc,
}
_l = engine.analyze_line(_row, 1)
check("编号行的第一条搜索词是型号", "CC1H103ZA1ED3F4D1100" in _l["query_plan"][0], _l["query_plan"][:2])
check("型号词带中文品类", "瓷片" in _l["query_plan"][0], _l["query_plan"][:2])
check("封装噪声 Through 不进搜索词", all("Through" not in q for q in _l["query_plan"]), _l["query_plan"])
check("参数词仍作为兜底保留", any("10nF" in q and "50V" in q for q in _l["query_plan"]), _l["query_plan"][:3])

_l2 = engine.analyze_line(dict(_row, line_no=2, lcsc_info=dict(_lc, matched_by="model")), 2)
check(
    "同型号反查不当采购依据（型号可能是复制错的）",
    not any("CC1H103" in q and "瓷片" in q for q in _l2["query_plan"]),
    _l2["query_plan"][:2],
)

_lc_d = {
    "code": "C7420365", "model": "SS34", "matched_by": "code",
    "desc": "Diode Independent 40V 3A Surface Mount SMA(DO-214AC)",
    "catalog": "Single Diodes", "attrs": {"整流电流": "3A"},
    "params": {}, "package": "SMA(DO-214AC)",
}
_l3 = engine.analyze_line(
    {"line_no": 3, "mpn": "SS34", "desc": "SS34", "package": "SMA(DO-214AC)", "qty": 1, "lcsc_info": _lc_d}, 3
)
check("二极管型号词配「二极管」", "二极管" in _l3["query_plan"][0], _l3["query_plan"][:2])
check("二极管行不出现「电感」", all("电感" not in q for q in _l3["query_plan"]), _l3["query_plan"])

_conf = {
    "line_no": 4, "mpn": "MF1/4W-1KΩ±1%T52", "desc": "470", "package": RES_PKG, "qty": 1,
    "value_conflict": "名称列写 470，型号对应 1kΩ，两者冲突",
    "lcsc": {"code": "C713997", "model": "MF1/4W-1KΩ±1%T52", "matched_by": "code"},
    "params": {"resistance_ohm": 470.0},
}
check("编号与名称列冲突 → 必须人工，不自动买", "冲突" in tr._review_reason(_conf), tr._review_reason(_conf))

_b1 = tr._procurement_basis(
    {"lcsc": {"code": "C2832505", "model": "CC1H103ZA1ED3F4D1100", "matched_by": "code"}},
    {"candidate": {"title": "CC1H103ZA1ED3F4D1100 瓷片电容 10nF 50V"}},
)
check("采购依据：标题命中型号", _b1.get("hit") is True and _b1.get("code") == "C2832505", _b1)
_b2 = tr._procurement_basis(
    {"lcsc": {"code": "C2832505", "model": "CC1H103ZA1ED3F4D1100", "matched_by": "code"}},
    {"candidate": {"title": "10nF 50V 瓷片电容 插件"}},
)
check("采购依据：未命中型号如实写「同规格替代」", _b2.get("hit") is False and "替代" in _b2.get("note", ""), _b2)
check(
    "同型号反查不产生采购依据",
    tr._procurement_basis({"lcsc": {"code": "C1", "model": "X", "matched_by": "model"}}) == {},
)
check("老缓存无 matched_by 按编号解析处理", engine._lcsc_by_code({"code": "C1"}) is True)
check("同型号记录不被当成编号", engine._lcsc_by_code({"code": "C1", "matched_by": "model"}) is False)

# 同一型号被用在两个差一个数量级的值上（实测 #4 10nF / #7 100nF 共用 C2832505）：
# 一个型号不可能有两种容值，八成是复制错型号 —— 不按名称硬买，交人工。
_conf7 = {
    "line_no": 7, "mpn": "CC1H103ZA1ED3F4D1100", "desc": "100nF",
    "package": "CAP-TH_L5.0-W2.5-P2.50", "qty": 14,
    "value_conflict": "BOM 名称列写 100nF，但立创同型号商品 C2832505 是 10nF，两者对不上",
    "lcsc": {"code": "C2832505", "model": "CC1H103ZA1ED3F4D1100", "matched_by": "model",
             "params": {"capacitance_f": 10e-9}},
    "params": {"capacitance_f": 100e-9},
}
check("同型号容值差 10 倍 → 人工（复制错型号）", "10 倍" in tr._review_reason(_conf7), tr._review_reason(_conf7))

_conf14 = {
    "line_no": 14, "mpn": "MF1/4W-1KΩ±1%T52", "desc": "470", "package": RES_PKG, "qty": 42,
    "value_conflict": "名称列写 470，型号对应 1kΩ，两者冲突",
    "lcsc": {"code": "C713997", "model": "MF1/4W-1KΩ±1%T52", "matched_by": "model",
             "params": {"resistance_ohm": 1000.0}},
    "params": {"resistance_ohm": 470.0},
}
check("同型号只差 2 倍 → 按名称列买，不拦", tr._review_reason(_conf14) == "", tr._review_reason(_conf14))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
