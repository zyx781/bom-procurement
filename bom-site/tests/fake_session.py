#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假的 Taobao session，用于测试站点的执行层协议（不访问网络）。"""

import json
import os
import sys
from pathlib import Path

TASK = "fake"


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


def main():
    print("[taobao] session starting (task_id=fake)", file=sys.stderr, flush=True)
    print("[taobao] session ready, waiting for commands", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        cmd = req.get("cmd")
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
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "已加入购物车", "data": {"cart_added": True, "confirmed": True, "qty": req.get("qty"), "qty_confirmed": True, "sku_verified": True, "sku_selection_count": 4, "item_index": 0}})
        elif cmd == "cart-view":
            out({"status": "success", "task_id": TASK, "screenshot": None, "page_text_summary": "购物车", "data": {"cart_item_count": 3, "items": []}})
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
