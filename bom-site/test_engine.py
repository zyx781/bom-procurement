#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则引擎单元测试：python test_engine.py（不依赖 pytest）"""

import pathlib
import sys
import tempfile

import engine

ROOT = pathlib.Path(__file__).parent
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


def eq(name, got, want):
    check(name, got == want, f"got={got!r} want={want!r}")


def one_line(row):
    return engine.analyze([row])["lines"][0]


print("== 封装规范化 ==")
eq("0603 带公制", engine.norm_package("0603"), "0603 (1608)")
eq("1608 反查英制", engine.norm_package("1608"), "0603 (1608)")
eq("TO220 加连字符", engine.norm_package("TO220"), "TO-220")
eq("LQFP48 加连字符", engine.norm_package("LQFP48"), "LQFP-48")
eq("插件", engine.norm_package("插件"), "插件")
eq("无封装", engine.norm_package(""), "")

print("== 参数识别 ==")
check("10kΩ", abs(engine._parse_resistance("10kΩ ±1%") - 10000) < 1e-6)
check("4k7", abs(engine._parse_resistance("4k7 电阻") - 4700) < 1e-6)
check("10K 电阻", abs(engine._parse_resistance("10K 电阻") - 10000) < 1e-6)
check("100nF", abs(engine._parse_capacitance("100nF 50V") - 100e-9) < 1e-15)
check("0.1uF", abs(engine._parse_capacitance("0.1uF") - 100e-9) < 1e-15)
check("1/10W", abs(engine._parse_power("1/10W") - 0.1) < 1e-9)
check("±1%", engine._parse_tolerance("±1%") == 1.0)
check("Rds 44mΩ", engine._parse_rds("Rds(on) 44mΩ") == 44.0)
check("N 沟道", engine._parse_type("N沟道 100V") == "N")
check("2.54mm", engine._parse_pitch("2.54mm 2P") == 2.54)

print("== 品类 ==")
eq("电阻", engine.classify("贴片电阻 10kΩ"), "passive")
eq("MCU", engine.classify("STM32F103C8T6 MCU"), "ic")
eq("MOSFET", engine.classify("IRF540N 功率MOSFET"), "power")
eq("连接器", engine.classify("2.54mm 排针连接器"), "connector")
eq("模组", engine.classify("ESP32 模组"), "module")

print("== BOM 解析 ==")
parsed = engine.parse_workbook("demo_bom.csv", (ROOT / "samples/demo_bom.csv").read_bytes())
eq("示例行数", len(parsed["rows"]), 6)
eq("行号", parsed["rows"][0]["line_no"], 1)
eq("数量", parsed["rows"][0]["qty"], 100)
res = engine.analyze(parsed["rows"])
eq("可执行", res["summary"]["ready"], 5)
eq("待确认", res["summary"]["needs_user"], 1)
eq("关键件", res["summary"]["critical"], 2)
eq("被动件数", res["categories"]["passive"], 2)
eq("封装带公制", res["lines"][0]["package"], "0603 (1608)")
eq("决策标题", res["lines"][0]["decision"]["headline"], "0603 10kΩ ±1% 贴片电阻 ×100")
check("搜索词 3 条", len(res["lines"][0]["decision"]["search_queries"]) == 3)
check("硬门槛含封装", any("封装" in g for g in res["lines"][0]["hard_gates"]))
eq("未知行 needs_user", res["lines"][5]["status"], "needs_user")

print("== 表头容错 / 数量写法 ==")
csv_with_title = "BOM 清单 2026-01\n行号,型号,名称,封装,参数,数量\n1,RC0603FR-0710KL,贴片电阻,0603,10kΩ ±1%,100pcs\n".encode()
p2 = engine.parse_workbook("t.csv", csv_with_title)
eq("跳过标题行", p2["rows"][0]["mpn"], "RC0603FR-0710KL")
eq("100pcs→100", p2["rows"][0]["qty"], 100)

print("== 重复行号 ==")
dup = engine.analyze(
    [
        {"line_no": 1, "mpn": "R1", "desc": "电阻", "package": "0603", "params": "10kΩ", "qty": 1},
        {"line_no": 1, "mpn": "R2", "desc": "电阻", "package": "0603", "params": "1kΩ", "qty": 1},
    ]
)
check("重复行号告警", any("重复" in w for w in dup["warnings"]), dup["warnings"])

print("== XLSX ==")
try:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["行号", "型号", "名称", "封装", "参数", "数量"])
    ws.append([1, "RC0603FR-0710KL", "贴片电阻", "0603", "10kΩ ±1%", 100])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        wb.save(f.name)
        xp = engine.parse_workbook("bom.xlsx", pathlib.Path(f.name).read_bytes())
    eq("xlsx 行数", len(xp["rows"]), 1)
    eq("xlsx 型号", xp["rows"][0]["mpn"], "RC0603FR-0710KL")
except ImportError:
    print("  skip openpyxl 未安装")

print("== 封装规范化：Altium / KiCad 写法 ==")
eq("C0402", engine.norm_package("C0402"), "0402 (1005)")
eq("R0603", engine.norm_package("R0603"), "0603 (1608)")
eq("LED0603", engine.norm_package("LED0603-RD"), "0603 (1608)")
eq("CONN-TH", engine.norm_package("CONN-TH_AXH-6A-11"), "插件")
eq("CAP-TH", engine.norm_package("CAP-TH_BD6.3-P2.50-D0.6-FD-1"), "插件")
eq("VQFN", engine.norm_package("VQFN-24_L4.0-W4.0-P0.50-BL-EP2.5"), "QFN-24")
eq("TDSON", engine.norm_package("TDSON-8_L5.9-W5.2-P1.27-BL"), "TDSON-8")
eq("SOD-123", engine.norm_package("SOD-123_L2.7-W1.6-LS3.7-R-RD"), "SOD-123")
eq("SMA 不被 SMAJ 误判", engine.norm_package("SMA_L4.3-W2.6-LS5.0-FD"), "SMA")
eq("SMD 兜底", engine.norm_package("SW-SMD_L6.0-W3.3-LS8.0_TS-1101-C-W"), "SMD")

print("== Altium 现成导出表 ==")
altium = (
    "No.,Quantity,Comment,Designator,Footprint,Value,Manufacturer Part,Manufacturer\n"
    "1,8,100nF,\"C1, C4\",C0402,100nF,,FOJAN(富捷)\n"
    "2,2,1kΩ,\"R1, R2\",R0603,1kΩ,0603WAF1001T5E,UNI-ROYAL(厚声)\n"
    "3,4,1N5819W,\"D1, D2\",SOD-123_L2.7-W1.6-LS3.7-R-RD,,,JSMSEMI\n"
    "4,4,DRV8701ERGER,U1,VQFN-24_L4.0-W4.0-P0.50-BL-EP2.5,,,TI\n"
    "5,16,TPH1R403NL,U2,TDSON-8_L5.9-W5.2-P1.27-BL,,,TECH PUBLIC\n"
    "6,4,LAIL-PHC2.54-6P-01-XH-L,CN1,CONN-TH_AXH-6A-11,,,LAILAN\n"
    "7,1,40MHz,X4,OSC-SMD_4P-L3.2-W2.5-BL_SIT8008BI,40MHz,,YXC\n"
)
ap = engine.parse_workbook("altium.csv", altium.encode())
eq("Altium 列映射", (ap["columns"].get("mpn"), ap["columns"].get("desc")), ("Manufacturer Part", "Comment"))
eq("Altium 行号列", ap["rows"][0]["line_no"], 1)
al = engine.analyze(ap["rows"])
by_no = {x["line_no"]: x for x in al["lines"]}
eq("100nF→被动件", by_no[1]["category"], "passive")
check("100nF 容值", abs(by_no[1]["params"].get("capacitance_f", 0) - 100e-9) < 1e-18)
eq("C0402 封装", by_no[1]["package"], "0402 (1005)")
eq("100nF 可执行", by_no[1]["status"], "ready")
eq("1kΩ 阻值", by_no[2]["params"].get("resistance_ohm"), 1000.0)
check("R0603 命中 0603 硬门槛", any("0603" in g for g in by_no[2]["hard_gates"]))
eq("二极管按型号定位", by_no[3]["mpn"], "1N5819W")
eq("二极管不被要求阻值", by_no[3]["status"], "ready")
eq("DRV8701 芯片", by_no[4]["category"], "ic")
eq("DRV8701 QFN-24", by_no[4]["package"], "QFN-24")
eq("TPH1R403NL 封装判功率管", by_no[5]["category"], "power")
eq("连接器", by_no[6]["category"], "connector")
eq("连接器针数", by_no[6]["params"].get("pins"), 6)
eq("连接器间距", by_no[6]["params"].get("pitch_mm"), 2.54)
eq("连接器插件", by_no[6]["package"], "插件")
check("晶振从封装抠料号", by_no[7]["mpn"] == "SIT8008BI", by_no[7]["mpn"])
eq("Altium 全行可执行", al["summary"]["needs_user"], 0)

print("== 候选评分：硬门槛 ==")
line = res["lines"][0]
cand = """title,price,sales,rating,shop_type,is_tmall,real_photo
0603 10KΩ ±1% 贴片电阻 100只,3.5,12000,0.98,企业店,false,true
0603 10kΩ 1% 电阻,2.8,3400,0.95,个人C店,false,false
0603 10K 5% 电阻,1.2,50000,0.97,企业店,false,true
0805 10kΩ ±1% 电阻,2.5,5000,0.97,企业店,false,true
0603 10k 1% 电阻 散新,0.9,9000,0.90,个人C店,false,false
0603 10kΩ ±1% 0.1W 50V,3.0,6000,0.96,专营店,false,true
0603 10kΩ ±1% 编带,3.8,1500,0.98,专营店,false,true
0603 10kΩ ±1% 电阻 盒装,3.2,4000,0.97,专营店,false,true
"""
sc = engine.score_candidates(line, cand)
by_title = {c["title"]: c for c in sc["candidates"]}
eq("0805→H01", by_title["0805 10kΩ ±1% 电阻"]["risks"][0]["code"], "H01")
check("5% 容差不淘汰", by_title["0603 10K 5% 电阻"]["score"] > 0, by_title["0603 10K 5% 电阻"])
check("5% 无 H03", not any(r["code"] == "H03" for r in by_title["0603 10K 5% 电阻"]["risks"]))
eq("散新→淘汰", by_title["0603 10k 1% 电阻 散新"]["decision"], "淘汰")
check("主选分数≥75", any(c["decision"] == "主选" for c in sc["candidates"]))
check("价格结论", sc["stats"]["price_conclusion"] is True)
check("中位价合理", 2.0 < sc["stats"]["median"] < 5.0, sc["stats"]["median"])

print("== 容差放宽到 ±20% / 耐压必须够 ==")
vline = engine.analyze(
    [{"line_no": 9, "mpn": "R1", "desc": "贴片电阻", "package": "0603",
      "params": "10kΩ ±1% 0.1W 50V", "qty": 10}]
)["lines"][0]
eq("BOM 记下耐压", vline["params"].get("voltage_v"), 50)
check("需求写放松宽", any("放宽" in g for g in vline["decision"]["buy_spec"]), vline["decision"]["buy_spec"])
check("搜索词不含精度", all("%" not in q for q in vline["decision"]["search_queries"]), vline["decision"]["search_queries"])
vcsv = """title,price,sales,rating,shop_type,is_tmall,real_photo
0603 10kΩ 1% 50V 电阻,3.0,3000,0.97,企业店,false,true
0603 10kΩ 5% 50V 电阻,2.8,2000,0.96,企业店,false,true
0603 10kΩ 10% 50V 电阻,2.6,1800,0.96,企业店,false,true
0603 10kΩ 20% 50V 电阻,2.4,1500,0.96,企业店,false,true
0603 10kΩ 1% 16V 电阻,1.0,1000,0.96,企业店,false,true
0603 10kΩ 1% 25V 电阻,1.2,1200,0.96,企业店,false,true
"""
vs = {c["title"]: c for c in engine.score_candidates(vline, vcsv)["candidates"]}
for t in ("0603 10kΩ 5% 50V 电阻", "0603 10kΩ 10% 50V 电阻", "0603 10kΩ 20% 50V 电阻"):
    check(f"{t} 可用", vs[t]["score"] > 0 and not any(r["code"] == "H03" for r in vs[t]["risks"]), vs[t])
check("±1% 仍是最优", vs["0603 10kΩ 1% 50V 电阻"]["score"] >= vs["0603 10kΩ 5% 50V 电阻"]["score"])
check("16V 耐压不足淘汰", any(r["code"] == "H03" for r in vs["0603 10kΩ 1% 16V 电阻"]["risks"]), vs["0603 10kΩ 1% 16V 电阻"]["risks"])
check("25V 耐压不足淘汰", any(r["code"] == "H03" for r in vs["0603 10kΩ 1% 25V 电阻"]["risks"]), vs["0603 10kΩ 1% 25V 电阻"]["risks"])
vcs = """title,price,sales,rating,shop_type,is_tmall,real_photo
0603 10kΩ 1% 10V 16V 25V 50V 电阻,3.0,3000,0.97,企业店,false,true
0603 10kΩ 1% 10V 电阻,1.0,1000,0.97,企业店,false,true
"""
vs2 = {c["title"]: c for c in engine.score_candidates(vline, vcs)["candidates"]}
check("多电压链接不误杀", vs2["0603 10kΩ 1% 10V 16V 25V 50V 电阻"]["score"] > 0, vs2["0603 10kΩ 1% 10V 16V 25V 50V 电阻"]["reasons"])
check("只有 10V 仍淘汰", any(r["code"] == "H03" for r in vs2["0603 10kΩ 1% 10V 电阻"]["risks"]), vs2["0603 10kΩ 1% 10V 电阻"]["risks"])

print("== 连接器：排针 / XH / 端子台 ==")
eq("XH 系列", engine.connector_series("MainParts:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical"), "XH")
eq("排针系列", engine.connector_series("MainParts:PinHeader_1x02_P2.54mm_Vertical"), "排针")
eq("排母系列", engine.connector_series("MainParts:PinSocket_1x08_P2.54mm"), "排母")
eq("端子台系列", engine.connector_series("MainParts:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02"), "端子台")
eq("XH 料号 B2B-XH-A", engine.connector_part("MainParts:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical"), "B2B-XH-A")
eq("XH 料号 B4B-XH-A", engine.connector_part("MainParts:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical"), "B4B-XH-A")
eq("排针无料号", engine.connector_part("MainParts:PinHeader_1x02_P2.54mm_Vertical"), "")
eq("MKDS 料号", engine.connector_part("TerminalBlock_Phoenix_MKDS-1,5-2-5.08"), "MKDS-1,5-2-5.08")
check("Altium 1x02 → 2 针", engine._parse_pins("MainParts:PinHeader_1x02_P2.54mm_Vertical") == 2)
check("Altium 1x04 → 4 针", engine._parse_pins("MainParts:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical") == 4)
check("间距 2.5/2.54 同档", engine.pitch_match(2.5, 2.54) and engine.pitch_match(2.54, 2.50))
check("间距 1.25/1.27 不同档", not engine.pitch_match(1.25, 1.27))
check("排针封装补单排", "单排" in engine._altium_package("PinHeader_1x02_P2.54mm_Vertical"))
check("排针封装补双排", "双排" in engine._altium_package("PinHeader_2x20_P2.54mm_Vertical"))

conn_rows = [
    {"line_no": 1, "mpn": "", "desc": "CAN: H,L,GND,NC", "package": "MainParts:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical", "qty": 1},
    {"line_no": 2, "mpn": "", "desc": "CORE EXT POWER LINK", "package": "MainParts:PinHeader_1x02_P2.54mm_Vertical", "qty": 1},
    {"line_no": 3, "mpn": "", "desc": "BATTERY INPUT 3S", "package": "MainParts:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal", "qty": 1},
]
cl = {x["line_no"]: x for x in engine.analyze(conn_rows)["lines"]}
eq("XH 行料号不被信号名污染", cl[1]["mpn"], "B4B-XH-A")
eq("XH 行针数", cl[1]["params"].get("pins"), 4)
check("XH 搜索词带 XH2.54/针数", any("XH2.54 4P" in x for x in cl[1]["decision"]["search_queries"]), cl[1]["decision"]["search_queries"])
check("排针搜索词带针数", any("排针 2P" in x for x in cl[2]["decision"]["search_queries"]), cl[2]["decision"]["search_queries"])
check("端子台搜索词带间距", any("5.08mm 端子台" in x for x in cl[3]["decision"]["search_queries"]), cl[3]["decision"]["search_queries"])
check("端子台针数", cl[3]["params"].get("pins") == 2, cl[3]["params"])

xcsv = """title,price,sales,rating,shop_type,is_tmall,real_photo
XH2.54-4P 连接器 直针 带扣,1.5,3000,0.97,企业店,false,true
XH2.5-4P 立式插座,1.6,2000,0.96,企业店,false,true
XH2.54-2P 连接器,0.8,5000,0.96,企业店,false,true
2.0mm 4P 连接器,1.2,900,0.96,企业店,false,true
"""
xs = {c["title"]: c for c in engine.score_candidates(cl[1], xcsv)["candidates"]}
check("XH2.54-4P 可用", xs["XH2.54-4P 连接器 直针 带扣"]["score"] > 0, xs["XH2.54-4P 连接器 直针 带扣"])
check("XH2.5-4P 可用（间距同档）", xs["XH2.5-4P 立式插座"]["score"] > 0, xs["XH2.5-4P 立式插座"])
check("XH 错针数淘汰", any(r["code"] == "H01" for r in xs["XH2.54-2P 连接器"]["risks"]), xs["XH2.54-2P 连接器"]["risks"])
check("XH 错间距淘汰", any(r["code"] == "H01" for r in xs["2.0mm 4P 连接器"]["risks"]), xs["2.0mm 4P 连接器"]["risks"])

hcsv = """title,price,sales,rating,shop_type,is_tmall,real_photo
2.54mm 单排排针 40P 可掰断,0.35,8000,0.97,企业店,false,true
2.54mm 单排排针 2P 直针,0.1,3000,0.97,企业店,false,true
2.54mm 双排排针 2x20,0.6,1000,0.96,企业店,false,true
2.54mm 排母 单排 40P,0.4,900,0.96,企业店,false,true
2.0mm 单排排针 40P,0.3,2000,0.96,企业店,false,true
"""
hs = {c["title"]: c for c in engine.score_candidates(cl[2], hcsv)["candidates"]}
check("排针 40P 可掰可用", hs["2.54mm 单排排针 40P 可掰断"]["score"] > 0, hs["2.54mm 单排排针 40P 可掰断"])
check("排针 2P 可用", hs["2.54mm 单排排针 2P 直针"]["score"] > 0)
check("排母淘汰", any(r["code"] == "H01" for r in hs["2.54mm 排母 单排 40P"]["risks"]), hs["2.54mm 排母 单排 40P"]["risks"])
check("双排淘汰", any(r["code"] == "H01" for r in hs["2.54mm 双排排针 2x20"]["risks"]), hs["2.54mm 双排排针 2x20"]["risks"])
check("排针错间距淘汰", any(r["code"] == "H01" for r in hs["2.0mm 单排排针 40P"]["risks"]), hs["2.0mm 单排排针 40P"]["risks"])

print("== 候选评分：样本 <5 不下价格结论 ==")
few = """title,price,sales,rating,shop_type,is_tmall,real_photo
0603 10kΩ ±1% 电阻,3.5,1200,0.98,企业店,false,true
0603 10kΩ ±1% 电阻 编带,3.8,800,0.97,专营店,false,true
"""
sc2 = engine.score_candidates(line, few)
check("样本不足", sc2["stats"]["price_conclusion"] is False)
check("分数仍可给出", any(c["score"] > 0 for c in sc2["candidates"]))

print("== 候选评分：功率器件 H04/H07 ==")
pline = res["lines"][2]
power_cand = """title,price,sales,rating,shop_type,is_tmall,real_photo
IRF540N TO-220 N沟道 100V 33A 原装,4.5,3000,0.98,企业店,false,true
IRF540N TO-220 N沟道 100V 33A 散新,2.0,9000,0.95,个人C店,false,false
IRF540N TO-220 N沟道 100V 33A,3.8,1000,0.97,,false,false
"""
sc3 = engine.score_candidates(pline, power_cand)
codes = {c["title"]: (c["risks"][0]["code"] if c["risks"] else "") for c in sc3["candidates"]}
eq("散新功率管 H04", codes["IRF540N TO-220 N沟道 100V 33A 散新"], "H04")
eq("无店铺无实拍 H07", codes["IRF540N TO-220 N沟道 100V 33A"], "H07")
eq("原装功率管有分", sc3["candidates"][0]["decision"], "主选")

print("== 候选评分：异常低价 H05 ==")
prices = ["1.2", "3.0", "3.1", "3.2", "3.3", "3.4"]
rows = ["title,price,sales,rating,shop_type,is_tmall,real_photo"]
for i, p in enumerate(prices):
    rows.append(f"0603 10kΩ ±1% 电阻 {i},{p},1000,0.97,企业店,false,true")
sc4 = engine.score_candidates(line, "\n".join(rows))
cheap = [c for c in sc4["candidates"] if c["price"] == 1.2][0]
eq("1.2 触发 H05", cheap["risks"][0]["code"], "H05")

print("== 候选评分：中文表头 ==")
cn = """标题,价格,销量,好评率,店铺类型,天猫,实拍
0603 10kΩ ±1% 电阻,3.5,1200,0.98,企业店,否,是
0603 10kΩ ±1% 电阻 编带,3.8,800,0.97,专营店,否,是
"""
sc5 = engine.score_candidates(line, cn)
eq("中文表头行数", len(sc5["candidates"]), 2)
check("中文表头可评分", sc5["candidates"][0]["score"] > 0)

print("== 候选评分：只买 1 个时不该选 100 只/包的链接 ==")
one = engine.analyze([
    {
        "line_no": 1,
        "mpn": "RC0603FR-0710KL",
        "desc": "贴片电阻",
        "package": "0603",
        "params": "10kΩ ±1% 0.1W 50V",
        "qty": 1,
    }
])["lines"][0]
ocsv = """title,price,sales,rating,shop_type,is_tmall,real_photo
0603 10kΩ ±1% 贴片电阻 100只/包,3.0,12000,0.98,企业店,false,true
0603 10kΩ ±1% 贴片电阻 100只/包 编带,3.2,9000,0.98,企业店,false,true
0603 10kΩ ±1% 贴片电阻 100只/包 全新,3.5,7000,0.97,企业店,false,true
0603 10kΩ ±1% 贴片电阻 单只 散卖,0.12,900,0.97,企业店,false,true
0603 10kΩ ±1% 贴片电阻 单只 零售,0.15,800,0.97,企业店,false,true
0603 10kΩ ±1% 贴片电阻 单只 可拆,0.2,700,0.97,企业店,false,true
"""
osc = engine.score_candidates(one, ocsv)
by_title = {c["title"]: c for c in osc["candidates"]}
_top = osc["candidates"][0]
eq("需求 1 只时主选散卖链接", _top["pack_size"] is None and _top["actual_pcs"] == 1, True)
_packs = [c for c in osc["candidates"] if c["pack_size"] == 100]
eq("整包排序被压到散卖之后",
   all(osc["candidates"].index(p) > 2 for p in _packs), True)
pack_c = by_title["0603 10kΩ ±1% 贴片电阻 100只/包 编带"]
eq("整包仍可买（超量只降排序不判淘汰）", pack_c["decision"] != "淘汰", True)
eq("整包 100 只被标超量", any(r["code"] == "OV01" for r in pack_c["risks"]), True)
eq("整包到手 100 只（需求 1）", pack_c["actual_pcs"], 100)
eq("整包采购合计 3.2", pack_c["cost"], 3.2)
loose_c = by_title["0603 10kΩ ±1% 贴片电阻 单只 散卖"]
eq("散卖到手 1 只", loose_c["actual_pcs"], 1)
eq("散卖采购合计 0.12", loose_c["cost"], 0.12)

print("== 候选评分：需求 100 只时整包反而更划算 ==")
hundred = dict(one)
hundred["qty"] = 100
hsc = engine.score_candidates(hundred, ocsv)
eq("需求 100 时主选整包", hsc["candidates"][0]["title"].startswith("0603 10kΩ ±1% 贴片电阻 100只/包"), True)
eq("整包买 1 份", hsc["candidates"][0]["buy_qty"], 1)

print("== 0Ω 跳线电阻：要求值为 0 不能除零 ==")
zero = {
    "line_no": 56,
    "category": "passive",
    "mpn": "",
    "package": "0603 (1608)",
    "params": {"resistance_ohm": 0.0},
    "qty": 3,
}
c0, t0, _ch, _rk = engine._check_title(zero, {"title": "0603 0Ω 跳线电阻 100只/包"}, {})
eq("0Ω 标题不被淘汰", c0, None)
eq("0R 也算 0Ω", engine._check_title(zero, {"title": "0603 0R 贴片电阻 1%"}, {})[0], None)
eq("10kΩ 被 0Ω 行淘汰", engine._check_title(zero, {"title": "0603 10kΩ ±1% 电阻 100只"}, {})[0], "H03")
eq("0Ω 行能出候选", len(engine.score_candidates(zero, "title,price\n0603 0Ω 跳线电阻 100只/包,2.5")["candidates"]), 1)

print("== 盘装/袋装数量（编带整盘最常见的写法）==")
eq("1盘1000", engine.title_pack("国产编带电容 47UF/35V/1210/10% 材质X5R 1盘1000")[0], 1000)
eq("1盘5000", engine.title_pack("0603 10K 电阻 1盘5000")[0], 5000)
eq("整盘=2000", engine.title_pack("整盘=2000 1206 电容")[0], 2000)
eq("1000/盘", engine.title_pack("1000/盘 47UF")[0], 1000)
eq("1袋500", engine.title_pack("1袋500 0603电容")[0], 500)
eq("1盘 47UF 不当数量", engine.title_pack("1盘 47UF 电容")[0], None)
eq("1盘 50V 不当数量", engine.title_pack("1盘 50V 电容")[0], None)
eq("需求10只 / 1盘1000只 → 只买1份", engine.plan_qty(10, 1000), (1, 1000, 1000))
eq("需求10只 / 1盘1000只 → 合计按1份算", engine.plan_qty(10, 1000)[0] * 120, 120)

print("== 保险丝：只写了额定电流，没有容值可搜 ==")
_fuse = engine.analyze_line({"refs": "F1", "desc": "8A / branch fault backup", "package": "SMD"}, 23)
eq("保险丝按电流出词", _fuse["query_plan"][0], "8A 贴片保险丝")
eq("电流带单位不写成 8.0", any("8.0" in q for q in _fuse["query_plan"]), False)
eq("电流进参数池", _fuse["params"].get("current_a"), 8.0)
eq("电流写进规格摘要", "8A" in (_fuse.get("params_text") or ""), True)
eq(
    "斜杠整档标题能展开",
    engine.title_amps("保险丝 1808 6125 1/1.5/2/3/8/25A"),
    [1.0, 1.5, 2.0, 3.0, 8.0, 25.0],
)
eq("整档标题不淘汰", engine._check_title(_fuse, {"title": "保险丝 1808 1/1.5/2/3/8/25A 250V"}, {})[0], None)
eq("单档不符淘汰", engine._check_title(_fuse, {"title": "0.5A 贴片保险丝 快断 5个"}, {})[0], "H03")

print("== 晶振：BOM 常写「8MHz ABM3」，要补通用词 ==")
_xtal = engine.analyze_line({"refs": "Y1", "desc": "8MHz ABM3", "package": "SMD 晶振"}, 30)
eq("晶振搜索词带频率", any("8MHz" in q for q in _xtal["query_plan"]), True)
eq("晶振不再抛 NameError", isinstance(_xtal["query_plan"], list), True)

print("== 芯片/模块：带封装的组合词常搜不到，裸料号先试 ==")
_ic = engine.analyze_line({"refs": "U9", "desc": "TXU0202DCUR", "package": "SSOP-8"}, 95)
eq("芯片首选裸料号", _ic["query_plan"][0], "TXU0202DCUR")
eq("第二条带封装", _ic["query_plan"][1], "TXU0202DCUR SSOP-8")
_mod = engine.analyze_line({"refs": "U9", "desc": "E104-BT40", "package": "模块"}, 107)
eq("模块也走裸料号", _mod["query_plan"][0], "E104-BT40")

print("== 电阻：Ω 搜不到时补「120R」写法 ==")
_r120 = engine.analyze_line({"refs": "R1", "desc": "120Ω", "package": "0603"}, 61)
eq("小阻值补 R 写法", "0603 120R" in _r120["query_plan"], True)
_r47k = engine.analyze_line({"refs": "R2", "desc": "47kΩ", "package": "0603"}, 57)
eq("kΩ 不生成 47kR", any(q.endswith("R") for q in _r47k["query_plan"]), False)

print("== 料号里的数字不该被当成封装 ==")
_sw = engine.analyze_line({"refs": "SW1", "desc": "TS-1088-AR02016", "package": "SMD"}, 81)
eq("AR02016 里的 0201 不算 0201 封装",
   engine._check_title(_sw, {"title": "全新原装 TS-1088-AR02016 4*3*2mm 立贴 轻触开关"}, {})[0], None)
_r0603 = engine.analyze_line({"refs": "R1", "desc": "10kΩ", "package": "0603"}, 1)
eq("真的 0805 标题仍判不符", engine._check_title(_r0603, {"title": "0805 10kΩ 电阻 100只"}, {})[0], "H01")
eq("0603 标题正常通过", engine._check_title(_r0603, {"title": "0603 10kΩ 电阻 100只"}, {})[0], None)

print("== 测试点：只搜「测试点」会召回到双排母/接线端子 ==")
_tp = engine.analyze_line({"refs": "TP1", "desc": "D1.5mm 测试点", "package": ""}, 83)
eq("测试点搜索词带直径", _tp["query_plan"][0], "1.5mm 测试点 焊盘")
eq("测试点搜索词不只有「测试点」", _tp["query_plan"][1], "1.5mm 测试点")
_tp2 = engine.analyze_line({"refs": "TP2", "desc": "MCU STATUS", "package": "测试点 D1.0mm"}, 84)
eq("封装里带测试点也走这条分支", "测试点" in _tp2["query_plan"][0], True)

print("== 电流：料号里的数字不该被当成额定电流 ==")
eq("AO3400A 不算 3400A", engine._parse_current("AO3400A MODE open drain"), None)
eq("8A 正常识别", engine._parse_current("8A / branch fault backup"), 8.0)
eq("500mA 换算成 0.5A", engine._parse_current("LDO 500mA 3.3V"), 0.5)
_q53 = engine.analyze_line({"refs": "Q1", "desc": "AO3400A MODE open drain", "mpn": "AO3400A", "package": "SOT-23"}, 53)
eq("AO3400A 不再带假电流", "id_a" in _q53["params"], False)

print("== 模块类：标题不写型号时按特征匹配 ==")
# 这里原来读 runs/bom-lines-master.json —— 那个文件是真实 BOM，不进仓库，
# 干净克隆会直接 StopIteration。改成把用到的那一行内联，测试自给自足。
_m107 = engine.analyze_line(
    {
        "refs": "U300",
        "desc": "E104-BT40 / CLASSIC SPP",
        "mpn": "",
        "package": "MainParts:E104_BT40_PROVISIONAL",
        "params": "E104-BT40 / CLASSIC SPP",
        "qty": 1,
    },
    107,
)
eq("亿佰特双模蓝牙 SPP 模块通过",
   engine._check_title(_m107, {"title": "亿佰特双模蓝牙BLE5.4透传从机模块低功耗SPP3.0协议AT指令配置"}, {})[0], None)
eq("不相干的无线模块仍拒绝",
   engine._check_title(_m107, {"title": "2.4G无线串口模块包邮开票无线串口收发485模块"}, {})[0], "H02")
eq("非模块行不享受放宽",
   engine._check_title(engine.analyze_line({"refs": "U1", "desc": "SN74LVC2G08DCUR", "package": "SSOP-8"}, 99),
                       {"title": "74LVC2G08 双与门 SOP8 逻辑芯片"}, {})[0], "H02")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
