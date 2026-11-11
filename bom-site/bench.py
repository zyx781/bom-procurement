#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多轮回归 / 速度测试驱动器。

用法：
  python bench.py --rounds 3 --dry                 # 演练 3 轮全量，不碰购物车
  python bench.py --rounds 5 --dry --only 61,107    # 只跑指定行号
  python bench.py --rounds 1 --clear-cart           # 真实加购 1 轮（先清空购物车）
  python bench.py --rounds 2 --dry --sample 30      # 随机抽 30 行

每轮结束打印：总耗时、每行均值、最慢 10 行、失败/待核对清单。
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNS = BASE / "runs"
API = "http://127.0.0.1:8765"
MASTER_FILE = RUNS / "bom-lines-master.json"  # 全量母本，永不被子集覆盖
LINES_FILE = RUNS / "batch-lines.json"


def call(path: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    url = API + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 409/5xx 也带 JSON 错误体，别让整轮崩掉
        body = e.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            data = {"ok": False, "error": body[:200]}
        data.setdefault("http_code", e.code)
        return data


class BenchBlocked(RuntimeError):
    """会话被验证扣住 / 浏览器不在：继续跑只会空转+加重风控，直接熔断。"""


def session_preflight() -> None:
    """跑一轮前的体检：会话没就绪或正在验证 → 熔断，不再硬撞。"""
    try:
        s = call("/api/session/status", timeout=15)
    except Exception as exc:  # noqa: BLE001
        raise BenchBlocked(f"服务不可达：{exc}") from exc
    if s.get("verification"):
        raise BenchBlocked("会话处于验证待人工状态（verification=true）")
    if not s.get("ready"):
        raise BenchBlocked("会话未就绪（ready=false），可能浏览器已关闭")


def load_lines() -> list:
    """优先读母本 —— batch-lines.json 会被每次 batch-start 覆盖成当轮子集。"""
    src = MASTER_FILE if MASTER_FILE.exists() else LINES_FILE
    data = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("lines") or []
    return data


def pick_lines(all_lines: list, only: str | None, sample: int | None) -> list:
    rows = all_lines
    if only:
        want = {int(x) for x in only.replace("，", ",").split(",") if x.strip()}
        rows = [ln for ln in rows if int(ln.get("line_no") or -1) in want]
    if sample and sample < len(rows):
        rows = random.sample(rows, sample)
        rows.sort(key=lambda ln: int(ln.get("line_no") or 0))
    return rows


def batch_state() -> dict:
    return call("/api/taobao/batch", timeout=30)


def run_round(lines: list, dry: bool, clear_cart: bool, label: str) -> dict:
    print(f"\n=== [{label}] 行数={len(lines)} 模式={'演练' if dry else '真实加购'} ===", flush=True)
    session_preflight()
    if clear_cart and not dry:
        # 购物车里全是本工具前几轮跑出来的测试商品，重跑前必须先清，
        # 否则同一行会叠成 2 件、3 件。服务端要求显式 all=true + confirm="清空"。
        before = call("/api/taobao/cart", {"keyword": None}, timeout=120)
        if before.get("http_code") == 409 or before.get("code") == "VERIFICATION_REQUIRED":
            raise BenchBlocked("cart-view 被验证拦截（409）")
        print(f"清空前购物车 {before.get('cart_item_count')} 件", flush=True)
        t0 = time.monotonic()
        res = {}
        after = before
        for _ in range(6):  # 购物车一页 30 件左右，全清要按页删几轮
            res = call("/api/taobao/cart-remove", {"all": True, "confirm": "清空"}, timeout=600)
            if res.get("http_code") == 409 or res.get("code") == "VERIFICATION_REQUIRED":
                raise BenchBlocked("cart-remove 被验证拦截（409）")
            after = call("/api/taobao/cart", {"keyword": None}, timeout=120)
            if after.get("http_code") == 409:
                raise BenchBlocked("cart-view 被验证拦截（409）")
            if not (after.get("cart_item_count") or 0):
                break
            if not res.get("ok"):
                break
        print(
            f"清空购物车：{res.get('message') or res} 剩 {after.get('cart_item_count')} 件 "
            f"({time.monotonic() - t0:.1f}s)",
            flush=True,
        )
    if not dry:
        call("/api/taobao/batch-reset", {}, timeout=30)
    t_start = time.monotonic()
    started = call(
        "/api/taobao/batch-start",
        {"lines": lines, "resume": False, "dry_run": dry, "max_candidates": 3},
        timeout=60,
    )
    if not started.get("ok", True) or started.get("http_code") == 409:
        raise BenchBlocked(f"batch-start 被拒：{started.get('error') or started.get('message') or started.get('code')}")
    last_done = -1
    stall = 0
    summary_blocked = False
    while True:
        time.sleep(4)
        try:
            s = batch_state()
        except Exception as exc:  # noqa: BLE001
            print(f"  状态读取失败：{exc}", flush=True)
            continue
        if not s.get("running"):
            stop_reason = s.get("stopped_reason") or ""
            if "验证" in stop_reason:
                print(f"  !! 批量已暂停：{stop_reason}（需要人工过一次滑块）", flush=True)
                summary_blocked = True
            break
        done = s.get("done") or 0
        if done != last_done:
            stall = 0
            last_done = done
            el = time.monotonic() - t_start
            per = el / max(1, done)
            print(
                f"  {done}/{s.get('total')} {s.get('phase')} "
                f"已用 {el / 60:.1f}min 均值 {per:.1f}s/行 ETA {(s.get('total') - done) * per / 60:.1f}min",
                flush=True,
            )
        else:
            stall += 1
            if stall % 15 == 0:
                print(f"  ... 卡在 {s.get('phase')}（{stall * 4}s 无新增）", flush=True)
            if stall > 150:  # 10 分钟毫无进展，别再等
                print("  判定卡死，发送停止", flush=True)
                call("/api/taobao/batch-stop", {}, timeout=60)
                break
    el = time.monotonic() - t_start
    rows = [
        r for r in (batch_state().get("results") or [])
        if r.get("status") in ("ok", "failed", "uncertain")
    ]
    times = [r.get("total_ms") / 1000 for r in rows if r.get("total_ms")]
    summary = {
        "label": label,
        "dry": dry,
        "count": len(rows),
        "wall_s": round(el, 1),
        "mean_line_s": round(st.mean(times), 2) if times else None,
        "median_line_s": round(st.median(times), 2) if times else None,
        "max_line_s": round(max(times), 2) if times else None,
        "ok": sum(1 for r in rows if r.get("status") == "ok"),
        "uncertain": sum(1 for r in rows if r.get("status") == "uncertain"),
        "failed": sum(1 for r in rows if r.get("status") == "failed"),
        "cart_count": batch_state().get("cart_count"),
        "blocked": summary_blocked,
    }
    print(
        f"[{label}] 收尾：{summary['ok']} ok / {summary['uncertain']} 待核对 / {summary['failed']} 失败 "
        f"墙钟 {el / 60:.1f}min 均值 {summary['mean_line_s']}s/行 最慢 {summary['max_line_s']}s",
        flush=True,
    )
    bad = [r for r in rows if r.get("status") != "ok"]
    for r in bad:
        print(
            f"   ! #{r.get('line_no')} {r.get('status')} {r.get('headline')} :: "
            f"{(r.get('message') or '')[:80]}",
            flush=True,
        )
    slow = sorted([r for r in rows if r.get("total_ms")], key=lambda r: -r["total_ms"])[:10]
    for r in slow:
        print(
            f"   slow #{r.get('line_no')} {r['total_ms'] / 1000:.1f}s "
            f"(搜索{(r.get('search_ms') or 0) / 1000:.1f} 开页{(r.get('open_ms') or 0) / 1000:.1f} "
            f"SKU{(r.get('select_ms') or 0) / 1000:.1f} 加购{(r.get('add_ms') or 0) / 1000:.1f})",
            flush=True,
        )
    summary["results"] = [
        {
            "line_no": r.get("line_no"),
            "status": r.get("status"),
            "headline": r.get("headline"),
            "message": (r.get("message") or "")[:120],
            "total_ms": r.get("total_ms"),
            "search_ms": r.get("search_ms"),
            "open_ms": r.get("open_ms"),
            "select_ms": r.get("select_ms"),
            "add_ms": r.get("add_ms"),
            "cost": r.get("cost"),
        }
        for r in rows
    ]
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--clear-cart", action="store_true")
    ap.add_argument("--gap", type=float, default=20.0, help="轮间隔秒数（给风控降火）")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    lines = pick_lines(load_lines(), args.only, args.sample)
    if not lines:
        raise SystemExit("没有可执行的行（检查 --only / runs/batch-lines.json）")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUNS / f"bench-{stamp}{('-'+args.tag) if args.tag else ''}.json"
    all_rounds = []
    blocked_streak = 0  # 连续卡验证且 0 进展的轮数 —— 风控在盯梢时继续撞只会更糟
    for i in range(1, args.rounds + 1):
        label = f"R{i}"
        try:
            summary = run_round(lines, args.dry, args.clear_cart, label)
        except BenchBlocked as exc:
            print(f"\n!! [{label}] 熔断：{exc}，剩余轮次取消（别在风控盯梢时硬撞）", flush=True)
            break
        all_rounds.append(summary)
        out.write_text(json.dumps(all_rounds, ensure_ascii=False, indent=2), encoding="utf-8")
        if summary.get("blocked") and not summary.get("ok"):
            blocked_streak += 1
            if blocked_streak >= 2:
                print(
                    "\n!! 连续 2 轮卡验证且零进展：会话已被风控重点盯梢，"
                    "自动停止今晚跑批。请人工在浏览器完成验证后再单独起跑。",
                    flush=True,
                )
                break
        else:
            blocked_streak = 0
        if not args.dry:
            # 子集跑会覆盖 batch-lines.json，跑完立刻还原成全量母本
            LINES_FILE.write_text(MASTER_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        if i < args.rounds:
            time.sleep(args.gap)
    print("\n=== 汇总 ===", flush=True)
    for s in all_rounds:
        print(
            f"{s['label']}: {s['ok']}ok/{s['uncertain']}?/{s['failed']}x "
            f"墙钟 {s['wall_s'] / 60:.1f}min 均值 {s['mean_line_s']}s/行",
            flush=True,
        )
    good = [s["mean_line_s"] for s in all_rounds if s["mean_line_s"]]
    if good:
        print(f"全程每行均值 {st.mean(good):.2f}s（最好 {min(good)}s / 最差 {max(good)}s）", flush=True)
    print(f"明细已存：{out.name}", flush=True)


if __name__ == "__main__":
    main()
