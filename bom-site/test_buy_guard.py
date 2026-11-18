#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""买错防线：数值对得上，但器件形态/结构不是同一种东西。

2026-11-18 全链路审查发现 12 类「值闸门全放行、装上去却不对」的路径：
贴片当插件、1/8W 顶 1/4W、红色 LED 买成绿色、钽电容买成铝电解、
单绕组电感买成共模、0.3 线顶 0.5 线、无源晶体买成有源晶振、
4P 晶振买成 2P、芯片买成核心板、1% 电阻买成 5%、直针买成弯针、
公头买成母座。这里逐条守住，并给正常候选留反向用例，防止闸门过严。
"""

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
        print(f"  FAIL {name} {extra}")


def mk(no, mpn="", desc="", pkg="", params="", qty=1, refs="R1", lcsc_info=None):
    row = {
        "line_no": no,
        "mpn": mpn,
        "desc": desc,
        "package": pkg,
        "params": params,
        "qty": qty,
        "refs": refs,
    }
    if lcsc_info:
        row["lcsc_info"] = lcsc_info
    return engine.analyze([row])["lines"][0]


print("== 1. 贴片 / 插件：焊盘不通用 ==")
_r0805 = mk(1, desc="10K", pkg="0805 (2012)", params="±1%")
check("0805 行拒绝直插电阻", "封装形态" in tr._material_conflict(_r0805, "10K 1/4W 直插金属膜电阻"))
check("0805 行放过贴片电阻", tr._material_conflict(_r0805, "0805 10K 贴片电阻") == "")
_rth = mk(2, desc="470", pkg="插件", params="470Ω 1/4W", refs="R2")
check("插件行拒绝贴片电阻", "封装形态" in tr._material_conflict(_rth, "0805 470Ω 贴片电阻"))
_capth = mk(3, desc="100uF 25V 电解电容", pkg="CAP-TH_BD8.0-P3.50-D0.6", params="100uF 25V", refs="C1")
check("插件电解拒绝贴片铝电解", "封装形态" in tr._material_conflict(_capth, "贴片铝电解电容 100uF 25V"))
check("插件电解放过直插铝电解", tr._material_conflict(_capth, "100uF 25V 直插铝电解电容") == "")
_diode = mk(4, mpn="1N5819W", desc="1N5819W", pkg="SOD-123", refs="D1")
check("SOD-123 拒绝 DO-41 直插", "封装形态" in tr._material_conflict(_diode, "1N5819 肖特基二极管 DO-41 直插"))
check("SOD-123 放过贴片同封装", tr._material_conflict(_diode, "1N5819W 肖特基 SOD-123 贴片") == "")
_ic = mk(5, mpn="AMC1311BDWVR", desc="AMC1311BDWVR", pkg="SOIC-8", refs="U1")
check("SOIC-8 拒绝 DIP-8", "封装形态" in tr._material_conflict(_ic, "AMC1311BDWVR DIP-8 直插"))
check("SOIC-8 放过 SOP-8", tr._material_conflict(_ic, "AMC1311BDWVR SOP-8 贴片") == "")

print("== 2. 电阻功率：只能放大，不能不足 ==")
_rp = mk(6, desc="10K", pkg="0805 (2012)", params="1/4W ±1%", refs="R3")
check("1/8W 顶 1/4W 被 SKU 否决", "功率不符" in (tr._sku_veto(_rp, "功率", "1/8W") or ""))
check("1/4W 正常放行", tr._sku_veto(_rp, "功率", "1/4W") is None)
check("1/2W 放大放行", tr._sku_veto(_rp, "功率", "1/2W") is None)
check("标题 1/8W 直接 H03", engine._check_title(_rp, {"title": "0805 10K 1/8W 贴片电阻"}, {})[0] == "H03")
check("多档标题 1/8W/1/4W 不误杀",
      engine._check_title(_rp, {"title": "0805 10K 1/8W/1/4W 贴片电阻"}, {})[0] is None)
check("1N5819W 不会被读成 5819W", engine._parse_power("1N5819W") is None)
check("1/4W 仍读 0.25", abs(engine._parse_power("1/4W") - 0.25) < 1e-12)
check("1W/2W 取到两档", max(engine._parse_powers("1W/2W")) == 2.0)

print("== 3. LED 颜色：不同颜色是不同料 ==")
_led = mk(7, mpn="KT-0805R", desc="红色 LED", pkg="0805 (2012)", params="红色", refs="LED1")
check("红色 LED 拒绝绿色", "颜色不符" in (tr._material_conflict(_led, "0805 LED 发光二极管 绿色 高亮") or ""))
check("红色 LED 拒绝 RGB", "颜色不符" in (tr._material_conflict(_led, "0805 RGB 全彩 LED") or ""))
check("白发红仍是红色", tr._material_conflict(_led, "0805 白发红 LED") == "")
check("不写颜色不误杀", tr._material_conflict(_led, "0805 LED 发光二极管") == "")

print("== 4. 钽电容 ≠ 铝电解 ==")
_tan = mk(8, mpn="T491A107K016AT", desc="100uF 16V 钽电容", pkg="1210 (3225)",
          params="100uF 16V", refs="C2")
check("BOM 判成钽", tr._bom_cap_family(_tan) == "钽")
check("钽拒绝铝电解", "钽电容" in (tr._material_conflict(_tan, "100uF 16V 铝电解电容 贴片") or ""))
check("钽放过钽", tr._material_conflict(_tan, "100uF 16V 钽电容 贴片") == "")
_alu = mk(9, desc="1000uF 35V", pkg="CAP-TH_BD10.0-P5.00-D0.6",
          params="1000uF 35V", refs="C3",
          lcsc_info={"attrs": {"极性类型": "有极性"}})
check("铝电解仍判电解", tr._bom_cap_family(_alu) == "电解")
check("铝电解行不拦钽（算升级）", tr._material_conflict(_alu, "1000uF 35V 钽电容") == "")

print("== 5. 电感结构：单绕组 ≠ 共模 ==")
_ind = mk(10, desc="3mH", pkg="IND-TH_L11.0-W4.6-P4.3-D0.5", params="3mH", refs="L1")
check("单绕组拒绝共模", "共模" in (tr._material_conflict(_ind, "共模电感 3mH 滤波电感") or ""))
check("单绕组放过单绕组", tr._material_conflict(_ind, "3MH 0.5线 2A 直插电感") == "")
_cm = mk(11, desc="10mH 共模电感", pkg="IND-TH_L11.0-W4.6", params="10mH", refs="L2")
check("共模行必须写共模", "共模" in (tr._material_conflict(_cm, "10mH 直插电感") or ""))
check("共模行放过共模", tr._material_conflict(_cm, "10mH 共模电感 双绕组") == "")

print("== 6. 电感线径 / 额定电流：不能往下换 ==")
check("0.3 线顶 0.5 线被拒", "线径" in (tr._sku_veto(_ind, "规格", "3MH 0.3线 2A") or ""))
check("0.5 线放行", tr._sku_veto(_ind, "规格", "3MH 0.5线 2A") is None)
check("没写线径不误杀", tr._sku_veto(_ind, "规格", "3MH 2A") is None)
_ind_a = mk(12, desc="3mH 2A 直插电感", pkg="IND-TH_L11.0-W4.6", params="3mH", refs="L3")
check("1A 顶 2A 被拒", "额定电流" in (tr._sku_veto(_ind_a, "规格", "3MH 1A") or ""))
check("3A 放行", tr._sku_veto(_ind_a, "规格", "3MH 3A") is None)

print("== 7. 晶振：有源/无源、4P/2P ==")
_xtal = mk(13, mpn="X32258MSB4SI", desc="8MHz",
           pkg="CRYSTAL-SMD_4P-L3.2-W2.5-BL", params="8MHz", refs="X1")
check("无源晶体拒绝有源晶振", "晶振类型" in (tr._material_conflict(_xtal, "8MHz 有源晶振 振荡器 OSC") or ""))
check("无源 4P 放过无源 4P", tr._material_conflict(_xtal, "8MHz 无源晶振 4P 贴片") == "")
check("4P 拒绝 2P", "脚数" in (tr._material_conflict(_xtal, "8MHz 无源晶振 2P 两脚") or ""))
check("8MHz 与 50MHz 仍是硬冲突",
      "频率" in (tr._sku_veto(_xtal, "规格", "50MHz") or ""))

print("== 8. 芯片 ≠ 核心板 / 转接板 / 烧录座 ==")
_mcu = mk(14, mpn="STM32G474CBT6", desc="STM32G474CBT6", pkg="LQFP-48", refs="U2")
check("STM32 拒绝核心板", "核心板" in (tr._candidate_veto(_mcu, "STM32G474CBT6 核心板 开发板 最小系统") or ""))
check("STM32 放过芯片", tr._candidate_veto(_mcu, "STM32G474CBT6 LQFP-48 贴片") is None)
_mod = mk(15, mpn="B0505LS-1W", desc="B0505LS-1W", pkg="插件", refs="U3")
check("电源模块不被当开发板误杀", tr._candidate_veto(_mod, "B0505LS-1W 电源模块 隔离 DC-DC") is None)

print("== 9. 连接器：朝向 / 公母 ==")
_xh = mk(16, mpn="B4B-XH-A", desc="XH2.54 4P 直针", pkg="XH2.54mm-4P", refs="J1")
check("直针拒绝弯针", "朝向" in (tr._material_conflict(_xh, "XH2.54 4P 弯针 插座") or ""))
check("直针放过直针", tr._material_conflict(_xh, "XH2.54 4P 直针 插座") == "")
_xt = mk(17, mpn="XT30UPB-M", desc="XT30UPB-M", pkg="CONN-TH_XT30UPB-M", refs="J2")
check("XT30-M 拒绝 XT30-F", "公母" in (tr._material_conflict(_xt, "XT30UPB-F 母头 连接器") or ""))
check("XT30-M 放过公头", tr._material_conflict(_xt, "XT30UPB-M 公头 直插") == "")

print("== 10. 容差：1% 也是精密档 ==")
_tol = mk(18, desc="10K", pkg="0603 (1608)", params="10kΩ ±1%", refs="R4")
check("1% 拒绝 5%", "容差" in (tr._sku_veto(_tol, "精度", "±5%") or ""))
check("1% 放过 2%", tr._sku_veto(_tol, "精度", "±2%") is None)
check("1% 放过 1%", tr._sku_veto(_tol, "精度", "±1%") is None)
_tight = mk(19, desc="24.9kΩ", pkg="0603 (1608)", params="24.9kΩ ±0.1%", refs="R5")
check("0.1% 仍拒绝 1%", tr._sku_veto(_tight, "精度", "±1%") is not None)

print("== 11. 同系列不同号：SS26 顶 SS34 / 1N5817 顶 1N5819W ==")
_ss = mk(20, mpn="SS34", desc="SS34", pkg="SMA", refs="D2")
check("标题只有 SS26 被拒", "SS26" in (tr._candidate_veto(_ss, "SS26 肖特基二极管 SMA") or ""))
check("标题列了 SS34 不误杀", tr._candidate_veto(_ss, "SS34/SS54 肖特基二极管 SMA") is None)
_n1 = mk(21, mpn="1N5819W", desc="1N5819W", pkg="SOD-123", refs="D3")
check("标题只有 1N5817 被拒", "1N5817" in (tr._candidate_veto(_n1, "1N5817 肖特基二极管 SOD-123") or ""))
check("1N5819 是 1N5819W 的前缀，不误杀", tr._candidate_veto(_n1, "1N5819 肖特基 SOD-123") is None)

print("== 12. 真实 BOM 的正常候选不被误杀 ==")
_bom = mk(22, desc="3mH", pkg="IND-TH_L11.0-W4.6-P4.3-D0.5", params="3mH", refs="L1")
check("3mH 0.5 线 2A 直插电感放行",
      tr._candidate_veto(_bom, "3MH 0.5线 2A 直插电感") is None)
_c24 = mk(23, mpn="C3225X7T0J107M250AB", desc="100uF", pkg="C1210", params="100uF 6.3V", refs="C4",
          lcsc_info={"attrs": {"温度系数": "X7T", "额定电压": "6.3V"}})
check("1210 100uF 6.3V 瓷片放行",
      tr._candidate_veto(_c24, "1210 100uF 6.3V 贴片陶瓷电容 X7T") is None)
_xtal2 = mk(24, mpn="X32258MSB4SI", desc="8MHz", pkg="CRYSTAL-SMD_4P", params="8MHz", refs="X2")
check("8MHz 无源 4P 贴片晶振放行",
      tr._candidate_veto(_xtal2, "8MHz 无源晶振 4P 贴片 3225") is None)
_c33 = mk(25, desc="1000uF", pkg="CAP-TH_BD10.0-P5.00-D0.6", params="1000uF 35V", refs="C5",
          lcsc_info={"attrs": {"极性类型": "有极性", "脚间距": "5mm"}})
check("1000uF 35V 直插铝电解放行",
      tr._candidate_veto(_c33, "1000uF 35V 直插铝电解电容 10x17") is None)
_j26 = mk(26, mpn="XT30UPB-M", desc="XT30UPB-M", pkg="CONN-TH_XT30UPB-M", refs="J3")
check("XT30UPB-M 公头直插放行",
      tr._candidate_veto(_j26, "XT30UPB-M 公头 直插 连接器") is None)
_j52 = mk(27, mpn="ZX-SH1.0-8PWT", desc="ZX-SH1.0-8PWT", pkg="CONN-SMD_8P-P1.00", refs="J4",
          lcsc_info={"attrs": {"连接器类型": "卧贴", "排数": "1", "插针结构": "1x8P"}})
check("SH1.0 8P 卧贴放行",
      tr._candidate_veto(_j52, "ZX-SH1.0-8PWT 卧贴 8P 连接器") is None)

print("\n== 13. 品类混装：电容行不能买成排针 ==")
_c24b = mk(30, mpn="C3225X7T0J107M250AB", desc="100uF", pkg="C1210", params="100uF 6.3V", refs="C4",
           lcsc_info={"attrs": {"温度系数": "X7T", "额定电压": "6.3V"}})
check("100uF 电容行拒绝排针",
      "品类不符" in (tr._material_conflict(_c24b, "PZ254V-11-03P 2.54mm 排针") or ""))
check("100uF 电容行拒绝端子线",
      "品类不符" in (tr._material_conflict(_c24b, "XH2.54 端子线 4P 连接线") or ""))
check("100uF 电容行放过贴片电容",
      tr._material_conflict(_c24b, "1210 100uF 6.3V 贴片陶瓷电容 X7T") == "")
check("电容标题里带「排针」也算被动件（送排针字样不误杀）",
      tr._material_conflict(_c24b, "1210 100uF 贴片电容 送排针") == "")
_j40 = mk(31, mpn="PZ254V-12-40P", desc="PZ254V-12-40P", pkg="HDR-TH_40P-P2.54-V-M-2", refs="J5")
check("排针行拒绝贴片电阻",
      "品类不符" in (tr._material_conflict(_j40, "0805 10K 贴片电阻 100只") or ""))
check("排针行放过双排针", tr._material_conflict(_j40, "2.54mm 双排针 2*20P 直针") == "")

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
