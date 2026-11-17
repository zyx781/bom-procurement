#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行层测试：用 fake_session 跑通 search → open → select-sku → cart-add → cart-view。"""

import json
import os
import pathlib
import shutil
import sys
import time

import engine
import taobao_runner as tr

BASE = pathlib.Path(__file__).parent
# 验证探针测试用：这个文件存在 = 页面上还挂着滑块
VERIFY_FLAG = BASE / "tests" / ".fake_verify_flag"
VERIFY_FLAG.unlink(missing_ok=True)
os.environ["FAKE_VERIFY_FLAG"] = str(VERIFY_FLAG)
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


runner = tr.SessionRunner(
    command=[sys.executable, str(BASE / "tests" / "fake_session.py")],
    repo=BASE,
    artifacts=BASE / "tests" / "artifacts",
)

line = engine.analyze(
    [
        {
            "line_no": 1,
            "mpn": "RC0603FR-0710KL",
            "desc": "贴片电阻",
            "package": "0603",
            "params": "10kΩ ±1% 0.1W 50V",
            "qty": 100,
        }
    ]
)["lines"][0]

print("== 启动 ==")
st = runner.start()
for _ in range(60):
    st = runner.status()
    if st["ready"]:
        break
    time.sleep(0.1)
check("进程运行", st["running"], st)
check("就绪", st["ready"], st)
check("有 task id", bool(st["task_id"]), st)

print("== search ==")
res = runner.search(line)
check("搜索成功", res["ok"], res)
check("候选非空", len(res["candidates"]) > 0, res)
check("命中封装", any("封装 0603" in (c.get("hits") or []) for c in res["candidates"]))
check("0805 被 H01 淘汰", any(c["title"].startswith("0805") and c["decision"] == "淘汰" for c in res["candidates"]))
check("5% 容差不再淘汰", any("5%" in c["title"] and c["score"] > 0 for c in res["candidates"]))
check("有主选", any(c["decision"] == "主选" for c in res["candidates"]))
check("候选带 index", all(c.get("index") is not None for c in res["candidates"]))

print("== open + SKU 匹配 ==")
op = runner.open_item(line, 0)
check("打开成功", op["ok"], op)
check("有 SKU 组", len(op["sku_groups"]) == 4, op["sku_groups"])
sug = {s["label"]: s["value"] for s in op["suggestions"]}
check("阻值选 10KΩ", sug.get("阻值") == "10KΩ", sug)
check("精度选 ±1%", sug.get("精度") == "±1%", sug)
check("封装选 0603", sug.get("封装") == "0603", sug)
check("无未匹配组", not op["unresolved"], op["unresolved"])

print("== add ==")
# 这组用例故意用「100 份」验证金额乘法（¥350），会撞上新加的单行金额上限。
# 只在这一段把上限调高，金额闸门本身的用例在文件末尾单独测（用真实配置）。
_orig_load_settings = tr.load_settings
tr.load_settings = lambda: {**_orig_load_settings(), "max_line_cost": {"default": 100000}}
selections = [{"label": s["label"], "value": s["value"]} for s in op["suggestions"]]
add = runner.add_to_cart(line, 0, selections, 100)
check("加购成功", add["ok"], add)
check("数量确认", add.get("qty_confirmed") is True, add)
check("SKU 确认", add.get("sku_verified") is True, add)
check("数量 100", add.get("qty") == 100, add)
_saved_add = add  # 留给后面的金额断言用

print("== 金额闸门：单行太贵就不点加购 ==")
tr.load_settings = _orig_load_settings  # 用真实配置（passive 上限 200）
runner.open_item(line, 0)
_cap = tr._line_cost_cap("passive")
check("passive 单行上限 200", _cap == 200.0, _cap)
_expensive = runner.add_to_cart(line, 0, selections, 100)  # 100 份 × ¥3.5 = ¥350
check("超上限拒绝加购", _expensive.get("code") == "COST_TOO_HIGH", _expensive)
check("给出预估金额", _expensive.get("est_cost") == 350.0, _expensive.get("est_cost"))
check("没有点加入购物车", _expensive.get("ok") is False, _expensive)
check("芯片类不拦（上限 5000）", tr._line_cost_cap("ic") == 5000.0, tr._line_cost_cap("ic"))

print("== 超量 50 倍以上且不便宜：不自动买整盘 ==")
_line_over = dict(line)
_line_over["qty"] = 10
_grp = []
for _g in op["sku_groups"]:
    if _g["label"] == "数量":
        continue  # 数量组单独换成「1000只」，模拟编带整盘
    _vals = []
    for _i, _v in enumerate(_g["values"]):
        _vv = dict(_v)
        _vv["selected"] = _i == 0
        _vals.append(_vv)
    _grp.append({"label": _g["label"], "values": _vals})
_grp.append({"label": "数量", "values": [{"text": "1000只", "disabled": False, "selected": True}]})
_orig_send = runner.send
_calls = {"add": 0}


def _fake_send(req):
    cmd = req.get("cmd")
    if cmd == "select-sku":
        return {"status": "success", "data": {"final_price": 120.0, "sku_groups": _grp}}
    if cmd == "cart-add":
        _calls["add"] += 1
        return {"status": "success", "data": {"cart_added": True, "confirmed": True}}
    return _orig_send(req)


runner.send = _fake_send
try:
    _over = runner.add_to_cart(_line_over, 0, selections, 1, auto_qty=True, pack_hint=1000)
finally:
    runner.send = _orig_send
check("超量 100 倍被拦", _over.get("code") == "OVERBUY_TOO_MUCH", _over)
check("没有真的点加购", _calls["add"] == 0, _calls)
check("算出超量倍数", _over.get("over_ratio") == 100.0, _over.get("over_ratio"))

print("== 标题价是引流价时记下背离倍数 ==")
_shift = runner.add_to_cart(line, 0, selections, 1, auto_qty=False, price_hint=0.5)
check("背离倍数 7 被记录", _shift.get("price_shift") == 7.0, _shift)
_flat = runner.add_to_cart(line, 0, selections, 1, auto_qty=False, price_hint=3.5)
check("价差不大不标记", _flat.get("price_shift") is None, _flat)

print("== SKU：只在卖数量的组不该把整行卡死 ==")
_l50 = {
    "params": {"voltage_v": 50.0},
    "mpn": "",
    "package": "0603",
    "desc": "贴片电容",
    "params_text": "0603 30pF ±5% 50V",
    "row": {"params": ""},
}
_groups = [
    {
        "label": "容值",
        "values": [
            {"text": "30PF", "selected": False},
            {"text": "22PF", "selected": False},
        ],
    },
    {
        "label": "数量",
        "values": [
            {"text": "50个", "selected": True},
            {"text": "100个", "selected": False},
        ],
    },
]
_sug, _unres = tr.suggest_sku(_l50, _groups)
check("不会因为「50个」判电压不符", not _unres, _unres)
check("数量组被识别为非规格组", any(s["label"] == "数量" for s in _sug), _sug)
check("容值仍按参数选 30PF", {s["label"]: s["value"] for s in _sug}.get("容值") == "30PF", _sug)

print("== 测试点：SKU 只有包装数量差异，不该把整行卡死 ==")
_tp = {
    "params": {},
    "mpn": "",
    "package": "1.5mm 测试点",
    "desc": "D1.5mm 测试点",
    "params_text": "1.5mm 测试点",
    "row": {"params": ""},
}
_tp_groups = [
    {
        "label": "颜色分类",
        "values": [
            {"text": "P100-A2（10个） 杯形凹头1.5mm", "selected": True},
            {"text": "P100-A2（100个） 杯形凹头1.5mm", "selected": False},
        ],
    }
]
_tp_sug, _tp_unres = tr.suggest_sku(_tp, _tp_groups)
check("测试点不因「认不出规格」失败", not _tp_unres, _tp_unres)
check("测试点保留页面默认", _tp_sug and _tp_sug[0]["auto"], _tp_sug)

print("== 保险丝：页面默认 0.25A，BOM 要 8A ==")
_fuse_line = engine.analyze_line({"refs": "F1", "desc": "8A / branch fault backup", "package": "SMD"}, 23)
_fuse_groups = [
    {
        "label": "颜色分类",
        "values": [
            {"text": "0.25A 125V/250V（5个）", "selected": True},
            {"text": "8A 125V/250V（5个）", "selected": False},
            {"text": "25A 125V/250V（5个）", "selected": False},
        ],
    }
]
_f_sug, _f_unres = tr.suggest_sku(_fuse_line, _fuse_groups)
check("按电流选 8A 而不是默认 0.25A", {s["label"]: s["value"] for s in _f_sug}.get("颜色分类") == "8A 125V/250V（5个）", _f_sug)
check("没有未解决项", not _f_unres, _f_unres)

print("== 搜索：候选全被门槛拒掉时换下一条搜索词 ==")
_searched = []


def _fake_search_send(req):
    if req.get("cmd") == "search":
        kw = req.get("keyword")
        _searched.append(kw)
        if len(_searched) == 1:
            return {
                "status": "success",
                "data": {"items": [{"title": "同系列替代型号 无完整料号", "price_value": 2.0, "index": 0}]},
            }
        return {
            "status": "success",
            "data": {"items": [{"title": "DMP4015SK3-13 场效应管 DPAK 原装", "price_value": 3.5, "index": 0}]},
        }
    return _orig_send(req)


_dmp = engine.analyze_line({"refs": "Q5", "desc": "DMP4015SK3-13", "package": "DPAK"}, 54)
runner.send = _fake_search_send
try:
    _res = runner.search(_dmp, require_scored=True)
finally:
    runner.send = _orig_send
check("第一条词不匹配会继续试", len(_searched) >= 2, _searched)
check("最终命中的是完整料号那条词", _res.get("query") == _dmp["query_plan"][1], (_res.get("query"), _dmp["query_plan"]))

print("== 批量行落盘：重启后能读回来接着跑 ==")
_saved = tr.load_batch_lines()
_bak_lines = (_saved, tr.BATCH_LINES_FILE)
try:
    tr.BATCH_LINES_FILE = BASE / "runs" / "_test-batch-lines.json"
    tr.save_batch_lines([{"line_no": 1, "qty": 2, "query_plan": ["a"]}])
    _back = tr.load_batch_lines()
    check("能读回落盘的行", _back == [{"line_no": 1, "qty": 2, "query_plan": ["a"]}], _back)
    tr.BATCH_LINES_FILE.unlink(missing_ok=True)
    check("文件缺失时返回空列表", tr.load_batch_lines() == [], tr.load_batch_lines())
finally:
    tr.BATCH_LINES_FILE = _bak_lines[1]

# 后面的用例继续用高上限，避免被金额闸门误伤
tr.load_settings = lambda: {**_orig_load_settings(), "max_line_cost": {"default": 100000}}

print("== 未选全时拒绝加购 ==")
partial = [{"label": "阻值", "value": "10KΩ"}]
runner.open_item(line, 0)
add2 = runner.add_to_cart(line, 0, partial, 100)
check("SKU_INCOMPLETE", add2.get("code") == "SKU_INCOMPLETE", add2)

print("== 容差放宽：SKU 里的 ±5% 不拦 ==")
_l = {"params": {"tolerance_pct": 1.0}, "mpn": "", "package": "0603", "desc": "贴片电阻",
      "params_text": "10kΩ ±1%", "row": {"params": ""}}
_s1 = tr._score_sku_value(_l, "精度", "±1%")
_s5 = tr._score_sku_value(_l, "精度", "±5%")
_s20 = tr._score_sku_value(_l, "精度", "±20%")
_s30 = tr._score_sku_value(_l, "精度", "±30%")
check("±1% 满分", _s1 > _s5, (_s1, _s5))
# I3 新契约：宽容差按倍数降权，普通档不否决、精密档才否决。
# 1% 要求下 ±5% 是 5× → 降权但仍可买（> -50）；±20% 是 20× → 重罚排最后。
check("±5% 降权但仍可买（> -50，不否决）", -50 < _s5 < _s1, _s5)
check("±20% 重罚（≤ -50，排到最后）", _s20 <= -50, _s20)
check("±30% 不优于 ±20%", _s30 <= _s20, (_s30, _s20))

print("== 批量续跑：进度落盘与跳过规则 ==")
_orig_progress = tr.PROGRESS_FILE
tr.PROGRESS_FILE = tr.RUNS / "_test_progress.json"
tr.save_batch_progress({
    "updated_at": "2026-01-01T00:00:00",
    "total": 3,
    "lines": {"1": {"status": "ok"}, "2": {"status": "failed"}, "3": {"status": "ok"}},
})
_sm = tr.batch_progress_summary()
check("进度可读", _sm["has_progress"] and _sm["ok_count"] == 2, _sm)
check("只把 ok 行当已完成", _sm["ok_line_nos"] == [1, 3], _sm)
check("failed 计入待处理", _sm["problem_count"] == 1, _sm)
tr.save_batch_progress({"total": 1, "lines": {"7": {"status": "ok"}}})
check("重写覆盖旧进度", tr.batch_progress_summary()["ok_line_nos"] == [7], tr.batch_progress_summary())
_dm = tr.done_map_from_progress(
    {"lines": {"1": {"status": "ok"}, "2": {"status": "failed"}, "3": {"status": "uncertain"}}}
)
check("续跑跳过 ok 与 uncertain", sorted(_dm) == ["1", "3"], sorted(_dm))
check("failed 行会重试", "2" not in _dm)
tr.clear_batch_progress()
check("清空进度后为空", tr.batch_progress_summary()["has_progress"] is False)
tr.PROGRESS_FILE = _orig_progress
# clear_batch_progress 只清主文件，"-prev" 副档是测试自己起的名，留在真实 runs/
# 就是垃圾（实测 2026-11-17 runs/ 里躺了个 _test_progress-prev.json）
(tr.RUNS / "_test_progress.json").unlink(missing_ok=True)
(tr.RUNS / "_test_progress-prev.json").unlink(missing_ok=True)

print("== 连接器 SKU：XH 系列 / 排针整条 ==")
_xh = {"category": "connector", "params": {"pins": 4, "pitch_mm": 2.5}, "mpn": "B4B-XH-A",
       "package": "XH2.50mm-4P", "desc": "ENCODER LEFT", "params_text": "", "row": {"params": ""}}
check("XH2.54-4PWT 打高分", tr._score_sku_value(_xh, "颜色分类", "XH2.54-4PWT") >= 30,
      tr._score_sku_value(_xh, "颜色分类", "XH2.54-4PWT"))
check("XH2.5-4P 同档可用", tr._score_sku_value(_xh, "颜色分类", "XH2.5-4P 直针") >= 30,
      tr._score_sku_value(_xh, "颜色分类", "XH2.5-4P 直针"))
check("XH2.54-4AW 弯针可用", tr._score_sku_value(_xh, "颜色分类", "XH2.54-4AW") >= 30,
      tr._score_sku_value(_xh, "颜色分类", "XH2.54-4AW"))
check("XH2.54-2PWT 扣分", tr._score_sku_value(_xh, "颜色分类", "XH2.54-2PWT") < 0,
      tr._score_sku_value(_xh, "颜色分类", "XH2.54-2PWT"))
check("PH2.0-4AW 间距不符扣分", tr._score_sku_value(_xh, "颜色分类", "PH2.0-4AW") < 0,
      tr._score_sku_value(_xh, "颜色分类", "PH2.0-4AW"))

_hd = {"category": "connector", "params": {"pins": 2, "pitch_mm": 2.54}, "mpn": "",
       "package": "排针 2.54mm 2P 单排", "desc": "CORE EXT POWER LINK", "params_text": "", "row": {"params": ""}}
check("排针整条 40P 可用", tr._score_sku_value(_hd, "颜色分类", "2.54mm单排排针 40P") >= 20,
      tr._score_sku_value(_hd, "颜色分类", "2.54mm单排排针 40P"))
check("排针 2P 更优", tr._score_sku_value(_hd, "颜色分类", "单排排针2.54 2P")
      > tr._score_sku_value(_hd, "颜色分类", "2.54mm单排排针 40P"))
check("2.0mm 排针扣分", tr._score_sku_value(_hd, "颜色分类", "2.0mm单排排针 40P") < 0,
      tr._score_sku_value(_hd, "颜色分类", "2.0mm单排排针 40P"))

print("== 整包报价：1 份 = N 只 ==")
check("100只/包 识别", engine.title_pack("0603贴片电阻 10K 100只/包")[0] == 100)
check("1包50个 识别", engine.title_pack("贴片电容 0805 10UF 1包50个")[0] == 50)
check("拍1发50 识别", engine.title_pack("拍1发50 0603 10K电阻")[0] == 50)
check("规格词不误判", engine.title_pack("STM32F103 核心板 3个串口")[0] is None)
check("单只报价不误判", engine.title_pack("0603 10K 1% 电阻 单只价")[0] is None)
check("折算 250 只 / 100 只每份 = 3 份", engine.plan_qty(250, 100) == (3, 100, 300))
check("无包装时按 BOM 用量", engine.plan_qty(37, None) == (37, 1, 37))

op_pack = runner.open_item(line, 0)
check("打开时识别包装", op_pack.get("pack_size") == 100, op_pack.get("pack_size"))
check("BOM 100 / 每份 100 → 1 份", op_pack.get("suggested_qty") == 1, op_pack.get("suggested_qty"))
check("到手 100 只", op_pack.get("actual_pcs") == 100, op_pack.get("actual_pcs"))
check("单只价 0.035", op_pack.get("unit_price") == 0.035, op_pack.get("unit_price"))

line250 = dict(line)
line250["qty"] = 250
op250 = runner.open_item(line250, 0)
sels250 = [{"label": s2["label"], "value": s2["value"]} for s2 in op250["suggestions"]]
add_pack = runner.add_to_cart(line250, 0, sels250, op250["suggested_qty"], auto_qty=True,
                              pack_hint=op250.get("pack_size"))
check("整包加购成功", add_pack["ok"], add_pack)
check("250 只折算成 3 份", add_pack.get("qty") == 3, add_pack.get("qty"))
check("回读到手 300 只", add_pack.get("actual_pcs") == 300, add_pack.get("actual_pcs"))
check("带回单只价", add_pack.get("unit_price") == 0.035, add_pack.get("unit_price"))

print("== cart-view ==")
cv = runner.cart_view()
check("购物车数量", cv.get("cart_item_count") == 3, cv)

print("== 验证码阻断 ==")
try:
    bad_line = dict(line)
    bad_line["query_plan"] = ["验证"]
    runner.search(bad_line)
    check("验证码错误被捕获", False)
except tr.SessionError as exc:
    check("抛出 VERIFICATION_REQUIRED", exc.code == "VERIFICATION_REQUIRED", exc.code)

print("== 验证卡死能自己解开（不用重启会话）==")
VERIFY_FLAG.write_text("1", encoding="utf-8")  # 页面上还挂着滑块
check("验证中 status 仍标记 verification", runner.status()["verification"] is True)
check("验证中 recheck 返回 False", runner.recheck_verification() is False)
VERIFY_FLAG.unlink(missing_ok=True)  # 人工过完滑块
check("验证解除后 recheck 返回 True", runner.recheck_verification() is True)
check("验证解除后 status 复位（按钮能点）", runner.status()["verification"] is False)

print("== 整包超量：后面没有更小包装就别换店（省 4 次开页）==")
_one = dict(line)
_one["qty"] = 1  # 只要 1 个，但候选的 SKU 是 100 只/份
_one["line_no"] = 900
_br = runner._add_line_to_cart(_one, prefer_cheap=True, max_candidates=3)
check("同包装不换店", len(_br.get("attempts") or []) == 1, len(_br.get("attempts") or []))
check("直接收第一家", _br.get("status") == "ok", _br.get("status"))

print("== 整包超量 + 后面有更小包装：换店 ==")
_bak_search_open = (runner.search, runner.open_item)
try:
    runner.search = lambda ln, query=None, require_scored=False: {
        "ok": True,
        "query": "0603 10kΩ 电阻",
        "items_count": 2,
        "candidates": [
            {"index": 0, "title": "0603 10kΩ ±1% 电阻 100只包邮", "price": 1.0, "score": 60},
            {"index": 1, "title": "0603 10kΩ ±1% 电阻 20只/包", "price": 1.2, "score": 55},
        ],
    }

    def _open_pack(ln, idx, *a, **k):
        pack = 100 if idx == 0 else 20
        price = 1.0 if idx == 0 else 1.2
        return {
            "ok": True, "item": {"title": f"item{idx}"}, "detail_price": price,
            "pack_size": pack, "suggested_qty": 1, "actual_pcs": pack,
            "unit_price": round(price / pack, 6), "sku_groups": [], "suggestions": [],
            "unresolved": [], "no_sku": True,
        }

    runner.open_item = _open_pack
    _br2 = runner._add_line_to_cart(dict(_one), prefer_cheap=True, max_candidates=3)
    check("有更小包装才换店", len(_br2.get("attempts") or []) >= 2, len(_br2.get("attempts") or []))
    check("换到了 20 只那家", (_br2.get("actual_pcs") or 0) <= 20, _br2.get("actual_pcs"))
finally:
    runner.search, runner.open_item = _bak_search_open

print("== 打开商品超时：重试一次，不要直接判死 ==")
_bak_search, _bak_open, _bak_add = runner.search, runner.open_item, runner.add_to_cart
_flaky = {"n": 0}
runner.search = lambda ln, query=None, require_scored=False: {
    "ok": True,
    "query": "0603 10kΩ 电阻",
    "items_count": 1,
    "candidates": [{"index": 0, "title": "0603 10kΩ ±1% 电阻 100只", "price": 2.0, "score": 50}],
}
_open_result = {
    "ok": True,
    "item": {"title": "0603 10kΩ ±1% 电阻 100只"},
    "detail_price": 2.0,
    "sku_groups": [],
    "no_sku": True,
    "suggestions": [],
    "unresolved": [],
    "pack_size": 100,
    "pack_source": "title",
    "bom_qty": 1,
    "suggested_qty": 1,
    "actual_pcs": 100,
    "unit_price": 0.02,
}


def _flaky_open(ln, index):
    _flaky["n"] += 1
    if _flaky["n"] == 1:
        raise tr.SessionError("Page.goto: Timeout 30000ms exceeded.", "OPEN_FAILED")
    return _open_result


runner.open_item = _flaky_open
runner.add_to_cart = lambda *a, **k: {
    "ok": True, "qty": 1, "final_price": 2.0, "cost": 2.0,
    "pack_size": 100, "actual_pcs": 100, "unit_price": 0.02, "message": "已加入购物车",
}
try:
    _to = dict(line)
    _to["qty"] = 1
    _to["line_no"] = 901
    _r_to = runner._add_line_to_cart(_to, prefer_cheap=True, max_candidates=3)
finally:
    runner.search, runner.open_item, runner.add_to_cart = _bak_search, _bak_open, _bak_add
check("超时重试后仍然买成", _r_to.get("status") == "ok", _r_to.get("status"))
check("重试过一次", _flaky["n"] == 2, _flaky["n"])
check("重试痕迹记在结果里", bool(_r_to.get("retries")), _r_to.get("retries"))
check("验证拦截不被当偶发错误", tr._is_transient_error(tr.SessionError("需要验证", "VERIFICATION_REQUIRED")) is False)
check("导航超时算偶发错误", tr._is_transient_error(tr.SessionError("Page.goto: Timeout", "OPEN_FAILED")) is True)

print("== 截图自动清理 ==")
import tempfile

_tmp_runs = pathlib.Path(tempfile.mkdtemp(prefix="shots-"))
_art = BASE / "tests" / "artifacts"
_art.mkdir(parents=True, exist_ok=True)
_real_runs = tr.RUNS
tr.RUNS = _tmp_runs
try:
    for i in range(3):
        (_art / f"unit-shot-{i}.png").write_bytes(b"x" * 1024)
        (_tmp_runs / f"unit-shot-{i}.png").write_bytes(b"x" * 1024)
    (_tmp_runs / "keep-me.log").write_text("log", encoding="utf-8")
    res = runner.cleanup_screenshots(keep=0)
    check("截图被清空", res["deleted"] == 6, res)
    check("统计到释放体积", res["freed_bytes"] == 6 * 1024, res)
    check("非截图文件不动", (_tmp_runs / "keep-me.log").exists())
    for i in range(2):
        (_tmp_runs / f"unit-shot-{i}.png").write_bytes(b"y" * 10)
    runner.cleanup_screenshots(keep=1)
    check("keep=1 保留最新一张", len(list(_tmp_runs.glob("*.png"))) == 1)
finally:
    tr.RUNS = _real_runs
    import shutil as _sh

    _sh.rmtree(_tmp_runs, ignore_errors=True)
    for p in _art.glob("unit-shot-*.png"):
        p.unlink(missing_ok=True)

print("== 进度不会被空批量覆盖 ==")
_pf = pathlib.Path(tempfile.mkdtemp(prefix="prog-")) / "batch-progress.json"
_orig_pf = tr.PROGRESS_FILE
_bak_batch = runner._batch  # 测完必须还回去，否则 stop() 会把测试数据写进真实进度
tr.PROGRESS_FILE = _pf
try:
    tr.save_batch_progress({"updated_at": "t0", "total": 119,
                            "lines": {"1": {"status": "ok"}, "2": {"status": "ok"}}})
    # 模拟会话停止后 _reset_state 把内存批量清空，finally 再来存一次
    runner._batch = {"running": False, "total": 0, "results": []}
    runner._save_progress()
    _d = tr.load_batch_progress()
    check("空批量不覆盖旧进度", len(_d.get("lines") or {}) == 2, _d)

    # 同一行旧结论被新结论覆盖（失败要能翻案成成功）
    runner._batch = {"running": False, "total": 119,
                     "results": [{"line_no": 2, "status": "failed", "message": "x"}]}
    runner._save_progress()
    _d = tr.load_batch_progress()
    check("新结论覆盖同行旧结论", (_d["lines"]["2"]["status"] == "failed"
                                and _d["lines"]["1"]["status"] == "ok"), _d)
finally:
    tr.PROGRESS_FILE = _orig_pf
    runner._batch = _bak_batch
    import shutil as _sh2

    _sh2.rmtree(_pf.parent, ignore_errors=True)

print("== 停止 ==")
st = runner.stop()
check("已停止", not st["running"], st)

print("== 金额：单价 × 份数记账 ==")
check("拿到页面单价", _saved_add.get("final_price") == 3.5, _saved_add.get("final_price"))
check("合计价 = 单价 × 份数", _saved_add.get("cost") == 350.0, _saved_add.get("cost"))
check("份数一并记下", _saved_add.get("qty_sent") == 100, _saved_add.get("qty_sent"))

print("== 金额：批量汇总只算有价的行 ==")
_bak = runner._batch
runner._batch = {
    "running": False,
    "total": 4,
    "results": [
        {"line_no": 1, "status": "ok", "cost": 3.5},
        {"line_no": 2, "status": "ok", "cost": 0.12},
        {"line_no": 3, "status": "ok", "cost": None},
        {"line_no": 4, "status": "failed", "cost": None},
    ],
}
_sum = runner.batch_status()
check("合计金额", _sum.get("cost_total") == 3.62, _sum.get("cost_total"))
check("有实付价的行数", _sum.get("cost_lines") == 2, _sum.get("cost_lines"))
check("成功行数", _sum.get("ok_count") == 3, _sum.get("ok_count"))
check("均价只在有价行上算", _sum.get("cost_avg") == 1.81, _sum.get("cost_avg"))
runner._batch = _bak

print("== 收尾指标：_save_run_metrics 能落盘（收尾路径曾引用未定义的 _log） ==")
_bak_batch = runner._batch
_bak_runs = tr.RUNS
_tr_tmp = BASE / "tests" / "_metrics_tmp"
_tr_tmp.mkdir(parents=True, exist_ok=True)
tr.RUNS = _tr_tmp
runner._batch = {
    "started_at": "2026-11-07T13:00:00",
    "finished_at": "2026-11-07T13:01:00",
    "cart_count": 3,
    "max_candidates": 20,
    "results": [
        {"line_no": 1, "status": "ok", "total_ms": 4000, "search_ms": 1500,
         "open_ms": 800, "select_ms": 700, "add_ms": 900, "attempts": [{"keyword": "x"}]},
        {"line_no": 2, "status": "pending", "total_ms": None},
    ],
}
try:
    runner._save_run_metrics()
    _files = sorted(_tr_tmp.glob("metrics-*.json"))
    check("指标文件已生成", len(_files) == 1, _files)
    _meta = json.loads(_files[0].read_text(encoding="utf-8")) if _files else {}
    check("均值只算跑完的行", _meta.get("mean_total_ms") == 4000, _meta.get("mean_total_ms"))
    check("只统计 ok/failed/uncertain", _meta.get("count") == 1, _meta.get("count"))
finally:
    tr.RUNS = _bak_runs
    runner._batch = _bak_batch
    shutil.rmtree(_tr_tmp, ignore_errors=True)

print("== 收尾对账：ok 行在购物车找不到必须降级 uncertain（P0-1） ==")
_bak_batch = runner._batch
runner._batch = {
    "running": False,
    "total": 3,
    "results": [
        {"line_no": 1, "status": "ok",
         "candidate": {"title": "0603贴片电阻 10KΩ ±1% (100只)", "price": 1.0}},
        {"line_no": 2, "status": "ok",
         "candidate": {"title": "0805贴片电容 47uF 25V X5R", "price": 2.0}},
        {"line_no": 3, "status": "uncertain", "candidate": {"title": "别的"}},
    ],
}
# 车里有 #1 的商品，没有 #2 的 → #2 必须被降级，#1 不动，uncertain 行不参与
_cart_fake = {
    "cart_item_count": 1,
    "rows_degraded": False,
    "rows": [{"title": "0603贴片电阻 10KΩ ±1% (100只)", "price": 1.0}],
}
runner._reconcile_cart(_cart_fake)
_re = runner._batch.get("reconcile") or {}
check("对账统计了 ok 行", _re.get("checked") == 2, _re)
check("找不到的行被降级", runner._batch["results"][1]["status"] == "uncertain", runner._batch["results"][1])
check("降级行带对账说明", "收尾对账" in (runner._batch["results"][1].get("message") or ""))
check("核对到的行保持 ok", runner._batch["results"][0]["status"] == "ok")
check("记录了差异清单", _re.get("downgraded") == [2], _re)
check("uncertain 行不被对账改动", runner._batch["results"][2]["status"] == "uncertain")

print("== 收尾对账：明细降级时不冤枉人 ==")
runner._batch["results"][1]["status"] = "ok"
runner._batch["results"][1]["message"] = ""
runner._reconcile_cart({"cart_item_count": 2, "rows_degraded": True, "rows": []})
check("degraded 时不降级", runner._batch["results"][1]["status"] == "ok")
check("记录 degraded 标志", (runner._batch.get("reconcile") or {}).get("cart_degraded") is True)
runner._batch = _bak_batch

print("== 进度恢复：跑完的批不再写「服务重启」（P2-4） ==")
_orig_lbp = tr.load_batch_progress
_rargs = dict(command=[sys.executable, str(BASE / "tests" / "fake_session.py")],
              repo=BASE, artifacts=BASE / "tests" / "artifacts")
tr.load_batch_progress = lambda: {
    "total": 2,
    "started_at": "2026-11-07T14:00:00",
    "finished_at": "2026-11-07T14:10:00",
    "stopped_reason": "",
    "lines": {
        "1": {"line_no": 1, "status": "ok", "headline": "A"},
        "2": {"line_no": 2, "status": "ok", "headline": "B"},
    },
}
try:
    _r_done = tr.SessionRunner(**_rargs)
    check("已跑完 → phase 不写重启", "已跑完" in _r_done._batch["phase"], _r_done._batch["phase"])
    check("已跑完 → stopped_reason 不写 server restarted",
          _r_done._batch.get("stopped_reason") != "server restarted", _r_done._batch.get("stopped_reason"))
    check("保留 finished_at", _r_done._batch.get("finished_at") == "2026-11-07T14:10:00")

    tr.load_batch_progress = lambda: {
        "total": 3,
        "lines": {
            "1": {"line_no": 1, "status": "ok", "headline": "A"},
            "2": {"line_no": 2, "status": "pending", "headline": "B"},
        },
    }
    _r_half = tr.SessionRunner(**_rargs)
    check("有 pending → 提示续跑", "继续加购" in _r_half._batch["phase"], _r_half._batch["phase"])
    check("中断批标 server restarted", _r_half._batch.get("stopped_reason") == "server restarted")
finally:
    tr.load_batch_progress = _orig_lbp

print("== 演练模式不做收尾对账（2026-11-09） ==")
# dry-run 本来就不点加购，购物车里当然找不到；以前照样跑 _reconcile_cart，
# 把演练的 ok 全降级成 uncertain，演练报告全是「待核对」。
_bak_batch3 = runner._batch
runner._batch = {
    "running": True, "total": 1, "done": 0, "index": -1, "phase": "",
    "results": [{"line_no": 1, "status": "pending", "message": ""}],
    "stopped_reason": "",
}
_cv_calls = []
runner._add_line_to_cart = lambda *a, **k: {
    "line_no": 1, "status": "ok", "message": "演练：未点加购（仅校验选型）", "attempts": [],
}
runner.send = lambda *a, **k: _cv_calls.append(a) or {"data": {"cart_item_count": 0, "rows": []}}
runner._save_run_metrics = lambda: None
runner.cleanup_screenshots = lambda: None
try:
    runner._run_batch([{"line_no": 1, "query_plan": ["0603 10kΩ"]}], True, 3, dry_run=True)
    check("演练模式不读购物车对账", not _cv_calls, _cv_calls)
    check("演练行保持 ok 不被降级", runner._batch["results"][0]["status"] == "ok",
          runner._batch["results"][0])
    check("演练不写 reconcile", not runner._batch.get("reconcile"), runner._batch.get("reconcile"))
finally:
    for _attr in ("_add_line_to_cart", "send", "_save_run_metrics", "cleanup_screenshots"):
        runner.__dict__.pop(_attr, None)
    runner._batch = _bak_batch3

print("== 浏览器中途被关：整批中止，不记成「搜不到」（2026-11-16） ==")
# 实测：浏览器在跑批中途被关掉，剩下 17 行全是「搜索词都没有可用结果」，
# 报出来像选型失败，实际是环境没了。这种必须整批停下来并说明原因。
_bak_batch4 = runner._batch
runner._batch = {
    "running": True, "total": 3, "done": 0, "index": -1, "phase": "",
    "results": [{"line_no": i, "status": "pending", "message": ""} for i in (1, 2, 3)],
    "stopped_reason": "",
}
_bg_calls = []
runner._add_line_to_cart = lambda *a, **k: _bg_calls.append(a) or {
    "line_no": 1, "status": "ok", "message": "", "attempts": [{"ok": True}],
}
runner._save_run_metrics = lambda: None
runner.cleanup_screenshots = lambda: None
runner._browser_gone = True
try:
    runner._run_batch(
        [{"line_no": i, "query_plan": ["0603 10kΩ"]} for i in (1, 2, 3)], True, 3, dry_run=True
    )
    check("浏览器没了 → 一行都不跑", len(_bg_calls) == 0, _bg_calls)
    check("给出中止原因", "浏览器" in (runner._batch.get("stopped_reason") or ""),
          runner._batch.get("stopped_reason"))
    check("不把剩余行记成失败",
          all(r["status"] != "failed" for r in runner._batch["results"]),
          [r["status"] for r in runner._batch["results"]])
finally:
    runner._browser_gone = False
    for _attr in ("_add_line_to_cart", "_save_run_metrics", "cleanup_screenshots"):
        runner.__dict__.pop(_attr, None)
    runner._batch = _bak_batch4

print("== 购物车确认：加购无回执时查车（2026-11-09） ==")
_bak_send2 = runner.send
runner.send = lambda *a, **k: {"data": {"rows_degraded": False,
                                        "rows": [{"title": "0603贴片电阻 10KΩ ±1% (100只)"}]}}
check("标题命中 → True", runner._cart_contains("0603贴片电阻 10KΩ ±1% (100只)")[0] is True)
check("前缀命中也算", runner._cart_contains("0603贴片电阻10KΩ±1%")[0] is True)
check("车里没有 → False", runner._cart_contains("完全不存在的型号XYZ")[0] is False)
check("同封装不同阻值不算命中", runner._cart_contains("0603贴片电阻 1KΩ ±1%")[0] is False)
runner.send = lambda *a, **k: {"data": {"rows_degraded": True, "rows": []}}
check("明细降级 → None（不下结论）", runner._cart_contains("任意商品")[0] is None)
runner.send = lambda *a, **k: {"data": {"rows_degraded": False, "rows": [], "cart_item_count": 0}}
check("购物车读成空 → None（不能当没加上）", runner._cart_contains("任意商品")[0] is None)
runner.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
check("会话出错 → None", runner._cart_contains("任意商品")[0] is None)
runner.send = _bak_send2

print("== 标题比对：规格原子而不是字符串相似度（2026-11-12） ==")
# 「0603 10kΩ」和「0603 1kΩ」相似度 0.96，纯相似度必然误判，所以按料号/封装/值比。
_SAME_TITLES = [
    ("0603贴片电阻 10KΩ ±1% (100只)", "0603贴片电阻10KΩ±1%100只"),
    ("贴片电阻 0603 10KΩ ±1%", "0603贴片电阻 10KΩ ±1% 100只"),
    ("1N5819W SOD-123 二极管", "1N5819W S4 B5819WS SOD-123 肖特基二极管"),
    ("AMS1117-3.3 SOT-223", "AMS1117-3.3V 1117稳压电源芯片 sot-223"),
]
_DIFF_TITLES = [
    ("0603贴片电阻 10KΩ ±1%", "0603贴片电阻 1KΩ ±1%"),
    ("0805贴片电容 47uF 25V X5R", "0805贴片电容 47nF 25V X5R"),
    ("XH2.54-6P 连接器", "PH2.0-6P 连接器"),
    ("SS34 SMA 二极管", "SS14 SMA 二极管"),
    ("0603 10kΩ 电阻", "0805 10kΩ 电阻"),
    ("0603 10kΩ ±1% 1/4W", "0603 10kΩ ±1% 1/8W"),
    ("0603 100nF 50V X7R", "0603 100nF 100V X7R"),
]
for _a, _b in _SAME_TITLES:
    check(f"同款认得出：{_a[:12]}…", tr._titles_match(_a, _b), (_a, _b))
for _a, _b in _DIFF_TITLES:
    check(f"不同款分得开：{_a[:12]}…", not tr._titles_match(_a, _b), (_a, _b))
check("4K7 夹点写法认成 4.7k", tr._spec_atoms("4K7 0603")["resistance"] == {4700.0},
      tr._spec_atoms("4K7 0603"))
check("1/4W 不被记成 4W", 4.0 not in tr._spec_atoms("1/4W 470R 0603")["power"],
      tr._spec_atoms("1/4W 470R 0603"))

print("== 收尾对账：读车异常时不再整车降级（2026-11-12） ==")
# 现场：19 行 18 行被「购物车里没找到」降级。真相更可能是车被清空/没渲染完，
# 不是 18 行同时加购失败 —— 所以要判「读车异常」，保留 ok 但标「未核实」。


def _mk_ok(n):
    return [{"line_no": i + 1, "status": "ok", "message": "已加入购物车",
             "candidate": {"title": f"物料{i + 1} 0603 {i + 1}kΩ"}, "cost": 1.0}
            for i in range(n)]


_bak_batch_rc = runner._batch
runner._batch = {"results": _mk_ok(18)}
runner._reconcile_cart({"rows": [], "rows_degraded": False, "cart_item_count": 0, "cart_empty": True})
_rc0 = runner._batch.get("reconcile") or {}
check("空车判为读车异常", _rc0.get("anomaly") is True, _rc0)
check("空车不降级任何一行", _rc0.get("downgraded") == [], _rc0)
check("空车后 18 行仍为 ok", all(x["status"] == "ok" for x in runner._batch["results"]))
check("空车给每行打 cart_unverified", all(x.get("cart_unverified") for x in runner._batch["results"]))
check("message 写明未核实", "未核实" in runner._batch["results"][0]["message"],
      runner._batch["results"][0].get("message"))

runner._batch = {"results": _mk_ok(6)}
runner._reconcile_cart({"rows": [{"title": "完全无关的商品 A"}, {"title": "完全无关的商品 B"}],
                        "rows_degraded": False, "cart_item_count": 2})
_rc1 = runner._batch.get("reconcile") or {}
check("整批都不匹配 → 判异常", _rc1.get("anomaly") is True, _rc1)
check("整批都不匹配 → 不降级", _rc1.get("downgraded") == [], _rc1)

runner._batch = {"results": _mk_ok(10)}
runner._reconcile_cart({"rows": [{"title": "物料1 0603 1kΩ"}],
                        "rows_degraded": False, "cart_item_count": 1})
_rc2 = runner._batch.get("reconcile") or {}
check("匹配率 10% → 判异常", _rc2.get("anomaly") is True, _rc2)
check("匹配率 10% → 不降级", _rc2.get("downgraded") == [], _rc2)

# 个别行对不上时维持原语义：只降级那几行
runner._batch = {"results": _mk_ok(6)}
runner._reconcile_cart({"rows": [{"title": "物料1 0603 1kΩ"}, {"title": "物料2 0603 2kΩ"},
                                 {"title": "物料3 0603 3kΩ"}, {"title": "物料4 0603 4kΩ"},
                                 {"title": "物料6 0603 6kΩ"}],
                        "rows_degraded": False, "cart_item_count": 5})
_rc3 = runner._batch.get("reconcile") or {}
check("匹配率 5/6 → 不算异常", _rc3.get("anomaly") is False, _rc3)
check("只降级对不上的第 5 行",
      [x["line_no"] for x in runner._batch["results"] if x["status"] == "uncertain"] == [5],
      [x["status"] for x in runner._batch["results"]])
check("记录了 matched 数", _rc3.get("matched") == 5, _rc3)
runner._batch = _bak_batch_rc

print("== 开跑前购物车预检（2026-11-11） ==")
_bak_send3 = runner.send
_bak_batch4 = runner._batch


def _precheck_case(count):
    runner._batch = {"running": False, "results": [], "stopped_reason": ""}
    runner.send = lambda *a, **k: {"data": {"cart_item_count": count}}
    runner._precheck_cart_limit()
    return dict(runner._batch)


_at_limit = _precheck_case(195)
check("接近上限直接不跑", "先清理" in (_at_limit.get("stopped_reason") or ""), _at_limit.get("stopped_reason"))
_ok = _precheck_case(20)
check("正常件数不拦", not _ok.get("stopped_reason"), _ok.get("stopped_reason"))
check("记录了开跑前件数", _ok.get("cart_count_before") == 20, _ok.get("cart_count_before"))
_warn = _precheck_case(160)
check("150+ 仅提醒不拦", not _warn.get("stopped_reason"), _warn.get("stopped_reason"))
runner.send = _bak_send3
runner._batch = _bak_batch4

print("== 批量结果导出（CSV 行） ==")
_rows = tr.batch_export_rows(
    [
        {
            "line_no": 3,
            "status": "ok",
            "headline": "0603 10kΩ ×2",
            "candidate": {"title": "0603WAF1002T5E"},
            "sku": [{"label": "阻值", "value": "10kΩ"}],
            "qty": 1,
            "cost": 1.284,
            "cart_verified_via": "xhr",
            "total_ms": 5234,
            "message": "已加入\n购物车",
        },
        {"line_no": 4, "status": "skipped", "message": "跳过"},
        {"line_no": 5, "status": "pending"},
    ]
)
check("表头 10 列", len(_rows[0]) == 10, _rows[0])
check("ok 行字段齐全", _rows[1][0] == "3" and _rows[1][6] == "1.28" and _rows[1][7] == "xhr", _rows[1])
check("备注里的换行被压平", "\n" not in _rows[1][9], _rows[1][9])
check("pending 行不导出", all(r[0] != "5" for r in _rows[1:]), _rows)
# 对账没做成的行：确认方式列不能留空（空值会被当成「没有确认方式」而不是「没核实」）
_unv_rows = tr.batch_export_rows(
    [{"line_no": 7, "status": "ok", "cart_unverified": True, "headline": "X", "message": "未核实"}]
)
check("未核实行导出写进确认方式列", "未核实" in _unv_rows[1][7], _unv_rows[1])

print("== 续跑行带 SKU / 不覆盖进度（2026-11-11） ==")
# 以前 resume 时只把金额搬回来，SKU 和 cart_verified_via 丢了 —— 界面上那条记录
# 只剩价格，导出 CSV 的「SKU」「确认方式」两列是空的；而且续跑行会被重新落盘，
# 把上一轮真实的 message/耗时覆盖成「续跑跳过」+ null。
_orig_lbp_resume = tr.load_batch_progress
_orig_sbp_resume = tr.save_batch_progress
_orig_sbl_resume = tr.save_batch_lines
_orig_lblm_resume = tr.load_batch_lines_meta
_bak_batch_r = runner._batch
_bak_run_r = runner._run_batch
_bak_clean_r = runner.cleanup_screenshots
_saved = []
# 不读磁盘上真实的 batch-lines.json：用户跑过一批之后，那份指纹跟这里的假行
# 天然对不上，续跑会被正确判成「换了 BOM」而作废 —— 这是产品行为，不是测试场景。
tr.load_batch_lines_meta = lambda: {}
tr.load_batch_progress = lambda: {
    "lines": {
        "1": {
            "status": "ok",
            "headline": "0603 10kΩ ×2",
            "message": "已加入购物车",
            "sku": [{"label": "阻值", "value": "10kΩ"}],
            "cart_verified_via": "xhr",
            "cost": 1.28,
            "final_price": 1.28,
            "total_ms": 5234,
        }
    }
}
tr.save_batch_lines = lambda lines: None
tr.save_batch_progress = lambda payload: _saved.append(payload)
runner._browser_gone = False
runner._browser_unreachable = lambda *a, **k: False
runner._run_batch = lambda *a, **k: None
runner.cleanup_screenshots = lambda: None
try:
    _st = runner.start_batch(
        [{"line_no": 1, "qty": 2, "query_plan": ["0603 10kΩ"],
          "decision": {"headline": "0603 10kΩ ×2"}},
         # 第 2 行没有进度：留一行待跑，否则「全部已跑完」会被新的
         # BATCH_ALL_DONE 闸门直接拒掉（那是产品行为，见 test_batch_progress_guard §5b）
         {"line_no": 2, "qty": 1, "query_plan": ["0805 100nF"],
          "decision": {"headline": "0805 100nF ×1"}}],
        resume=True,
    )
    _r1 = (_st.get("results") or [{}])[0]
    check("续跑行带回 SKU", _r1.get("sku") == [{"label": "阻值", "value": "10kΩ"}], _r1.get("sku"))
    check("续跑行带回确认方式", _r1.get("cart_verified_via") == "xhr", _r1.get("cart_verified_via"))
    # 续跑行不能覆盖磁盘上的真实记录
    runner._save_progress()
    _persisted = {}
    for _p in _saved:
        _persisted.update(_p.get("lines") or {})
    check("续跑行不重新落盘（message 不被冲掉）", "1" not in _persisted, _persisted.keys())
    # 结论真的变了（收尾对账把 ok 降成 uncertain）还是要落盘，但明细沿用上一轮
    _saved.clear()
    runner._batch["results"][0].update(status="uncertain",
                                       message="收尾对账：购物车里没找到该商品")
    runner._save_progress()
    _persisted = {}
    for _p in _saved:
        _persisted.update(_p.get("lines") or {})
    _row1 = _persisted.get("1") or {}
    check("降级后的结论会落盘", _row1.get("status") == "uncertain", _row1.get("status"))
    check("新 message 生效", "购物车里没找到" in (_row1.get("message") or ""), _row1.get("message"))
    check("明细沿用上一轮（SKU 还在）", _row1.get("sku") == [{"label": "阻值", "value": "10kΩ"}], _row1.get("sku"))
    check("耗时不被抹成 null", _row1.get("total_ms") == 5234, _row1.get("total_ms"))
finally:
    tr.load_batch_progress = _orig_lbp_resume
    tr.save_batch_progress = _orig_sbp_resume
    tr.save_batch_lines = _orig_sbl_resume
    tr.load_batch_lines_meta = _orig_lblm_resume
    runner._run_batch = _bak_run_r
    runner.cleanup_screenshots = _bak_clean_r
    runner._batch = _bak_batch_r

print("== 没有搜索词的行直接跳过（2026-11-11） ==")
# 残缺行（型号/名称/参数全空，只剩封装）没有 query_plan，跑下去只会抛 NO_QUERY，
# 白等一轮再记一条「失败」。现在直接标 skipped，并且不去点搜索/加购。
_bak_batch_q = runner._batch
_calls_q = []
_bak_add_q = runner._add_line_to_cart
runner._add_line_to_cart = lambda *a, **k: _calls_q.append(a) or {
    "line_no": 2, "status": "ok", "message": "已加入购物车", "attempts": [],
}
runner.send = lambda *a, **k: {"data": {"cart_item_count": 0, "rows": []}}
runner._save_run_metrics = lambda: None
runner.cleanup_screenshots = lambda: None
runner._precheck_cart_limit = lambda *a, **k: None
runner.hide_browser = lambda: None
runner._batch = {
    "running": True, "total": 2, "done": 0, "index": -1, "phase": "",
    "results": [{"line_no": 1, "status": "pending", "message": ""},
                {"line_no": 2, "status": "pending", "message": ""}],
    "stopped_reason": "",
}
try:
    runner._run_batch(
        [
            {"line_no": 1, "qty": 30, "package": "SOT-23", "status": "needs_user",
             "blocking": ["整行没有可用信息"], "query_plan": [], "decision": {}},
            {"line_no": 2, "qty": 1, "query_plan": ["0603 10kΩ"],
             "decision": {"headline": "0603 10kΩ"}},
        ],
        True, 3, dry_run=True,
    )
    _r_skip = runner._batch["results"][0]
    check("无搜索词行标 skipped", _r_skip["status"] == "skipped", _r_skip)
    check("跳过原因写进 message", "整行没有可用信息" in (_r_skip.get("message") or ""), _r_skip.get("message"))
    check("跳过的行不去搜索加购", len(_calls_q) == 1, len(_calls_q))
    check("有搜索词的行照常跑", runner._batch["results"][1]["status"] == "ok",
          runner._batch["results"][1])
finally:
    runner._add_line_to_cart = _bak_add_q
    for _attr in ("_save_run_metrics", "cleanup_screenshots", "_precheck_cart_limit", "hide_browser"):
        runner.__dict__.pop(_attr, None)
    runner._batch = _bak_batch_q

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
