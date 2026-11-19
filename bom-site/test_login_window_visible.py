#!/usr/bin/env python3
"""启动时浏览器窗口必须在屏幕内。

2026-11-19 现场：用户点「启动淘宝会话」后，浏览器窗口开在屏幕外（或 profile
恢复成上次藏起来的坐标 -2000,0），界面上写着「等待你在浏览器中登录…」，
人却找不到窗口。用户原话：「界面一开始就是隐藏的，那我该如何登录」。

定下来的规则（按用户要求简化）：
1. 启动会话时窗口必须在屏幕内 —— 不看登录状态、不搞「登录成功后自动藏」；
2. 用户不想看就自己点「藏起来」，程序不再替用户做这个决定；
3. 只有主窗口（按尺寸过滤掉启动瞬间的占位矩形）真在屏幕内才不折腾。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

BASE = pathlib.Path(__file__).parent
sys.path.insert(0, str(BASE))

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


tmp = pathlib.Path(tempfile.mkdtemp(prefix="bom_visible_"))
tr.RUNS = tmp / "runs"
tr.RUNS.mkdir(parents=True, exist_ok=True)


class FakeWP:
    """够用的 window_pos 替身：窗口坐标集中在这里。"""

    SCREEN_POS = (80, 80)

    def __init__(self, rects=None):
        self.rects = rects if rects is not None else {1: (80, 80, 1400, 900)}
        self.shown = 0
        self.hidden = 0
        self.alive = True

    def windows_of(self, pids):
        return dict(self.rects)

    def on_screen(self, rect):
        return rect[0] >= 0 and rect[2] > 0 and rect[1] >= 0

    def is_offscreen(self, rect, screen_w=0):
        return not self.on_screen(rect)

    def show(self, pids=None, pos=None):
        self.shown += 1
        self.rects = {1: (80, 80, 1400, 900)}
        return True

    def hide(self, pids=None):
        self.hidden += 1
        self.rects = {1: (-2000, 0, -500, 1000)}
        return True

    def debug_browser_alive(self, port=9222, timeout=0.8):
        return self.alive

    def debug_edge_pids(self, port=9222):
        return set(self.rects)

    def debug_edge_pids_for(self, port):
        return set(self.rects)


class FakeProc:
    pid = 4242

    def poll(self):
        return None


print("== ensure_browser_visible：屏幕外才摆回来 ==")
wp = FakeWP()
tr._window_pos = wp
check("已经在屏幕内 → 不动", tr.ensure_browser_visible("http://127.0.0.1:9222") is False and wp.shown == 0,
      f"shown={wp.shown}")

wp = FakeWP({1: (-2000, 0, -500, 1000)})
tr._window_pos = wp
check("藏到屏幕外 → 摆回屏幕内", tr.ensure_browser_visible("http://127.0.0.1:9222") is True and wp.shown == 1,
      f"shown={wp.shown}")
check("摆回后坐标在屏幕内", wp.on_screen(wp.rects[1]), str(wp.rects))

wp = FakeWP({1: (-21333, -21333, -21175, -21307), 2: (90, 984, 885, 1010)})
tr._window_pos = wp
check("只有占位矩形（窗口还没显示）→ 主动摆正", tr.ensure_browser_visible("http://127.0.0.1:9222") is True
      and wp.shown == 1, f"shown={wp.shown}")

wp = FakeWP({1: (-2000, 0, -500, 1000), 2: (80, 80, 1400, 900)})
tr._window_pos = wp
check("有一个真窗口在屏幕内 → 不动（用户自己摆好的位置别抢）",
      tr.ensure_browser_visible("http://127.0.0.1:9222") is False and wp.shown == 0, f"shown={wp.shown}")

wp = FakeWP({})
tr._window_pos = wp
check("一个窗口都没有 → 不动", tr.ensure_browser_visible("http://127.0.0.1:9222") is False and wp.shown == 0)

print("== status：不再自动藏窗口 ==")


def make_runner(wp):
    tr._window_pos = wp
    r = tr.SessionRunner(command=[sys.executable, "-c", "pass"], repo=BASE)
    r._proc = FakeProc()  # type: ignore[assignment]
    r._log_path = None
    r._started_mono = time.monotonic()
    r._login_state = "ok"
    r._ready = True
    r._waiting_login = False
    return r


wp = FakeWP()
r = make_runner(wp)
r.status(auto_probe=False)
time.sleep(0.3)
check("已登录、就绪 → 不再自动藏", wp.hidden == 0, f"hidden={wp.hidden}")
check("代码里没有自动藏标记", "_autohide_done" not in (BASE / "taobao_runner.py").read_text(encoding="utf-8"))

print("== 入口一致 ==")
ps1 = (BASE / "launcher.ps1").read_text(encoding="utf-8-sig")
line = [ln for ln in ps1.splitlines() if "$argLine" in ln and "remote-debugging-port" in ln][0]
check("launcher 窗口开在屏幕内", "--window-position=100,60" in line, line)
check("launcher 不再把窗口放屏幕外", "-2000" not in line, line)
check("launcher 开完会主动摆正（Chromium 会恢复旧坐标）", "window_pos.py" in ps1 and '"show"' in ps1)
js = (BASE / "static" / "app.js").read_text(encoding="utf-8")
check("界面提示登录时窗口在哪个窗口", "显示淘宝窗口" in js)
check("界面不再承诺自动藏窗口", "自动藏" not in js)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
