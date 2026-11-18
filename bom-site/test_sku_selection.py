#!/usr/bin/env python3
"""SKU 选择回归：这些用例全部来自 2026-11-06 那轮跑批里**真实买错**的行。

修 F1/F2 之前，9 个用例全部选错（买 6.3V 顶 16V、0.3pF 顶 10µF、
2.2R 顶 0Ω、3*40P 顶 3P……）；现在的口径是：

  硬属性冲突的值直接出局；剩下的同分选项按「电压正好等于要求 → 包装最小」挑，
  一个都不剩就报 unresolved（交给下一家店或人工），**绝不允许静默加购**。
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


def mkrow(**kw):
    row = {"line_no": 1, "mpn": "", "desc": "", "package": "", "params": "", "qty": 1}
    row.update(kw)
    return engine.analyze([row])["lines"][0]


def group(label, values, selected=None):
    return {
        "label": label,
        "values": [
            {"text": v, "disabled": False, "selected": (i == selected if selected is not None else False)}
            for i, v in enumerate(values)
        ],
    }


def pick(line, groups):
    sels, unres = tr.suggest_sku(line, groups)
    return {s["label"]: s["value"] for s in sels}, unres


print("== 阻值写法解析 ==")
check("2.2R = 2.2Ω", tr._sku_resistance("0603 2.2R 0.1% (20个)") == 2.2,
      tr._sku_resistance("0603 2.2R 0.1% (20个)"))
check("0R = 0Ω", tr._sku_resistance("0603 0R 跳线") == 0.0)
check("100R = 100Ω", tr._sku_resistance("100R") == 100.0)
check("1R2 = 1.2Ω", tr._sku_resistance("1R2") == 1.2)
check("R24 = 0.24Ω", tr._sku_resistance("R24") == 0.24)
check("4K7 = 4700Ω", tr._sku_resistance("4K7") == 4700.0)
check("4.7KΩ = 4700Ω", tr._sku_resistance("4.7KΩ") == 4700.0)
check("100NF 不当电阻", tr._sku_resistance("0603 100NF") is None)

print("== 非规格组识别：针数写在汉字前面也要认出来 ==")
_hdr = mkrow(desc="CAN", package="XH2.50mm-4P", params="4P 间距2.5mm", qty=1)
check("2P适用单头渡锡线 算规格组", tr._sku_has_spec(_hdr, "2P适用单头渡锡线") is True)
check("0.2m 不算规格组", tr._sku_has_spec(_hdr, "0.2m") is False)

print("== 电容：耐压不够必须否决，不能靠容值分淹掉 ==")
_c2 = mkrow(desc="100nF", package="0603 (1608)", params="100nF 16V X7R ±10%", qty=38)
_sel, _un = pick(_c2, [group("电压", ["6.3V", "10V", "16V", "25V", "50V"], selected=0)])
check("6.3V 默认值被拒", _sel.get("电压") != "6.3V", _sel)
check("选中 16V（正好等于要求）", _sel.get("电压") == "16V", _sel)

_c4 = mkrow(desc="47uF", package="1210 (3225)", params="47uF 16V X6S ±10%", qty=10)
_sel, _un = pick(_c4, [group("颜色分类", ["1210 47UF 476K 6.3V=10个"], selected=0)])
check("唯一选项但耐压不够 → unresolved", not _sel and bool(_un), (_sel, _un))
check("unresolved 说明是耐压问题", "耐压" in (_un[0]["reason"] if _un else ""), _un)

_c17 = mkrow(desc="10uF", package="0603 (1608)", params="10uF 10V X7R", qty=2)
_sel, _un = pick(_c17, [group("颜色分类",
                             ["10uF_±20%_6.3V_X5R", "10uF_±10%_10V_X5R", "10uF_±20%_25V_X5R"],
                             selected=0)])
check("默认 6.3V 换成 10V", _sel.get("颜色分类") == "10uF_±10%_10V_X5R", _sel)

_c9 = mkrow(desc="10uF", package="0603 (1608)", params="10uF", qty=1)
_sel, _un = pick(_c9, [group("颜色分类",
                             ["0.3pF_±0.1pF_50V_C0G", "1pF_±0.25pF_50V_C0G",
                              "100nF_±10%_50V_X7R", "1uF_±10%_25V_X5R", "10uF_±20%_6.3V_X5R"],
                             selected=0)])
check("0.3pF 被拒", "0.3pF" not in str(_sel.get("颜色分类")), _sel)
check("选中 10uF", _sel.get("颜色分类", "").startswith("10uF"), _sel)

_c14 = mkrow(desc="100nF", package="0805 (2012)", params="100nF 50V X7R", qty=2)
_sel, _un = pick(_c14, [group("颜色分类",
                              ["0805 100UF 107M 20% 4V 1个", "0805 100NF 104K 50V 10% 50个"],
                              selected=0)])
check("100µF 4V 被拒", _sel.get("颜色分类", "").startswith("0805 100NF"), _sel)

_c15 = mkrow(desc="100uF", package="插件", params="100uF 35V ±20%", qty=1)
_sel, _un = pick(_c15, [group("颜色分类",
                              ["固态16V1500UF体积10X17MM（2个）", "直插 100UF 35V 8x12（5个）"],
                              selected=0)])
check("1500µF 被拒", _sel.get("颜色分类", "").startswith("直插 100UF 35V"), _sel)

print("== 电阻：0Ω 不能买成 2.2R ==")
_r0 = mkrow(desc="0R", package="0603 (1608)", params="0Ω", qty=3)
_sel, _un = pick(_r0, [group("颜色分类",
                             ["0603 2.2R 0.1% (20个) 推荐", "0603 0R 跳线 (100个)", "0603 10R 1% (100个)"],
                             selected=0)])
check("选中 0R", "0R" in str(_sel.get("颜色分类")), _sel)

_r100 = mkrow(desc="100Ω", package="0603 (1608)", params="100Ω ±1%", qty=6)
_sel, _un = pick(_r100, [group("颜色分类", ["0603 2.2R 0.1% (20个) 推荐"], selected=0)])
check("只有 2.2R → unresolved", not _sel and bool(_un), (_sel, _un))

print("== 排针：排数/针数不符必须否决 ==")
_h3 = mkrow(desc="VENC SELECT", package="排针 2.54mm 3P 单排", params="3P 间距2.54mm", qty=1)
_sel, _un = pick(_h3, [group("颜色分类",
                             ["直针 3X40P 2.54mm三排 (1个)", "直针 1X40P 2.54mm单排 (1个)",
                              "直针 1X4P 2.54mm单排 (50个)", "直针 1X3P 2.54mm单排 (50个)"],
                             selected=0)])
check("3X40P 被拒", "3X40P" not in str(_sel.get("颜色分类")), _sel)
check("选中 1X3P", "1X3P" in str(_sel.get("颜色分类")), _sel)

_h2 = mkrow(desc="TERM ENABLE", package="排针 2.54mm 2P 单排", params="2P 间距2.54mm", qty=1)
_sel, _un = pick(_h2, [group("颜色分类",
                             ["2.54排针2*2p（50个） 店长主推", "2.54排针1*2p（50个）", "2.54排针1*3p（50个）"],
                             selected=0)])
check("双排 2*2p 被拒", "2*2p" not in str(_sel.get("颜色分类")), _sel)
check("选中 1*2p", "1*2p" in str(_sel.get("颜色分类")), _sel)

print("== 双排排针：40P 的位子不能选 1*40P 单排条（2026-11-17 误加购） ==")
# 真机事故：PZ254-2-20-Z-8.5 是 2×20 双排 40P，链接里同时挂 1*40P / 2*40P，
# 页面默认选中「2.54mm/白色1*40P（10只）」，自动加购照抄默认值 ——
# 40 针没错、间距没错，但一条单排条装不到 2×20 的焊盘上。
_h40 = mkrow(desc="PZ254-2-20-Z-8.5", package="HDR-TH_40P-P2.54-V-M-R2-C20-S2.54",
             params="", qty=2, lcsc="C3012260",
             lcsc_info={"code": "C3012260", "model": "PZ254-2-20-Z-8.5",
                        "catalog": "Headers, Male Pins",
                        "attrs": {"排数": "双排", "间距": "2.54mm", "总PIN数": "40P",
                                  "插针结构": "2x20P"}})
check("行排数读到双排", tr._line_rows(_h40) == 2, tr._line_rows(_h40))
_sel, _un = pick(_h40, [group("颜色分类切换大图模式",
                              ["2.54mm/白色1*40P（10只）", "2.54mm/黑色2*20P（10只）",
                               "2.54mm/白色2*40P（10只）"],
                              selected=0)])
check("单排 1*40P 被拒", "1*40P" not in str(_sel.get("颜色分类切换大图模式")), _sel)
check("选中双排 2*20P", "2*20P" in str(_sel.get("颜色分类切换大图模式")), (_sel, _un))

_sel2, _un2 = pick(_h40, [group("颜色分类", ["2.54mm/白色1*40P（10只）"])])
check("只有单排选项 → unresolved，不自动买", not _sel2 and bool(_un2), (_sel2, _un2))

# 老数据没有 rows 字段（分析落盘前存下的行）：必须靠立创属性兜住
_h40_stale = {k: v for k, v in _h40.items() if k != "params"}
_h40_stale["params"] = {"pins": 40, "pitch_mm": 2.54}
_h40_stale["params_text"] = "40P 间距 2.54mm"
check("旧行没 rows 也能从立创属性读出双排", tr._line_rows(_h40_stale) == 2, tr._line_rows(_h40_stale))
check("旧行也不许选单排条",
      tr._sku_veto(_h40_stale, "颜色分类", "2.54mm/白色1*40P（10只）") is not None)

check("双排 2*40P 整条可剪，放行",
      tr._sku_veto(_h40, "颜色分类", "2.54mm/白色2*40P（10只）") is None)

print("== 连接器：要板端针座，不能买带线母头 ==")
_xh = mkrow(desc="CAN: H,L,GND,NC", package="XH2.50mm-4P", params="4P 间距2.5mm", qty=1)
_sel, _un = pick(_xh, [group("颜色分类", ["2P适用单头渡锡线", "4P单头渡锡线20CM（1条）"], selected=0)])
check("带线商品一律不认", not _sel and bool(_un), (_sel, _un))

check("标题带线 → 候选否决",
      tr._candidate_veto(_xh, "信容芯 插头带线兼容适用于B2B/B3B/B4B/B5B/6B-XH-A母头带线20CM") is not None)
check("直插针座 → 候选可用",
      tr._candidate_veto(_xh, "XH2.54-2P 直插针座 B2B-XH-A (LF)(SN) JST 2.5mm 间距接插件") is None)

print("== 保险丝：不能把电容/连接器当保险丝 ==")
_fuse = mkrow(desc="8A fast", package="SMD 保险丝", mpn="0451008", params="8A", qty=1)
check("FFC 连接器被拒",
      tr._candidate_veto(_fuse, "AFC01-S18FCA-00 FFC/FPC连接器 间距0.5mm 翻盖式 下接 卧贴/18P") is not None)
check("铝电解电容被拒",
      tr._candidate_veto(_fuse, "贴片铝电解电容器220UF 6.3V 10V 16V 25V") is not None)
check("真保险丝通过",
      tr._candidate_veto(_fuse, "美国力特1808贴片陶瓷保险丝管LF T 1A 2A 3A 4A 5A 6A 7A 8A 12A") is None)

print("== 有料号的候选优先打开（不改否决，只改顺序）==")
check("标题含料号算命中", tr._title_has_mpn(_fuse, "原装 0451008 一次性保险丝") is True)
check("标题不含料号不算", tr._title_has_mpn(_fuse, "贴片保险丝 8A") is False)
check("料号太短不当依据", tr._title_has_mpn(mkrow(mpn="D1.5mm"), "随便什么标题") is True)

print("== 容差（I3）：精密档买宽了就是另一个等级的料 ==")
_tight = mkrow(desc="24.9kΩ", package="0603 (1608)", params="24.9kΩ ±0.1% 1/4W", qty=2)
_sel, _un = pick(_tight, [group("精度", ["±1%", "±0.1%"], selected=0)])
check("0.1% 与 1% 同组 → 选 0.1%", _sel.get("精度") == "±0.1%", _sel)
check("0.1% 要求下 ±1% 被否决", tr._sku_veto(_tight, "精度", "±1%") is not None)
check("0.1% 要求下 ±0.5%（5×）被否决", tr._sku_veto(_tight, "精度", "±0.5%") is not None)
check("0.1% 要求下 ±0.1% 放行", tr._sku_veto(_tight, "精度", "±0.1%") is None)
_sel, _un = pick(_tight, [group("精度", ["±1%", "±5%"], selected=0)])
check("只有 1%/5% → 精密档判 unresolved", not _sel and bool(_un), (_sel, _un))

_norm = mkrow(desc="10kΩ", package="0603 (1608)", params="10kΩ ±1%", qty=10)
_sel, _un = pick(_norm, [group("精度", ["±5%", "±1%"], selected=0)])
check("1% 与 5% 同组 → 选 1%", _sel.get("精度") == "±1%", _sel)
# 1% 也是精密档：明确标 ±5% 是 5 倍误差，属于降规格，不能靠「唯一选项」蒙混。
check("1% 要求下 ±5% 被否决（降规格）", tr._sku_veto(_norm, "精度", "±5%") is not None)
_sel, _un = pick(_norm, [group("精度", ["±5%"], selected=0)])
check("只有 ±5% → 不自动买（降规格）", not _sel and bool(_un), (_sel, _un))
check("1% 要求下 ±2% 仍在同档容差内", tr._sku_veto(_norm, "精度", "±2%") is None)
_sel, _un = pick(_norm, [group("精度", ["不标精度", "±1%"])])
check("容差未知的选项排在写清楚的后头", _sel.get("精度") == "±1%", _sel)

print("== P0-3：精密档「容差未标注」不能自动买（#70 的真实错买形态） ==")
_sel, _un = pick(_tight, [group("阻值", ["24.9K"], selected=0)])
check("全组未标容差 → unresolved", not _sel and bool(_un), (_sel, _un))
check("说明写清是容差问题", "容差" in (_un[0]["reason"] if _un else ""), _un)
_sel, _un = pick(_tight, [group("阻值", ["24.9K ±1%", "24.9K"], selected=0)])
check("±1% 被 veto 后，未标注选项也不能顶上 → unresolved", not _sel and bool(_un), (_sel, _un))
_sel, _un = pick(_tight, [group("阻值", ["24.9K"], selected=0), group("精度", ["±1%", "±0.1%"], selected=0)])
check("容值/容差独立组、容差组合规 → 正常选", _sel.get("阻值") == "24.9K" and _sel.get("精度") == "±0.1%", (_sel, _un))
_sel, _un = pick(_norm, [group("阻值", ["10K"], selected=0)])
check("普通档未标容差不受影响", _sel.get("阻值") == "10K", (_sel, _un))

print("== P0-4：介质 C0G/NP0 与 X7R/X5R 不能互顶 ==")
_cog = mkrow(desc="1nF", package="0603 (1608)", params="1nF 50V C0G 5%", qty=2)
check("BOM 介质解析：C0G", _cog["params"].get("dielectric") == "C0G", _cog["params"])
check("NP0 归一成 C0G", engine.parse_dielectric("22pF NP0 50V") == "C0G")
check("COG/NPO 变体也认", engine.parse_dielectric("30pF NPO 5%") == "C0G")
check("X7R 解析", engine.parse_dielectric("100nF X7R 10%") == "X7R")
check("没写介质返回 None", engine.parse_dielectric("100nF 50V ±10%") is None)
check("SKU 明确 X7R → 否决",
      tr._sku_veto(_cog, "容值", "1nF_±10%_50V_X7R") is not None)
check("否决理由写明介质", "介质不符" in (tr._sku_veto(_cog, "容值", "1nF_±10%_50V_X7R") or ""))
check("SKU 明确 C0G → 放行", tr._sku_veto(_cog, "容值", "1nF_±5%_50V_C0G") is None)
check("SKU 没写介质 → 放行（挂备注）", tr._sku_veto(_cog, "容值", "1nF_±5%_50V") is None)
check("MPN 里嵌的 X7R 也认",
      tr._dielectric_of("贴片电容 CC0603KRX7R9BB103 0603 10nF") == "X7R")
check("候选标题 X7R → 否决",
      tr._candidate_veto(_cog, "贴片电容 1nF 50V ±10% X7R 0603") is not None)
check("候选标题 C0G → 放行",
      tr._candidate_veto(_cog, "贴片电容 1nF 50V C0G ±5% 0603") is None)
check("候选标题没写介质 → 放行",
      tr._candidate_veto(_cog, "贴片电容 1nF 50V 0603 (100只)") is None)
_sel, _un = pick(_cog, [group("颜色分类", ["1nF_±10%_50V_X7R", "1nF_±5%_50V_C0G"], selected=0)])
check("自动选型避开 X7R 选 C0G", _sel.get("颜色分类") == "1nF_±5%_50V_C0G", (_sel, _un))
_sels, _un2 = tr.suggest_sku(_cog, [group("颜色分类", ["1nF_±5%_50V"], selected=0)])
check("只有未标介质的选项 → 仍可买但挂备注",
      bool(_sels) and "介质未标注" in (_sels[0].get("note") or ""), _sels)
_x7r = mkrow(desc="100nF", package="0603 (1608)", params="100nF 50V X7R ±10%", qty=38)
check("要 X7R 给 C0G 也拦", tr._sku_veto(_x7r, "容值", "100nF_±10%_50V_C0G") is not None)

# ---- 2026-11-10 真机买错的三个形态：电气参数全对，物理上装不进去 ----
print("== 电解电容体积/耐压（BOM 25V 220µF 6×8mm）==")
_elec = mkrow(mpn="PA25V220M6x8", desc="220uF", package="CAP-TH_BD6.3-P2.50-D0.6-FD-1",
              params="220uF", qty=1)
check("13×20mm 的 80V 电解 → 拦",
      tr._sku_veto(_elec, "颜色分类", "80v220uf 体积：13*20mm（5个）") is not None)
check("6×8mm 的同规格 → 放行",
      tr._sku_veto(_elec, "颜色分类", "25v220uf 体积：6*8mm（5个）") is None)
check("多档电压标题不按第一个数字误杀",
      tr._candidate_veto(_elec, "全新电解电容10/16V/25/35V 220uF") is None)

print("== 连接器系列（BOM XH 4P，KF2510 不算同族）==")
_xh = mkrow(mpn="LAIL-PHC2.54-4P-01-XH-L", desc="LAIL-PHC2.54-4P-01-XH-L",
            package="CONN-TH_LAIL-PHC2.54-4P-01-XH-L", qty=2)
check("KF2510 4P → 拦",
      tr._sku_veto(_xh, "颜色分类", "KF2510 4P胶壳 (20个)") is not None)
check("XH 2.54 4P → 放行",
      tr._sku_veto(_xh, "颜色分类", "XH2.54 4P 直针座") is None)
check("标题层也拦 KF2510",
      tr._candidate_veto(_xh, "2.54mm KF2510直针弯针胶壳插头座4P") is not None)
_kf = mkrow(mpn="KF128-5.08-2P-AA", desc="KF128-5.08-2P-AA",
            package="CONN-TH_P5.08_KF128-5.08-2P", qty=1)
check("KF128 与 2EDG 同族 → 放行",
      tr._sku_veto(_kf, "颜色分类", "仅 直针座 2P (10只) 5.08mm") is None)

print("== 电感尺寸（BOM 7.1×6.6 焊盘 → 13.5×12.6 装不下）==")
_ind = mkrow(desc="15uH", mpn="SLO0630H150MTT",
             package="IND-SMD_L7.1-W6.6_SLO0618H", params="15uH", qty=1)
check("13.5×12.6mm 15A → 拦",
      tr._candidate_veto(_ind, "功率电感 XR1365-150M SMD,13.5x12.6mm 15uH 15A") is not None)
check("6×6mm 15uH → 放行",
      tr._candidate_veto(_ind, "贴片功率电感 6x6mm 15uH 3A") is None)

print("== 系列标题（v0.11.0）：标题列了 8 个档位，选中值里明写 33K ==")
# 真机 #4（200kΩ）那家店：标题「贴片电阻 5% 1% 0402 30K 33K 36K …」，SKU 一档一值。
# 旧实现把标题和选中值拼成一整串再解析，只认第一个数（30K），于是「标题里永远没有 BOM 的值」，
# 页级闸门报「这家没有这一档」—— 店确实没有 200kΩ，但同一套逻辑会把有货的店也判死。
_series = mkrow(desc="33kΩ", package="R0402", params="33kΩ ±1%", qty=2)
_series_title = "贴片电阻 5% 1% 0402 30K 33K 36K 39K 43K 47K 51K 56K 62K J F"
_g = [group("颜色分类", ["0402 （精度1%）33K 10000个", "0402 （精度1%）36K 10000个",
                          "0402 （精度5%）33K 10000个"], selected=0)]
_s, _u = tr.suggest_sku(_series, _g, _series_title)
check("系列标题下选中值仍能核实", not [x for x in _u if x["label"] == "整页"], _u)
check("选中的是 33K 那一档", (_s and _s[0]["value"].endswith("33K 10000个")), _s)

print("== 页面默认值不许降规格（v0.11.0）：要 1%，默认 5%，1% 也在 ==")
# #4 那家店页面默认选中的是「精度5% 33K」。旧实现只要默认值得正分就保留，
# 于是自动买了 ±5%，把 80 分的 ±1% 跳过去 —— 自动降精度。
_g2 = [group("颜色分类", ["0402 （精度5%）33K 10000个", "0402 （精度1%）33K 10000个"], selected=0)]
_s2, _u2 = tr.suggest_sku(_series, _g2, _series_title)
check("默认 5% 不保留，改选 1%", (_s2 and "精度1%" in _s2[0]["value"]), _s2)
check("没被当成「保留页面已选值」", (_s2 and _s2[0].get("note") != "保留页面已选值"), _s2)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
