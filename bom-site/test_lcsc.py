#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""立创商城（LCSC）器件查询回归。

全部离线：网络层用注入的假 fetcher，不打真接口、不依赖外网。

用例来源：
  · 2026-11-12 用户从嘉立创EDA 导出的 BOM —— 表头是
    ID / Name / Designator / Footprint / Quantity / Manufacturer Part /
    Manufacturer / Supplier / Supplier Part，`Supplier Part` 就是立创编号 C46749；
  · 官方 BOM 规范：「编号与 Comment/封装 冲突时以编号为准」「编号错比编号缺更危险」；
  · 现场买错的三行：5kΩ / 470 / 2.2K 挂着同一个 1K 型号；
    #7 名称写 100nF 却复用了 #4 的 10nF 型号。
"""

import json
import os
import pathlib
import sys
import tempfile

os.environ["TAOBAO_RUNS_DIR"] = tempfile.mkdtemp(prefix="bom-lcsc-test-")

import engine  # noqa: E402
import lcsc  # noqa: E402

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


def detail(code, model, brand, pkg, intro, params, stock=1000, packet="Reel", unit="Piece"):
    """构造一份和真实接口同形的详情返回。"""
    return {
        "code": 200,
        "result": {
            "productCode": code,
            "productModel": model,
            "brandNameEn": brand,
            "encapStandard": pkg,
            "productIntroEn": intro,
            "stockNumber": stock,
            "minPacketUnit": packet,
            "productUnit": unit,
            "pdfUrl": "https://datasheet.example/x.pdf",
            "catalogName": intro.split()[-1] if intro else "",
            "parentCatalogList": [{"catalogNameEn": "Passives"}, {"catalogNameEn": "Resistors"}],
            "paramVOList": [
                {"paramName": k, "paramValue": v} for k, v in params
            ],
        },
    }


def list_result(items):
    return {"code": 200, "result": {"dataList": items}}


RES = detail(
    "C25804", "0603WAF1002T5E", "UNI-ROYAL", "0603",
    "10kΩ ±1% 100mW 0603 Thick Film Resistor",
    [("功率", "100mW"), ("阻值", "10kΩ"), ("最大工作电压", "75V"),
     ("电阻类型", "厚膜电阻"), ("温度系数", "±100ppm/℃"), ("精度", "±1%")],
    stock=3712000,
)


# --------------------------------------------------------------------------
print("== 1. 立创编号识别 ==")
check("标准写法", lcsc.normalize_code("C46749") == "C46749")
check("小写", lcsc.normalize_code("c46749") == "C46749")
check("带空格", lcsc.normalize_code(" C 46749 ") == "C46749")
check("中文「无」不算编号", lcsc.normalize_code("无") == "")
check("横杠不算编号", lcsc.normalize_code("-") == "")
check("纯数字不算编号", lcsc.normalize_code("46749") == "")
check("位数太少不算编号", lcsc.normalize_code("C12") == "")
check("None 安全", lcsc.normalize_code(None) == "")


print("== 2. 型号归一化（只统一写法，不做模糊） ==")
check("空白/大小写", lcsc.normalize_model(" ss34 ") == lcsc.normalize_model("SS34"))
check("µ 与 u 等价", lcsc.normalize_model("22µF") == lcsc.normalize_model("22uF"))
check("OHM 写成 Ω", lcsc.normalize_model("10KOHM") == lcsc.normalize_model("10KΩ"))
check("不同型号不相等", lcsc.normalize_model("MF1/4W-1KΩ") != lcsc.normalize_model("MF1/4W-2KΩ"))


print("== 3. 立创参数 → 引擎参数 ==")
attrs = {
    "阻值": "10kΩ", "功率": "100mW", "精度": "±1%", "最大工作电压": "75V",
    "容值": "22uF", "额定电压": "50V", "耗散功率(Pd)": "700mW",
    "额定电流": "500mA", "触点数量": "2P", "脚间距": "2.54mm",
    "安装方式": "直插", "工作温度": "-55℃~+155℃",
}
p = lcsc.extract_params(attrs)
check("阻值 10kΩ → 10000", p.get("resistance_ohm") == 10000.0, p.get("resistance_ohm"))
check("功率 100mW → 0.1", p.get("power_w") == 0.1, p.get("power_w"))
check("精度 ±1% → 1", p.get("tolerance_pct") == 1.0)
check("耐压 75V → 75", p.get("voltage_v") == 75.0)
check("容值 22uF → 22µF", abs(p.get("capacitance_f", 0) - 22e-6) < 1e-12)
check("耗散功率带括号也认", lcsc.extract_params({"耗散功率(Pd)": "700mW"}).get("power_w") == 0.7)
check("额定电流 500mA → 0.5", lcsc.extract_params({"额定电流": "500mA"}).get("current_a") == 0.5)
check("触点数量 2P → 2", lcsc.extract_params({"触点数量": "2P"}).get("pins") == 2)
check("未知参数不臆造", "channel" not in lcsc.extract_params({"颜色": "红"}))
check("「-」占位不解析", lcsc.extract_params({"耗散功率(Pd)": "-"}).get("power_w") is None)
check("安装方式直插 → 插件", lcsc.mount_from_attrs({"安装方式": "直插"}) == "插件")
check("安装方式贴片 → 贴片", lcsc.mount_from_attrs({"安装方式": "贴片"}) == "贴片")


print("== 4. 编号解析（注入 fetcher，不联网） ==")
calls = []


def fake_fetch(url, payload=None, timeout=10):
    calls.append(url)
    if "C25804" in url:
        return RES
    return {"code": 200, "result": None}


rec = lcsc.resolve("C25804", fetch=fake_fetch)
check("型号解析出来", (rec or {}).get("model") == "0603WAF1002T5E", rec and rec.get("model"))
check("品牌", (rec or {}).get("brand") == "UNI-ROYAL")
check("封装", (rec or {}).get("package") == "0603")
check("阻值进入 params", (rec or {}).get("params", {}).get("resistance_ohm") == 10000.0)
check("库存带出来", (rec or {}).get("stock") == 3712000)
check("参数原文保留", (rec or {}).get("attrs", {}).get("温度系数") == "±100ppm/℃")
check("来源标记", (rec or {}).get("source") == "lcsc")

n_before = len(calls)
rec2 = lcsc.resolve("C25804", fetch=fake_fetch)
check("第二次走缓存不打网络", len(calls) == n_before, f"{n_before} → {len(calls)}")
check("缓存结果一致", (rec2 or {}).get("model") == "0603WAF1002T5E")

check("非法编号直接返回 None", lcsc.resolve("无", fetch=fake_fetch) is None)


def boom(url, payload=None, timeout=10):
    raise OSError("network down")


check("网络异常返回 None，不抛", lcsc.resolve("C999999", fetch=boom) is None)
check("查无此件返回 None", lcsc.resolve("C999998", fetch=fake_fetch) is None)


print("== 5. 型号精确匹配（模糊命中一律放弃） ==")
sent = []


def model_fetch(url, payload=None, timeout=10):
    sent.append((url, payload))
    if "query/list" in url:
        return list_result([
            {"productCode": "C713997", "productModel": "MF1/4W-1KΩ±1%T52"},
            {"productCode": "C111111", "productModel": "MF1/4W-2KΩ±1%T52"},
        ])
    if "C713997" in url:
        return detail("C713997", "MF1/4W-1KΩ±1%T52", "Huaxing", "Through Hole,D2.3xL6.5mm",
                      "1kΩ 250mW Metal Film Resistor",
                      [("阻值", "1kΩ"), ("功率", "250mW"), ("精度", "±1%")])
    return {"code": 200, "result": None}


m = lcsc.resolve_model("MF1/4W-1KΩ±1%T52", fetch=model_fetch)
check("型号命中", (m or {}).get("code") == "C713997", m and m.get("code"))
check("标记 matched_by=model", (m or {}).get("matched_by") == "model")
check("阻值 1kΩ", (m or {}).get("params", {}).get("resistance_ohm") == 1000.0)
check("检索用原始型号（不能发归一化后的「欧」）",
      sent and sent[0][1].get("keyword") == "MF1/4W-1KΩ±1%T52", sent[:1])

sent.clear()
check("模糊命中不认（型号对不上就放弃）",
      lcsc.resolve_model("MF1/4W-3K3Ω±1%T52", fetch=model_fetch) is None)
check("不存在的型号放弃", lcsc.resolve_model("NOSUCHPART-123", fetch=model_fetch) is None)


print("== 6. 批量富化 enrich_rows ==")
# 这一段要验证「会去查」，所以显式打开网络层；用的是假 fetcher，不会真联网。
# （跑全量回归时常把 LCSC_OFFLINE=1 设进环境，这里必须先摘掉）
os.environ.pop("LCSC_OFFLINE", None)
lcsc.clear_cache()
rows = [
    {"line_no": 1, "mpn": "0603WAF1002T5E", "lcsc": "C25804"},
    {"line_no": 2, "mpn": "0603WAF1002T5E", "lcsc": "C25804"},   # 同编号，只查一次
    {"line_no": 3, "mpn": "", "lcsc": ""},
]
n0 = len(calls)
st = lcsc.enrich_rows(rows, fetch=fake_fetch)
check("两行都挂上 lcsc_info", all(r.get("lcsc_info") for r in rows[:2]))
check("无编号行不挂", rows[2].get("lcsc_info") is None)
check("同编号只请求一次", len(calls) - n0 == 1, len(calls) - n0)
check("统计按行计", st.get("by_code") == 2, st)

os.environ["LCSC_OFFLINE"] = "1"
lcsc.clear_cache()
calls.clear()
st_off = lcsc.enrich_rows([{"line_no": 1, "lcsc": "C25804"}], fetch=fake_fetch)
check("离线不打网络", len(calls) == 0, calls)
check("离线统计为 0 行", st_off.get("resolved") == 0)
os.environ.pop("LCSC_OFFLINE", None)


print("== 7. 引擎接入：立创核准后的选型行为 ==")
RES_PKG = "RES-TH_BD2.3-L6.5-P10.50-D0.5"


def analyze_one(row):
    return engine.analyze([row])["lines"][0]


# 7.1 编号与型号列一致：参数以立创为准
r1 = {
    "line_no": 1, "mpn": "KH-TYPE-C-2P", "desc": "KH-TYPE-C-2P",
    "package": "USB-SMD_KH-TYPE-C-2P", "qty": 1, "refs": "CN1", "lcsc": "C2919656",
    "lcsc_info": {
        "code": "C2919656", "model": "KH-TYPE-C-2P", "brand": "kinghelm",
        "package": "Through Hole", "matched_by": "code",
        "params": {"pins": 2}, "attrs": {"连接器类型": "Type-C", "触点数量": "2P"},
    },
}
l1 = analyze_one(r1)
check("连接器认成 connector", l1["category"] == "connector", l1["category"])
check("针数来自立创", l1["params"].get("pins") == 2, l1["params"])
check("连接器类型进闸门",
      any("Type-C" in g for g in l1["hard_gates"]), l1["hard_gates"])
check("搜索词带「母座」，不会召回到公头/端子",
      any("母座" in q for q in l1["query_plan"]), l1["query_plan"])
check("lcsc 徽标带编号", (l1.get("lcsc") or {}).get("code") == "C2919656")

# 7.2 名称列 vs 立创：同一型号两个设计值 → V01
r2 = {
    "line_no": 7, "mpn": "CC1H103ZA1ED3F4D1100", "desc": "100nF",
    "package": "CAP-TH_L5.0-W2.5-P2.50-D0.5", "qty": 14, "refs": "C7",
    "lcsc": "",  # 无编号，靠型号匹配
    "lcsc_info": {
        "code": "C2832505", "model": "CC1H103ZA1ED3F4D1100", "matched_by": "model",
        "package": "Through Hole,P=2.54mm", "attrs": {"容值": "10nF"},
        "params": {"capacitance_f": 10e-9, "voltage_v": 50.0},
    },
}
l2 = analyze_one(r2)
v01 = next((r["text"] for r in l2["risks"] if r["code"] == "V01"), "")
check("报 V01 冲突", bool(v01), l2["risks"])
check("冲突说明里有立创同型号商品", "立创同型号商品" in v01, v01)
check("冲突说明点出 10nF", "10nF" in v01 or "10nF" in str(l2["params"]), v01)
check("仍按名称列（100nF）采购", abs(l2["params"].get("capacitance_f", 0) - 100e-9) < 1e-12,
      l2["params"].get("capacitance_f"))

# 7.3 型号列与编号不符 → 以编号为准，记 V02
r3 = {
    "line_no": 9, "mpn": "OLD-WRONG-PN", "desc": "SS34", "package": "SMA", "qty": 1,
    "lcsc": "C7420365",
    "lcsc_info": {"code": "C7420365", "model": "SS34", "matched_by": "code",
                  "package": "SMA(DO-214AC)", "params": {}, "attrs": {}},
}
l3 = analyze_one(r3)
check("型号改成立创的", l3["mpn"] == "SS34", l3["mpn"])
check("记 V02", any(r["code"] == "V02" for r in l3["risks"]), l3["risks"])

# 7.4 芯片的电参数不许污染摘要行
r4 = {
    "line_no": 13, "mpn": "CD4511BE", "desc": "CD4511BE", "package": "DIP-16", "qty": 6,
    "lcsc": "C507153",
    "lcsc_info": {"code": "C507153", "model": "CD4511BE", "matched_by": "code",
                  "package": "DIP-16", "attrs": {"耗散功率(Pd)": "700mW"},
                  "params": {"power_w": 0.7}},
}
l4 = analyze_one(r4)
check("摘要行仍是型号", l4["params_text"] == "CD4511BE", l4["params_text"])
check("功耗不进 params", "power_w" not in l4["params"], l4["params"])

# 7.5 电容耐压进搜索词
r5 = {
    "line_no": 10, "mpn": "ERG50V22M5X11", "desc": "22uF",
    "package": "CAP-TH_BD5.0-P2.00-D0.5", "qty": 2, "lcsc": "C47344185",
    "lcsc_info": {"code": "C47344185", "model": "ERG50V22M5X11", "matched_by": "code",
                  "package": "Through Hole,D5xL11mm",
                  "attrs": {"容值": "22uF", "额定电压": "50V"},
                  "params": {"capacitance_f": 22e-6, "voltage_v": 50.0}},
}
l5 = analyze_one(r5)
check("耐压 50V 进闸门", any("50V" in g for g in l5["hard_gates"]), l5["hard_gates"])
check("搜索词带 50V",
      any("50V" in q for q in l5["query_plan"]), l5["query_plan"])

# 7.6 没有立创数据时行为不变
r6 = {"line_no": 2, "mpn": "MF1/4W-1KΩ±1%T52", "desc": "5kΩ",
      "package": RES_PKG, "qty": 3, "refs": "R1"}
l6 = analyze_one(r6)
check("无立创数据照旧出 5kΩ", abs(l6["params"].get("resistance_ohm", 0) - 5000) < 1,
      l6["params"].get("resistance_ohm"))
check("无立创数据没有 lcsc 徽标", l6.get("lcsc") is None)


print("== 8. 表头识别 ==")
raw = (
    "ID,Name,Designator,Footprint,Quantity,Manufacturer Part,Manufacturer,Supplier,Supplier Part\n"
    "1,NE555P,U4,DIP-8_L9.8,1,NE555P,TI(德州仪器),LCSC,C46749\n"
    "2,470,R1,RES-TH,42,MF1/4W-1KΩ±1%T52,华星,LCSC,\n"
).encode("utf-8")
book = engine.parse_workbook("bom.csv", raw)
rows = book["rows"]
check("Supplier Part → lcsc", rows[0].get("lcsc") == "C46749", rows[0].get("lcsc"))
check("空格不变成编号", rows[1].get("lcsc") == "", rows[1].get("lcsc"))
check("ID 当行号", rows[1].get("line_no") == 2, rows[1].get("line_no"))
check("Supplier 列认出来", book["columns"].get("supplier") == "Supplier", book["columns"])
check("Manufacturer → brand", rows[0].get("brand") == "TI(德州仪器)")

raw2 = (
    "序号,数量,器件名称,器件标号,器件参数,供应商编号,供应商,封装,备注\n"
    "1,1,贴片电阻,R1,10kΩ, C25804 ,LCSC,0603,\n"
).encode("utf-8")
book2 = engine.parse_workbook("easyeda.csv", raw2)
check("中文表头也认", book2["rows"][0].get("lcsc") == "C25804", book2["rows"][0].get("lcsc"))

print("== 9. 编号取值容错（老数据只有 raw 原始列） ==")
check("lcsc 字段优先", lcsc.code_of_row({"lcsc": "C46749"}) == "C46749")
check("从 raw 的 Supplier Part 取",
      lcsc.code_of_row({"raw": {"Supplier Part": "C46749"}}) == "C46749")
check("从中文列名取",
      lcsc.code_of_row({"raw": {"供应商编号": " C2919656 "}}) == "C2919656")
check("空编号返回空",
      lcsc.code_of_row({"raw": {"Supplier Part": "", "Manufacturer Part": "NE555P"}}) == "")
check("名称里像编号也不算",
      lcsc.code_of_row({"raw": {"Name": "C123"}}) == "", "不该整行乱扫")
check("非 dict 安全", lcsc.code_of_row(None) == "")


print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
