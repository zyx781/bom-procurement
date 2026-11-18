#!/usr/bin/env python3
"""选型回放：拿真机抓到的 SKU 档位，重跑一遍「该选哪个值 / 该不该拒买」。

为什么单开一套：`test_candidate_replay.py` 只回放到标题层（标题写错了才拦得住），
而真机上最贵的一类错误发生在**标题对、SKU 选错**：
  · 一个链接挂 20 个档位，店里根本没有 BOM 要的那一档 → 挑了「最近的」；
  · 混合封装链接里选中了 2010 的 100R，BOM 要 0402 的 39kΩ；
  · 「下单备注型号」这种让买家自己备注的选项，实际发出什么值全靠运气。
这几种都在 2026-11-16 那次电源板真机演练里现形，当时全靠事后闸门兜住。

夹具 `tests/fixtures/sku-replay.json` 来自 `runs/sku-observations.jsonl`
（真机开页时自动留痕），由 `tests/build_sku_replay.py` 生成并人工标注期望。
不联网、不开浏览器。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
FIXTURE = ROOT / "tests" / "fixtures" / "sku-replay.json"

sys.path.insert(0, str(ROOT))
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


def replay(case):
    """按线上同样的顺序跑一遍：选值 → 材料闸门 → 值核实。

    注意「选了值」≠「会买」：只要还有任何一组 unresolved，执行层就会转人工，
    所以真正的判定是 will_buy（有选择且无未决项）。
    """
    line = case["line"]
    sels, unresolved = tr.suggest_sku(line, case.get("sku_options") or [], case.get("title") or "")
    picks = {s["label"]: s["value"] for s in sels}
    will_buy = bool(picks) and not unresolved
    if picks:
        _joined = " ".join(picks.values())
        # 选中了就得能过材料闸门（族系/介质/功率…）
        mat = tr._material_conflict(line, _joined)
        # 并且值必须真的读得到 —— 这是「宁可买不到也不买错」的底线
        need = tr._required_value_kinds(line)
        proved = tr._title_value_kinds(line, _joined + " " + str(case.get("title") or ""))
        miss = need - proved
        return picks, unresolved, mat, miss, will_buy
    return picks, unresolved, "", set(), False


def main() -> int:
    if not FIXTURE.exists():
        print(f"缺少夹具 {FIXTURE}：先跑 python tests/build_sku_replay.py")
        return 1
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    print(f"== 选型回放（{len(cases)} 家真店）==")
    for case in cases:
        name = case["id"]
        picks, unresolved, mat, miss, will_buy = replay(case)
        exp = case["expect"]
        if exp.get("pick"):
            check(f"{name}：会买", will_buy, f"picks={picks} unresolved={unresolved[:1]}")
            check(f"{name}：选中的值", picks == exp["pick"], f"got={picks} want={exp['pick']}")
            check(f"{name}：过材料闸门", not mat, mat)
            check(f"{name}：值可核实", not miss, sorted(miss))
        else:
            check(f"{name}：不许自动买", not will_buy, f"got={picks}")
            _want = exp.get("reason_contains") or ""
            _why = " ".join(u.get("reason", "") for u in unresolved)
            if _want:
                check(f"{name}：理由说清（含「{_want}」）", _want in _why, _why[:120])
            check(f"{name}：确实给了未决项", bool(unresolved), unresolved[:1])

    # 不变式：任何一家「会自动买」的店，选出来的值都必须能核实到 BOM 要的参数。
    # 这条比逐条期望更硬 —— 以后新增店铺、改动打分函数，只要选了个对不上的值就会红。
    print("== 不变式：选中即核实 ==")
    for case in cases:
        picks, _, mat, miss, will_buy = replay(case)
        if not will_buy:
            continue
        check(f"{case['id']}：选中值能核实 BOM 参数", not miss, sorted(miss))
        check(f"{case['id']}：材料/族系不冲突", not mat, mat)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
