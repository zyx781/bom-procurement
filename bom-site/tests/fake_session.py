#!/usr/bin/env python3
"""假的 Taobao session，用于测试站点的执行层协议（不访问网络）。"""

import json
import os
import sys
import time
from pathlib import Path

TASK = "fake"
OPENED = {"index": 0}  # 当前打开的商品，cart-add 按它往车里塞


def out(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


ITEMS = [
    {"index": 0, "title": "0603 10KΩ ±1% 贴片电阻 100只 1/10W", "url": "https://item.taobao.com/item.htm?id=1", "price": "¥3.50", "price_value": 3.5, "sales_count": 12000, "rating": 0.98, "is_tmall": False, "free_shipping": True},
    {"index": 1, "title": "0603 10kΩ 1% 电阻 0.1W 50V", "url": "https://item.taobao.com/item.htm?id=2", "price": "¥2.80", "price_value": 2.8, "sales_count": 3400, "rating": 0.95, "is_tmall": False, "free_shipping": True},
    {"index": 2, "title": "0603 10K 5% 电阻 1/10W", "url": "https://item.taobao.com/item.htm?id=3", "price": "¥1.20", "price_value": 1.2, "sales_count": 50000, "rating": 0.97, "is_tmall": False, "free_shipping": False},
    {"index": 3, "title": "0805 10kΩ ±1% 电阻", "url": "https://item.taobao.com/item.htm?id=4", "price": "¥2.50", "price_value": 2.5, "sales_count": 5000, "rating": 0.97, "is_tmall": False, "free_shipping": True},
    {"index": 4, "title": "0603 10kΩ ±1% 电阻 编带", "url": "https://item.taobao.com/item.htm?id=5", "price": "¥3.80", "price_value": 3.8, "sales_count": 1500, "rating": 0.98, "is_tmall": True, "free_shipping": True},
]

GROUPS = [
    {"label": "阻值", "values": [{"text": "10KΩ", "disabled": False, "selected": False}, {"text": "100KΩ", "disabled": False, "selected": False}]},
    {"label": "精度", "values": [{"text": "±1%", "disabled": False, "selected": False}, {"text": "±5%", "disabled": False, "selected": False}]},
    {"label": "封装", "values": [{"text": "0603", "disabled": False, "selected": False}, {"text": "0805", "disabled": False, "selected": False}]},
    {"label": "数量", "values": [{"text": "100只", "disabled": False, "selected": True}]},
]

# 购物车状态：cart-view / cart-remove 都基于它（删除要能看到 before→after）
CART = [
    {"title": "0603 10KΩ ±1% 贴片电阻 100只 1/10W", "price": 3.5, "qty": 1, "spec": ""},
    {"title": "0603 100nF ±10% 50V X7R 贴片电容", "price": 1.0, "qty": 1, "spec": ""},
    {"title": "SS34 SMA 肖特基二极管", "price": 1.7, "qty": 1, "spec": ""},
]


def cart_rows():
    return [
        {
            "index": i,
            "title": c["title"],
            "spec": c.get("spec") or "",
            "price": c["price"],
            "price_text": f"¥{c['price']}",
            "qty": c.get("qty"),
            "url": "https://item.taobao.com/item.htm?id=fake",
        }
        for i, c in enumerate(CART)
    ]


def main():
    print("[taobao] session starting (task_id=fake)", file=sys.stderr, flush=True)
    print("[taobao] session ready, waiting for commands", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        cmd = req.get("cmd")
        # 测试用：把每条命令拖慢 N 秒，好观察前端在跑批过程中是不是真的在刷新
        _delay = float(os.environ.get("FAKE_DELAY_S") or 0)
        if _delay > 0 and cmd != "quit":
            time.sleep(_delay)
        if cmd == "search":
            kw = req.get("keyword", "")
            if "验证" in kw:
                out({"status": "error", "task_id": TASK, "error": {"message": "验证码", "code": "VERIFICATION_REQUIRED"}})
                continue
            if "空" in kw:
                out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "没有找到相关宝贝", "data": {"keyword": kw, "items": [], "items_count": 0}})
                continue
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "搜索结果", "data": {"keyword": kw, "items": ITEMS, "items_count": len(ITEMS)}})
        elif cmd == "open":
            OPENED["index"] = int(req.get("index", 0))
            for g in GROUPS:
                for v in g["values"]:
                    v["selected"] = v["text"] == "100只"
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "商品详情", "data": {"item": ITEMS[int(req.get("index", 0))], "sku_groups": GROUPS, "detail_price": 3.5}})
        elif cmd == "select-sku":
            for sel in req.get("selections", []):
                for g in GROUPS:
                    if g["label"] == sel.get("label"):
                        for v in g["values"]:
                            v["selected"] = v["text"] == sel.get("value")
            ok = all(any(v["selected"] for v in g["values"]) for g in GROUPS)
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "已选择", "data": {"all_selected": ok, "selections": req.get("selections"), "final_price": 3.5, "sku_groups": GROUPS}})
        elif cmd == "cart-add":
            if os.environ.get("FAKE_CART_GROW") == "1":
                # 标题必须与 open 返回的一致，否则收尾对账会判「车里找不到」而误降级
                _item = ITEMS[OPENED["index"]]
                CART.append({"title": _item["title"], "price": _item["price_value"],
                             "qty": req.get("qty") or 1, "spec": "阻值：10KΩ"})
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "已加入购物车", "data": {"cart_added": True, "confirmed": True, "qty": req.get("qty"), "qty_confirmed": True, "sku_verified": True, "sku_selection_count": 4, "item_index": 0}})
        elif cmd == "cart-view":
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "购物车", "data": {
                "cart_item_count": len(CART),
                "cart_empty": not CART,
                "rows": cart_rows(),
                "rows_page": len(CART),
                "rows_total": len(CART),
                "rows_degraded": False,
                "rows_selector": ".cart-item",
                "pages_read": 1,
            }})
        elif cmd == "cart-remove":
            kw = (req.get("keyword") or "").strip()
            all_flag = bool(req.get("all"))
            if not kw and not all_flag:
                out({"status": "error", "task_id": TASK,
                     "error": {"message": "Refusing an implicit cart clear", "code": "CART_REMOVE_AMBIGUOUS"}})
                continue
            before = len(CART)
            if all_flag:
                matched = list(CART)
                CART.clear()
            else:
                matched = [c for c in CART if kw.lower() in c["title"].lower()]
                for c in matched:
                    CART.remove(c)
            after = len(CART)
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "删除完成",
                 "data": {
                     "before": before, "after": after, "removed": before - after,
                     "matched": [m["title"] for m in matched],
                     "confirm_clicked": True,
                     "verified": after < before,
                     "pages_touched": 1,
                 }})
        elif cmd == "ping":
            # FAKE_VERIFY_FLAG 存在 = 页面还挂着验证，ping 也要回 VERIFICATION_REQUIRED
            flag = os.environ.get("FAKE_VERIFY_FLAG")
            if flag and Path(flag).exists():
                out({"status": "error", "task_id": TASK, "error": {"message": "验证码未过", "code": "VERIFICATION_REQUIRED"}})
            else:
                out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": None, "data": {"pong": True}})
        elif cmd == "quit":
            print("[taobao] session closed", file=sys.stderr, flush=True)
            return


if __name__ == "__main__":
    main()
