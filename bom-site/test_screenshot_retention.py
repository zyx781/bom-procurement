#!/usr/bin/env python3
"""截图留存：跑完只留这一批的证据图。

踩过的两个坑，这里各钉一条：

1. `cleanup_screenshots` 以前只 glob `*.png`，而 `persist_screenshot` 存的是 `.jpg`
   —— 清理从没生效过，runs/ 堆到 223 张 27 MB。
2. 停会话时按「全清」执行，把用户下一次点「删上一次加购」时核对用的图一起删了。

现在的口径：**进度里点名的截图一张不动，其余全删**；`screenshot_keep` 只额外保留最近 N 张。
"""
import json
import pathlib
import sys
import tempfile
import time

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import taobao_runner as tr  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_env(tmp: pathlib.Path, shots: dict[str, float], progress: dict, archived: dict | None = None):
    """铺一个假的 runs/：截图按 {名字: mtime 偏移天} 建，进度写进临时文件。"""
    runs = tmp / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    arts = tmp / "artifacts"
    arts.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name, days in shots.items():
        p = runs / name
        p.write_bytes(b"x" * 1024)
        ts = now - days * 86400
        import os

        os.utime(p, (ts, ts))
    if progress is not None:
        (runs / "batch-progress.json").write_text(
            json.dumps(progress, ensure_ascii=False), encoding="utf-8"
        )
    if archived is not None:
        (runs / "batch-progress-prev.json").write_text(
            json.dumps(archived, ensure_ascii=False), encoding="utf-8"
        )
    # 指向临时目录，别碰真实 runs/
    tr.RUNS = runs
    tr.PROGRESS_FILE = runs / "batch-progress.json"
    return runs, arts


def runner(arts: pathlib.Path):
    r = tr.SessionRunner(artifacts=str(arts))
    r._proc = None
    r._batch = {}
    r._append_log = lambda *a, **k: None
    return r


def rec(shot):
    return {"status": "ok", "screenshot": shot}


print("== 1. 只留进度里点名的图（.jpg 也要能删，以前只认 .png）==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(
        tmp,
        {
            "20261116-200122-cart_add_bom-20261116-174023.jpg": 0.1,   # 本批第 1 件
            "20261116-200128-cart_add_bom-20261116-174023.jpg": 0.1,   # 本批第 2 件
            "20261109-101010-cart_add_bom-20261109-095959.jpg": 8,     # 上一批
            "20261108-090000-probe.png": 9,                            # 更早的采集图
            "20261107-080000-cart_add_bom-20261107-075959.jpg": 10,    # 更早的批次
        },
        {"lines": {"1": rec("/runs/20261116-200122-cart_add_bom-20261116-174023.jpg"),
                   "2": rec("/runs/20261116-200128-cart_add_bom-20261116-174023.jpg")},
         "started_at": "2026-11-16T20:01:16"},
    )
    r = runner(arts)
    out = r.cleanup_screenshots()
    left = sorted(p.name for p in runs.iterdir() if p.suffix.lower() in tr.SHOT_EXTS)
    check("删掉 3 张旧图", out["deleted"] == 3, out)
    check("本批 2 张 .jpg 都留着", left == [
        "20261116-200122-cart_add_bom-20261116-174023.jpg",
        "20261116-200128-cart_add_bom-20261116-174023.jpg",
    ], left)
    check("返回里带上本批张数", out.get("kept_batch") == 2, out)

print("== 2. 停会话不清证据图（旧实现是「全清」）==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(
        tmp,
        {"20261116-210000-cart_add_bom-20261116-205959.jpg": 0.1,
         "20261108-090000-old.jpg": 9},
        {"lines": {"5": rec("/runs/20261116-210000-cart_add_bom-20261116-205959.jpg")}},
    )
    r = runner(arts)
    r.cleanup_screenshots()
    left = [p.name for p in runs.iterdir() if p.suffix.lower() in tr.SHOT_EXTS]
    check("证据图还在（下次删车要核对）", "20261116-210000-cart_add_bom-20261116-205959.jpg" in left, left)
    check("旧图仍被清掉", "20261108-090000-old.jpg" not in left, left)

print("== 3. 进度被清过（重跑/重置）→ 退到底档，别把要删的那批图删了 ==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(
        tmp,
        {"20261116-220000-cart_add_bom-20261116-215959.jpg": 0.1,
         "20261101-010101-ancient.jpg": 16},
        None,  # 当前进度已被 clear_batch_progress 清掉
        {"lines": {"3": rec("/runs/20261116-220000-cart_add_bom-20261116-215959.jpg")}},
    )
    r = runner(arts)
    out = r.cleanup_screenshots()
    left = [p.name for p in runs.iterdir() if p.suffix.lower() in tr.SHOT_EXTS]
    check("底档里点名的图保留", "20261116-220000-cart_add_bom-20261116-215959.jpg" in left, left)
    check("没有进度也不至于一张不删", out["deleted"] >= 1, out)

print("== 4. screenshot_keep=N 只是额外留最近 N 张 ==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(
        tmp,
        {"20261116-230000-batch.jpg": 0.1,
         "20261115-120000-recent.jpg": 1,
         "20261114-120000-older.jpg": 2,
         "20261113-120000-oldest.jpg": 3},
        {"lines": {"1": rec("/runs/20261116-230000-batch.jpg")}},
    )
    r = runner(arts)
    out = r.cleanup_screenshots(keep=2)
    left = sorted(p.name for p in runs.iterdir() if p.suffix.lower() in tr.SHOT_EXTS)
    check("证据图 + 最近 2 张 = 留 3 张", len(left) == 3, left)
    check("留的是最近的两张，不是最旧的", "20261115-120000-recent.jpg" in left and "20261115-120000-recent.jpg" in left, left)
    check("最旧的被删", "20261113-120000-oldest.jpg" not in left, left)
    check("keep_extra 如实回报", out.get("keep_extra") == 2, out)

print("== 5. 批量运行中一律不动 ==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(tmp, {"20261116-235959-x.jpg": 0.1}, {"lines": {}})
    r = runner(arts)
    r._batch = {"running": True}
    out = r.cleanup_screenshots()
    check("运行中直接跳过", out.get("skipped") == "批量运行中", out)
    check("一张没删", len(list(runs.glob("*.jpg"))) == 1, list(runs.glob("*.jpg")))

print("== 6. 非图片文件不许误伤 ==")
with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)
    runs, arts = make_env(tmp, {"20261116-111111-batch.jpg": 0.1}, {"lines": {}})
    (runs / "batch-progress.json").exists()  # 进度文件在
    (runs / "lcsc-cache.json").write_text("{}", encoding="utf-8")
    (runs / "session-bom-20261116-111111.log").write_text("log", encoding="utf-8")
    r = runner(arts)
    r.cleanup_screenshots()
    check("进度文件还在", (runs / "batch-progress.json").exists())
    check("缓存还在", (runs / "lcsc-cache.json").exists())
    check("日志还在", (runs / "session-bom-20261116-111111.log").exists())

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
