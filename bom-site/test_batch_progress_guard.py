#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换 BOM 后不许复用上一批进度（2026-11-11 真实事故回归）。

事故经过：新 BOM 恰好也是 19 行，`start_batch(resume=True)` 的 done_map 只按
line_no 匹配，把上一批 19 条结果整套套过来 —— 一行没跑；收尾对账拿旧候选
商品去空购物车里找，19 行全降级「待核对」。用户看到的是「全部识别成功，
但一件都没加进购物车」。

跑法：python test_batch_progress_guard.py（不依赖 pytest / 浏览器 / 网络）
"""

import json
import pathlib
import sys
import tempfile
import threading

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import taobao_runner as tr  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def make_lines(mpns, qty=1):
    out = []
    for i, mpn in enumerate(mpns, 1):
        out.append(
            {
                "line_no": i,
                "mpn": mpn,
                "desc": mpn,
                "package": "DIP-8",
                "params": "",
                "qty": qty,
                "query_plan": [mpn],
                "decision": {
                    "headline": f"{mpn} DIP-8 原装 ×{qty}",
                    "search_queries": [mpn],
                },
            }
        )
    return out


class FakeRunner:
    """只借用真实 start_batch，其余外部副作用全部隔离。"""

    start_batch = tr.SessionRunner.start_batch

    def __init__(self):
        self._batch = {}
        self._state_lock = threading.RLock()
        self._browser_gone = False

    def _browser_unreachable(self):
        return False

    def cleanup_screenshots(self):
        return {}

    def _save_progress(self):
        pass

    def _append_log(self, msg):
        pass

    def _run_batch(self, *a, **k):
        pass

    def batch_status(self):
        return dict(self._batch)


class SavingRunner(FakeRunner):
    _save_progress = tr.SessionRunner._save_progress


BOM_A = make_lines(["NE555P", "CD4518BE", "SS34"])
BOM_B = make_lines(["LM358P", "CD4017BE", "1N4148"])
FP_A = tr.batch_fingerprint(BOM_A)
FP_B = tr.batch_fingerprint(BOM_B)


def prior_for(lines, fingerprint=FP_A):
    rows = {}
    for ln in lines:
        rows[str(ln["line_no"])] = {
            "status": "ok",
            "headline": ln["decision"]["headline"],
            "qty": ln["qty"],
            "candidate": {"title": "上一批的旧商品", "price": 1.0},
            "final_price": 1.0,
        }
    return {
        "updated_at": "2026-11-11T11:55:42",
        "total": len(lines),
        "fingerprint": fingerprint,
        "lines": rows,
    }


print("== 1. BOM 指纹 ==")
check("同一份 BOM 指纹稳定", FP_A == tr.batch_fingerprint(make_lines(["NE555P", "CD4518BE", "SS34"])))
check("换 BOM 指纹不同", FP_A != FP_B)
check("只改数量也算换 BOM", FP_A != tr.batch_fingerprint(make_lines(["NE555P", "CD4518BE", "SS34"], qty=9)))
check("行顺序不同指纹不同", FP_A != tr.batch_fingerprint(make_lines(["CD4518BE", "NE555P", "SS34"])))
check("空列表不报错", isinstance(tr.batch_fingerprint([]), str) and len(tr.batch_fingerprint([])) == 16)
# 引擎升级会改 headline（摘要/口径文案）。那不是「换了 BOM」，不能让用户
# 刷新一次页面就被告知「旧进度作废」——一作废就会整批重跑、重复加购。
_renamed = make_lines(["NE555P", "CD4518BE", "SS34"])
for _ln in _renamed:
    _ln["decision"]["headline"] = "（引擎升级后的新文案）"
check("引擎改文案不算换 BOM", FP_A == tr.batch_fingerprint(_renamed))
_with_cand = make_lines(["NE555P", "CD4518BE", "SS34"])
_with_cand[0]["candidates"] = [{"title": "某商品", "price": 9.9}]
check("候选商品变化不算换 BOM", FP_A == tr.batch_fingerprint(_with_cand))

print("== 2. 进度归属判定 ==")
ok, why = tr.progress_belongs_to(prior_for(BOM_A), BOM_A)
check("同 BOM 判定可续跑", ok is True, why)
check("同 BOM 用 fingerprint 判定", why == "fingerprint")
ok, why = tr.progress_belongs_to(prior_for(BOM_A), BOM_B)
check("换 BOM 判定不可续跑", ok is False, why)
check("无进度时放行", tr.progress_belongs_to({}, BOM_B)[0] is True)
legacy = prior_for(BOM_A)
legacy.pop("fingerprint")
ok, why = tr.progress_belongs_to(legacy, BOM_B)
check("老进度无指纹 → headline 兜底识破换表", ok is False, why)
ok, why = tr.progress_belongs_to(legacy, BOM_A)
check("老进度无指纹但同表仍可续跑", ok is True, why)
legacy_empty = {"lines": {"1": {"status": "ok"}}}
check("老进度连 headline 都没有时不误杀", tr.progress_belongs_to(legacy_empty, BOM_A)[0] is True)

print("== 3. 进度摘要对外隐藏错配进度 ==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    saved = (tr.PROGRESS_FILE, tr.BATCH_LINES_FILE, tr.RUNS, tr.ACTIVE_BOM_FILE)
    tr.PROGRESS_FILE = tmp / "batch-progress.json"
    tr.BATCH_LINES_FILE = tmp / "batch-lines.json"
    tr.RUNS = tmp / "runs"
    tr.ACTIVE_BOM_FILE = tmp / "active-bom.json"
    tr.RUNS.mkdir(parents=True, exist_ok=True)
    saved_active = tr.ACTIVE_BOM_FINGERPRINT
    try:
        tr.save_batch_progress(prior_for(BOM_A))
        tr.save_batch_lines(BOM_A)

        summary_same = tr.batch_progress_summary(active_fingerprint=FP_A)
        check("同 BOM 摘要正常显示 3 行完成", summary_same["ok_count"] == 3, summary_same)
        check("同 BOM 不标 stale", summary_same["stale"] is False)

        summary_stale = tr.batch_progress_summary(active_fingerprint=FP_B)
        check("换 BOM 摘要隐藏 ok_count", summary_stale["ok_count"] == 0, summary_stale)
        check("换 BOM 摘要 has_progress=False", summary_stale["has_progress"] is False)
        check("换 BOM 摘要标 stale", summary_stale["stale"] is True)
        check("换 BOM 不删进度文件（精准清车还要用）", tr.PROGRESS_FILE.exists())
        check("不带 active 指纹时保留原摘要", tr.batch_progress_summary()["ok_count"] == 3)

        print("== 4. 当前 BOM 持久化 ==")
        tr.ACTIVE_BOM_FINGERPRINT = ""
        check("set_active_bom 返回新指纹", tr.set_active_bom(BOM_B) == FP_B)
        tr.ACTIVE_BOM_FINGERPRINT = ""  # 模拟服务重启，内存丢失
        check("重启后仍能读回当前 BOM 指纹", tr.active_bom_fingerprint() == FP_B)

        print("== 5. start_batch：同表续跑 / 换表从头跑 ==")
        # 同表，只跑了一半：应复用已完成的 2 行。
        # （3 行全跑完的情况见 §5b —— 那时 resume 会直接拒绝，不再静默空跑）
        tr.save_batch_progress(prior_for(BOM_A[:2]))
        tr.save_batch_lines(BOM_A)
        r1 = FakeRunner()
        st1 = r1.start_batch(BOM_A, resume=True)
        resumed1 = [x for x in st1["results"] if x.get("resumed")]
        check("同 BOM 续跑跳过已完成的 2 行", len(resumed1) == 2, st1.get("phase"))
        check(
            "没跑过的第 3 行仍是待跑",
            [x["status"] for x in st1["results"] if x["line_no"] == 3] == ["pending"],
        )
        check("同 BOM 不标记 stale_progress_dropped", st1.get("stale_progress_dropped") is False)

        # 换表：一行都不许复用
        tr.save_batch_progress(prior_for(BOM_A))
        tr.save_batch_lines(BOM_A)
        r2 = FakeRunner()
        st2 = r2.start_batch(BOM_B, resume=True)
        check("换 BOM 不复用任何旧行", all(not x.get("resumed") for x in st2["results"]))
        check("换 BOM 3 行全部待跑", all(x["status"] == "pending" for x in st2["results"]))
        check("换 BOM 不带上旧候选商品", all(not x.get("candidate") for x in st2["results"]))
        check("换 BOM 标记 stale_progress_dropped", st2.get("stale_progress_dropped") is True)
        check("换 BOM 清空旧进度文件", not tr.PROGRESS_FILE.exists())
        check("换 BOM 写入新指纹", tr.load_batch_lines_meta().get("fingerprint") == FP_B)
        check("换 BOM 状态里带上新指纹", st2.get("fingerprint") == FP_B)

        # 老进度（无 fingerprint）+ 换表：headline 兜底也要拦住
        legacy2 = prior_for(BOM_A)
        legacy2.pop("fingerprint")
        tr.save_batch_progress(legacy2)
        tr.save_batch_lines(BOM_A)
        r3 = FakeRunner()
        st3 = r3.start_batch(BOM_B, resume=True)
        check("老进度换 BOM 也被拦住", all(not x.get("resumed") for x in st3["results"]))

        # 进度自带明确指纹时以它为准，不被 batch-lines 的旧指纹误伤
        tr.save_batch_progress(prior_for(BOM_B[:2], FP_B))
        tr.save_batch_lines(BOM_A)  # 上次开跑是旧表，但进度明确属于新表
        r4 = FakeRunner()
        st4 = r4.start_batch(BOM_B, resume=True)
        check("明确同表进度不被 batch-lines 误伤", st4.get("stale_progress_dropped") is False)
        check(
            "明确同表仍正常续跑已完成的行",
            len([x for x in st4["results"] if x.get("resumed")]) == 2,
        )

        # 老进度没有指纹、headline 又被改得一致 → 只能靠 batch-lines 指纹兜底
        legacy3 = prior_for(BOM_A)
        legacy3.pop("fingerprint")
        for k, v in legacy3["lines"].items():
            v["headline"] = BOM_B[int(k) - 1]["decision"]["headline"]
        tr.save_batch_progress(legacy3)
        tr.save_batch_lines(BOM_A)
        r5 = FakeRunner()
        st5 = r5.start_batch(BOM_B, resume=True)
        check("老进度靠 batch-lines 指纹识破换表", st5.get("stale_progress_dropped") is True)

        print("== 5b. 重跑：全部跑完不许静默空跑 ==")
        # 用户真实反馈：觉得上次买错了想重跑，点一下按钮，done_map 把 ok 行全跳过，
        # 瞬间「跑完」—— 一行没动，还以为系统重跑了。
        tr.save_batch_progress(prior_for(BOM_B, FP_B))
        tr.save_batch_lines(BOM_B)
        r7 = FakeRunner()
        raised = None
        try:
            r7.start_batch(BOM_B, resume=True)
        except tr.SessionError as exc:  # noqa: PERF203
            raised = exc
        check("全部跑完再点继续 → 报 BATCH_ALL_DONE 而不是空跑",
              raised is not None and raised.code == "BATCH_ALL_DONE", raised)
        check("拒绝时不动进度文件", len((tr.load_batch_progress().get("lines") or {})) == 3)
        check("拒绝时不起批量任务", not r7._batch.get("running"))

        # 只重跑第 2 行
        r8 = FakeRunner()
        st8 = r8.start_batch(BOM_B, resume=True, redo_lines=[2])
        resumed8 = {x["line_no"] for x in st8["results"] if x.get("resumed")}
        check("redo_lines 只跳过第 1、3 行", resumed8 == {1, 3}, resumed8)
        check("被点名的第 2 行回到待跑",
              [x["status"] for x in st8["results"] if x["line_no"] == 2] == ["pending"])
        check("状态里记下重跑了哪几行", st8.get("redo_line_nos") == [2], st8.get("redo_line_nos"))

        # 整批重跑
        tr.save_batch_progress(prior_for(BOM_B, FP_B))
        r9 = FakeRunner()
        st9 = r9.start_batch(BOM_B, resume=True, redo=True)
        check("redo 整批不跳过任何行", all(not x.get("resumed") for x in st9["results"]))
        check("redo 全部行回到待跑", all(x["status"] == "pending" for x in st9["results"]))
        check("redo 清空旧进度文件", not tr.PROGRESS_FILE.exists())
        check("redo 状态标记 drop_prior_progress", st9.get("drop_prior_progress") is True)
        check("redo 阶段文案说明在重跑", "重新跑全部" in (st9.get("phase") or ""), st9.get("phase"))

        # 重跑落盘不能把上一轮的 ok 继承回来（_save_progress 是合并写）
        tr.save_batch_progress(prior_for(BOM_B, FP_B))
        r10 = SavingRunner()
        r10._batch = {
            "fingerprint": FP_B,
            "total": 3,
            "started_at": "t0",
            "finished_at": None,
            "drop_prior_progress": True,
            "results": [{"line_no": 2, "status": "failed", "headline": "x"}],
        }
        r10._save_progress()
        rows10 = tr.load_batch_progress().get("lines") or {}
        check("整批重跑后落盘不继承旧 ok 行", set(rows10.keys()) == {"2"}, rows10)

        # 摘要要把问题行号给出来，重跑按钮才知道重跑谁
        mixed = prior_for(BOM_B, FP_B)
        mixed["lines"]["1"]["status"] = "uncertain"
        mixed["lines"]["2"]["status"] = "failed"
        tr.save_batch_progress(mixed)
        sum_mixed = tr.batch_progress_summary(active_fingerprint=FP_B)
        check("摘要给问题行号", sum_mixed["problem_line_nos"] == [1, 2], sum_mixed)
        check("摘要的 ok 只剩第 3 行", sum_mixed["ok_line_nos"] == [3], sum_mixed)

        # 重跑清进度后，「删除上一批」还要能认出上一批的商品 —— 否则用户想清理
        # 重复商品时，记录已经被自己清掉了。
        tr.save_batch_progress(prior_for(BOM_B, FP_B))
        r11 = FakeRunner()
        r11.start_batch(BOM_B, resume=True, redo=True)
        archived = tr.load_archived_progress()
        check("重跑前留了进度底档", len(archived.get("lines") or {}) == 3, archived)
        check("清进度后仍能算出要删的商品", len(tr.batch_removal_targets()) == 3)

        print("== 6. 落盘时绑定指纹 ==")
        tr.clear_batch_progress()
        r6 = SavingRunner()
        r6._batch = {
            "fingerprint": FP_B,
            "total": 1,
            "started_at": "t0",
            "finished_at": "t1",
            "results": [
                {"line_no": 1, "status": "ok", "headline": "LM358P DIP-8 原装 ×1",
                 "qty": 1, "final_price": 2.5, "candidate": {"title": "LM358P"}}
            ],
        }
        r6._save_progress()
        written = json.loads(tr.PROGRESS_FILE.read_text(encoding="utf-8"))
        check("进度文件写入 fingerprint", written.get("fingerprint") == FP_B, written.get("fingerprint"))
    finally:
        (tr.PROGRESS_FILE, tr.BATCH_LINES_FILE, tr.RUNS, tr.ACTIVE_BOM_FILE) = saved
        tr.ACTIVE_BOM_FINGERPRINT = saved_active

print("== 7. 整批空跑兜底 ==")
full_resumed = {
    "results": [{"line_no": 1, "resumed": True, "status": "uncertain"},
                {"line_no": 2, "resumed": True, "status": "uncertain"}],
    "reconcile": {"downgraded": [1, 2]},
}
check("整批未执行 + 对账全降级 → 应清进度", tr.should_clear_stale_progress(full_resumed, 0) is True)
check("真跑过行就不清", tr.should_clear_stale_progress(full_resumed, 1) is False)
check("对账只降级一部分不清", tr.should_clear_stale_progress(
    {**full_resumed, "reconcile": {"downgraded": [1]}}, 0) is False)
check("没有 downgraded 不清", tr.should_clear_stale_progress(
    {**full_resumed, "reconcile": {"downgraded": []}}, 0) is False)
check("全新批次（无 resumed）不清", tr.should_clear_stale_progress(
    {"results": [{"line_no": 1, "status": "failed"}], "reconcile": {"downgraded": []}}, 0) is False)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
