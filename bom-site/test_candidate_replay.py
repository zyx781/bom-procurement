#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选回放：python test_candidate_replay.py（不联网、不开浏览器）

规则全是对着淘宝标题做判断的：59 条正则、20 处评分权重、几十条 H 编号闸门。
改其中任何一条，以前只能重新跑一次真机（两分钟、要登录、还会撞滑块）才知道
有没有把别的行弄坏，而且「弄坏了」不一定当场看得出来。

这里把真机上**实际打开过**的商品标题固化成夹具，直接跑两段只看标题的闸门：
  1. `_check_title` —— 型号/规格/封装/针数/间距
  2. `_material_conflict` —— 材料族系、功率放大、耐压超标

覆盖边界要说清楚：**SKU 层（链接里那一堆颜色分类）不在这套里**。历史上有 7 条
候选是在 SKU 层被拒的（标题写「1K-820K」，要到选项里才发现没有 5kΩ 这一档），
那需要抓到 SKU 选项才能回放，属于下一步。所以本文件的通过数不等于「都能买」。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
FIXTURE = ROOT / "tests" / "fixtures" / "replay-titles.json"

import engine  # noqa: E402
import taobao_runner as tr  # noqa: E402

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


def verdict(line, title):
    code, text, _checks, risk = engine._check_title(line, {"title": title, "price": 1.0}, {})
    if code:
        return {"stage": "title", "code": code, "text": (text or "")[:110], "risk": bool(risk)}
    why = tr._material_conflict(line, title) or ""
    if why:
        return {"stage": "material", "code": "MAT", "text": why[:110], "risk": True}
    return {"stage": "", "code": "", "text": "", "risk": bool(risk)}


records = json.loads(FIXTURE.read_text(encoding="utf-8"))
real = [r for r in records if r["source"] == "real"]
curated = [r for r in records if r["source"] == "curated"]

print("== 夹具完整性 ==")
check("回放夹具存在且不为空", len(records) >= 30, f"got={len(records)}")
check("含真机标题", len(real) >= 20, f"got={len(real)}")
check("含对抗样本", len(curated) >= 4, f"got={len(curated)}")

print("\n== 对抗样本：危险结论必须按预期 ==")
for r in curated:
    got = verdict(r["line"], r["title"])
    want_pass = r["want"] == "pass"
    ok = (got["stage"] == "") if want_pass else (got["stage"] != "")
    check(
        f"[{r['line']['desc'] or r['line']['line_no']}] {r['title'][:32]} 应{'通过' if want_pass else '拒绝'}",
        ok,
        f"want={r['want']} got={got['stage'] or 'pass'} {got['text']}",
    )

print("\n== 真机标题：判定不许漂移 ==")
drift = []
for r in real:
    got = verdict(r["line"], r["title"])
    pinned = r.get("pinned") or {}
    if got != pinned:
        drift.append(
            f"#{r['line']['line_no']} {r['title'][:34]}："
            f"{pinned.get('stage') or 'pass'} → {got['stage'] or 'pass'}"
        )
check(f"{len(real)} 条真机标题判定稳定", not drift, "\n      " + "\n      ".join(drift[:10]))

print("\n== 覆盖情况（供人工核对，不算断言）==")
n_pass = sum(1 for r in real if (r.get("pinned") or {}).get("stage") == "")
n_mat = sum(1 for r in real if (r.get("pinned") or {}).get("stage") == "material")
n_sku = sum(1 for r in real if not r.get("was_ok") and (r.get("pinned") or {}).get("stage") == "")
print(f"  真机候选 {len(real)} 条：标题/材料闸门放行 {n_pass}，被材料闸门拦下 {n_mat}")
print(f"  其中历史上被拒 {sum(1 for r in real if not r.get('was_ok'))} 条，"
      f"离线能复现 {sum(1 for r in real if not r.get('was_ok') and (r.get('pinned') or {}).get('stage'))} 条，"
      f"剩 {n_sku} 条是 SKU 层（未覆盖）")

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
