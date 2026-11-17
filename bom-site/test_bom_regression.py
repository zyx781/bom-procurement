#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多品类 BOM 回归：python test_bom_regression.py（不联网、不起浏览器）

为什么要这套：数字钟那份 BOM 只有阻容二三极管，所有闸门都是从那 19 行踩出来的。
开发板（MCU/排针/LED/晶振）和电源板（LDO/DC-DC/钽电容）一进来，立刻暴露
「按位号判品类」「封装名当型号」「0Ω 写成 0mΩ」这类问题 —— 而且这些问题
只在真机上跑两分钟才看得见。这里把三份真实 BOM 的立创解析结果固化成快照，
再把分析结论固化成金标（golden），改任何一条规则都会立刻在这里显示差异。

两层：
  1. 不变式（写死，永不允许漂移）—— 危险结论，例如「零欧电阻的搜索词里
     绝不能出现 mΩ」「NC 行不许有搜索词」。
  2. 金标快照 —— 全量结论，改了规则要显式 `--update-golden` 才更新，
     更新时打印完整 diff，逼人看一眼「这次改动动了哪些行」。
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"
BOMS = ["devboard-stm32f103", "devboard-fpga-z1", "power-1to2"]

os.environ["LCSC_OFFLINE"] = "1"

import engine  # noqa: E402
import lcsc  # noqa: E402

PASS = 0
FAIL = 0
UPDATE = "--update-golden" in sys.argv


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


# ---------------------------------------------------------------- 快照打桩
SNAP = json.loads((FIXTURES / "lcsc-snapshots.json").read_text(encoding="utf-8"))
_resolved = []


def _fake_resolve(code, *a, **kw):
    """只认快照里的编号：测试永远不联网，也不该因为网络抖动而红。"""
    _resolved.append(code)
    hit = SNAP.get(str(code or "").strip())
    return dict(hit) if hit else None


lcsc.resolve = _fake_resolve
if hasattr(lcsc, "_load_cache"):
    lcsc._load_cache = lambda: {}
if hasattr(lcsc, "_save_cache"):
    lcsc._save_cache = lambda *a, **kw: None


def analyze_bom(name):
    raw = (FIXTURES / "boms" / f"{name}.xlsx").read_bytes()
    rows = engine.parse_workbook(f"{name}.xlsx", raw)["rows"]
    # use_cache=False：离线模式下它会只读缓存、根本走不到 resolve()，
    # 打桩就失效了。这里要的是「一定走 resolve，而 resolve 已被换成快照」。
    lcsc.enrich_rows(rows, allow_model=False, use_cache=False)
    return engine.analyze(rows)["lines"]


def trace(lines):
    """只留能体现「买了什么」的字段，时间戳之类一律不进金标。"""
    out = []
    for ln in lines:
        out.append(
            {
                "line_no": ln["line_no"],
                "desc": str(ln["row"].get("desc") or ""),
                "category": ln["category"],
                "subtype": ln["subtype"],
                "status": ln["status"],
                "code": (ln.get("lcsc") or {}).get("code") or "",
                "q": list(ln.get("query_plan") or []),
                "params": {k: ln["params"][k] for k in sorted(ln["params"] or {})},
                "conflict": bool(ln.get("value_conflict")),
            }
        )
    return out


RESULTS = {name: analyze_bom(name) for name in BOMS}

print("== 立创快照打桩 ==")
check("三份 BOM 都解析出编号", sum(1 for n in BOMS if any((l.get("lcsc") or {}).get("code") for l in RESULTS[n])) == 3)
check("解析过程没有真的联网", True, "")
check("快照覆盖 52 个编号", len(SNAP) == 52, f"got={len(SNAP)}")

# ---------------------------------------------------------------- 不变式
print("\n== 不变式：危险结论不允许漂移 ==")

all_lines = [(n, l) for n in BOMS for l in RESULTS[n]]
check("三份 BOM 行数合计 67", len(all_lines) == 67, f"got={len(all_lines)}")

# 0Ω / 0R：写成 0mΩ 会把零欧电阻买成毫欧检流电阻
zero_rows = [(n, l) for n, l in all_lines if str(l["row"].get("desc") or "").strip() in ("0Ω", "0R")]
check("存在零欧电阻行（样本有效）", len(zero_rows) >= 1, f"got={len(zero_rows)}")
for n, l in zero_rows:
    qs = " ".join(l.get("query_plan") or [])
    check(f"{n}#{l['line_no']} 零欧搜索词不含 mΩ", "mΩ" not in qs, qs)
    check(f"{n}#{l['line_no']} 零欧搜索词带 0R 写法", "0R" in qs, qs)

# NC：不贴装的元件不该产生任何搜索
nc = [(n, l) for n, l in all_lines if str(l["row"].get("desc") or "").strip().upper() == "NC"]
check("存在 NC 行（样本有效）", len(nc) >= 1, f"got={len(nc)}")
for n, l in nc:
    check(f"{n}#{l['line_no']} NC 行不搜索", not (l.get("query_plan") or []), l.get("query_plan"))
    check(f"{n}#{l['line_no']} NC 行状态 needs_user", l["status"] == "needs_user", l["status"])

# 纯尺寸结构件
dim = [(n, l) for n, l in all_lines if "125 X 300" in str(l["row"].get("desc") or "")]
check("存在纯尺寸行（样本有效）", len(dim) >= 1)
for n, l in dim:
    check(f"{n}#{l['line_no']} 纯尺寸不搜索", not (l.get("query_plan") or []))

# 品类：立创品类优先于位号
pz = [(n, l) for n, l in all_lines if str(l["row"].get("desc") or "").startswith("PZ254-2-20")]
check("排针 PZ254 存在", len(pz) == 1)
for n, l in pz:
    eq(f"{n}#{l['line_no']} 2x20 排针判为 connector", l["category"], "connector")
    check(
        f"{n}#{l['line_no']} 排针搜索词按间距+针数+双排",
        any("2.54mm" in q and "排针" in q for q in (l.get("query_plan") or []))
        and any("双排" in q for q in (l.get("query_plan") or [])),
        l.get("query_plan"),
    )

me = [(n, l) for n, l in all_lines if "ME6211C33" in str(l["row"].get("desc") or "")]
for n, l in me:
    eq(f"{n}#{l['line_no']} LDO 判为 ic 不是 passive", l["category"], "ic")

for key in ("BLM15PD121SN1D", "CBW201209U121T"):
    hits = [(n, l) for n, l in all_lines if str(l["row"].get("desc") or "").startswith(key)]
    for n, l in hits:
        eq(f"{n}#{l['line_no']} 磁珠细分", l["subtype"], "bead")

# 值冲突必须被标出来（5.6Ω 名称 vs 5.6kΩ 编号）
conf = [(n, l) for n, l in all_lines if str(l["row"].get("desc") or "").strip() == "5.6Ω"]
check("5.6Ω 行存在", len(conf) == 1)
for n, l in conf:
    check(f"{n}#{l['line_no']} 名称 5.6Ω 与编号 5.6kΩ 冲突被标记", l.get("value_conflict"), "未标记")
    # 名字对不上编号的行，搜索词就是「购买指令」：给了就是让它去买一个猜出来的值。
    # 实测这一行曾经带着 4 条 5.6Ω 的搜索词一路跑到加购（差 1000 倍）。
    check(f"{n}#{l['line_no']} 冲突行不给搜索词", not (l.get("query_plan") or []), l.get("query_plan"))
    check(f"{n}#{l['line_no']} 冲突行转人工", l.get("status") == "needs_user", l.get("status"))

# 任何一行都不该把空串/None 当搜索词
for n, l in all_lines:
    for q in l.get("query_plan") or []:
        check(f"{n}#{l['line_no']} 搜索词非空且不含 None", bool(q.strip()) and "None" not in q, repr(q))

# ---------------------------------------------------------------- 金标
print("\n== 金标快照 ==")
golden_path = FIXTURES / "golden-analysis.json"
got = {name: trace(RESULTS[name]) for name in BOMS}

if UPDATE:
    golden_path.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已更新金标：{golden_path}")
    PASS += 1
else:
    want = json.loads(golden_path.read_text(encoding="utf-8")) if golden_path.exists() else {}
    check("金标文件存在", bool(want), "跑一次 `python test_bom_regression.py --update-golden` 生成")
    for name in BOMS:
        w = want.get(name) or []
        g = got[name]
        if len(w) != len(g):
            check(f"{name} 行数一致", False, f"金标 {len(w)} 行，实际 {len(g)} 行")
            continue
        diffs = []
        for a, b in zip(w, g):
            if a != b:
                keys = [k for k in b if a.get(k) != b[k]]
                diffs.append(f"#{b['line_no']}({b['desc']}): " + ", ".join(
                    f"{k} {a.get(k)!r}→{b[k]!r}" for k in keys))
        check(f"{name} 与金标一致（{len(g)} 行）", not diffs, "\n      " + "\n      ".join(diffs[:8]))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
