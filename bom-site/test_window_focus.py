#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口/焦点测试（2026-11-08）：跑批时浏览器不许盖住用户正在用的软件。

用户反馈：「挑商品的时候页面会强行覆盖我正在用的软件，跑到最上层」。根因是执行层
用 CDP 驱动**用户自己的** Edge，Chromium 在部分输入/聚焦路径上会抢前台，而窗口
平时从来没被藏起来（旧代码只在等人工验证时才挪窗口）。

这组测试不碰真浏览器、不动真窗口：
* 纯函数（should_restore / pick_memory / is_offscreen）
* FocusGuard 用假 user32 跑 _tick，看它有没有压 Z 序 + 还焦点
* SessionRunner 用假 guard / 假 window_pos，看 hide/show/暂停/恢复的调用顺序
"""

import inspect
import pathlib
import sys

BASE = pathlib.Path(__file__).parent
sys.path.insert(0, str(BASE))

import focus_guard as wg  # noqa: E402
import taobao_runner as tr  # noqa: E402
import window_pos as wp  # noqa: E402

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


# ---------------------------------------------------------------- 纯判断
def test_pure():
    edge = {101, 102}
    check("前台是 Edge + 自动化持行中 → 该抢回来", wg.should_restore(101, edge, True))
    check("前台是 Edge 但自动化没在跑 → 不管", not wg.should_restore(101, edge, False))
    check("前台是用户自己的软件 → 不管", not wg.should_restore(999, edge, True))
    check("没有前台窗口 → 不管", not wg.should_restore(0, edge, True))

    check("记住用户窗口：Edge 在前台时保留旧值", wg.pick_memory(101, edge, 999) == 999)
    check("记住用户窗口：用户窗口在前台时更新", wg.pick_memory(555, edge, 999) == 555)
    check("记住用户窗口：没有前台时保留旧值", wg.pick_memory(0, edge, 999) == 999)

    check("藏屏外判定：(-2000,0,-500,1000) 算藏好", wp.is_offscreen((-2000, 0, -500, 1000), 1920))
    check("藏屏外判定：(80,80,1580,1080) 不算藏", not wp.is_offscreen((80, 80, 1580, 1080), 1920))


# ---------------------------------------------------------------- 假 user32
class FakeUser32:
    def __init__(self, fg=0):
        self.fg = fg
        self.foreground_calls = []
        self.zorder_calls = []

    def GetForegroundWindow(self):
        return self.fg

    def IsWindow(self, _h):
        return True

    def SetWindowPos(self, hwnd, insert_after, *_rest):
        self.zorder_calls.append((int(hwnd), int(insert_after)))
        return True

    def BringWindowToTop(self, hwnd):
        self.foreground_calls.append(int(hwnd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.foreground_calls.append(int(hwnd))
        self.fg = int(hwnd)
        return True

    def GetWindowThreadProcessId(self, _hwnd, _ptr):
        return 0


def test_guard_tick():
    fake = FakeUser32(fg=101)  # 101 = 调试 Edge 的窗口
    orig_user32, orig_windows = wg.wp._user32, wg.wp.windows_of
    orig_alive = wg.wp.debug_browser_alive
    wg.wp._user32 = lambda: fake
    wg.wp.windows_of = lambda pids: {101: (-2000, 0, -500, 1000)}
    wg.wp.debug_browser_alive = lambda port=9222, timeout=0.8: True
    try:
        guard = wg.FocusGuard(pids={1234}, tail_s=0.0)
        guard._user_hwnd = 999  # 用户本来在用的软件

        guard.begin()
        guard._tick()
        check(
            "Edge 抢到前台 → 压到 Z 序底部",
            (101, wp.HWND_BOTTOM) in fake.zorder_calls,
            str(fake.zorder_calls),
        )
        check("Edge 抢到前台 → 焦点还给用户窗口", 999 in fake.foreground_calls, str(fake.foreground_calls))
        check("统计里记了一次归还", guard.restores == 1, str(guard.restores))
        guard.end()

        # 暂停（等人工过验证）：用户点浏览器不该被压回去
        fake.zorder_calls.clear()
        fake.foreground_calls.clear()
        fake.fg = 101
        guard.pause("等人工过验证")
        guard.begin()
        guard._tick()
        check("暂停后不再抢回焦点", not fake.zorder_calls and not fake.foreground_calls)
        check("暂停时 active() 为假", not guard.active())
        guard.end()
        guard.resume()
        guard.begin()
        check("恢复后 active() 重新为真", guard.active())
        guard.end()

        # 用户在用自己的软件：守卫只更新记忆，不动他的窗口
        fake.zorder_calls.clear()
        fake.foreground_calls.clear()
        fake.fg = 555
        guard.begin()
        guard._tick()
        check("前台是用户软件时不动 Z 序", not fake.zorder_calls)
        check("前台是用户软件时记住它", guard._user_hwnd == 555, str(guard._user_hwnd))
        guard.end()

        # 没在跑命令时也要记住用户窗口：否则第一次抢前台时不知道该还给谁
        # （实测踩过：restores=0，前台一直留在 Edge）
        fresh = wg.FocusGuard(pids={1234}, tail_s=0.0)
        fake.fg = 777
        fresh._tick()
        check("不活动时也记住用户窗口", fresh._user_hwnd == 777, str(fresh._user_hwnd))
        fake.fg = 101
        fresh.begin()
        fake.zorder_calls.clear()
        fresh._tick()
        check("第一次抢前台就能还回去", 777 in fake.foreground_calls and fresh.restores == 1,
              f"{fake.foreground_calls} restores={fresh.restores}")
        fresh.end()

        # 命令区间外（tail 过期）即使 Edge 在前台也不动手
        fake.zorder_calls.clear()
        fake.foreground_calls.clear()
        fake.fg = 101
        guard._tail_until = 0.0
        guard._tick()
        check("没在跑命令时 Edge 在前台也不抢", not fake.zorder_calls)
    finally:
        wg.wp._user32, wg.wp.windows_of = orig_user32, orig_windows
        wg.wp.debug_browser_alive = orig_alive


# ---------------------------------------------------------------- 假 guard / 假 window_pos
class FakeGuard:
    def __init__(self):
        self.begins = 0
        self.ends = 0
        self.pauses = 0
        self.resumes = 0
        self.pids = {4321}

    def begin(self):
        self.begins += 1

    def end(self):
        self.ends += 1

    def pause(self, _reason=""):
        self.pauses += 1

    def resume(self):
        self.resumes += 1

    def known_pids(self):
        return set(self.pids)

    def is_alive(self):
        return True


class FakeProc:
    def __init__(self):
        self.calls = []
        self.stdin = self
        self.pid = 4242

    def poll(self):
        return None

    def write(self, s):
        self.calls.append(s)

    def flush(self):
        return None


class FakeWindowPos:
    SCREEN_POS = (80, 80)
    HWND_BOTTOM = wp.HWND_BOTTOM

    def __init__(self):
        self.calls = []

    def hide(self, pids=None):
        self.calls.append(("hide", tuple(sorted(pids or ()))))
        return True

    def show(self, pids=None, pos=(80, 80)):
        self.calls.append(("show", tuple(sorted(pids or ())), pos))
        return True

    def debug_edge_pids(self):
        return {4321}

    def debug_browser_alive(self, port=9222, timeout=0.8):
        return getattr(self, "alive", True)


def test_runner():
    runner = tr.SessionRunner()
    fake_guard = FakeGuard()
    runner._guard = fake_guard

    # 命令区间开关
    runner._guard_begin()
    runner._guard_end()
    check("send 的守卫区间能开能关", (fake_guard.begins, fake_guard.ends) == (1, 1))

    # send 真的会包区间
    runner._proc = FakeProc()
    runner._responses.put({"status": "ok"})
    fake_guard.begins = fake_guard.ends = 0
    resp = runner.send({"cmd": "ping"})
    check("send 全程带着守卫区间", (fake_guard.begins, fake_guard.ends) == (1, 1))
    check("send 返回正常", resp.get("status") == "ok", str(resp))

    # hide / show 用守卫缓存的 pid，避免每次起 powershell
    orig_wp = tr._window_pos
    fake_wp = FakeWindowPos()
    tr._window_pos = fake_wp
    try:
        check("hide_browser 把窗口藏起来", runner.hide_browser())
        check("hide 用的是守卫的 pid", fake_wp.calls[-1] == ("hide", (4321,)), str(fake_wp.calls))
        check("show_browser 把窗口移回屏幕内", runner.show_browser())
        check("show 传到屏幕坐标", fake_wp.calls[-1][2] == (80, 80), str(fake_wp.calls))

        # 等人工验证：先让守卫退场 + 显示窗口，结束后藏回去 + 恢复守卫
        order = []
        runner.show_browser = lambda *a, **k: order.append("show") or True
        runner.hide_browser = lambda *a, **k: order.append("hide") or True
        runner._wait_verification_loop = lambda deadline, poll_s: order.append("wait") or False
        ok = runner.wait_verification(max_s=0)
        check("wait_verification 超时返回 False", ok is False)
        check("等人工时先显示窗口再等", order[:2] == ["show", "wait"], str(order))
        check("等完把窗口藏回去", order[-1] == "hide", str(order))
        check("等人工时守卫暂停一次", fake_guard.pauses == 1, str(fake_guard.pauses))
        check("等完守卫恢复一次", fake_guard.resumes == 1, str(fake_guard.resumes))

        # 浏览器被手动关掉：status 里要看得出来（否则用户点了加购才看到报错）
        fake_wp.alive = False
        runner2 = tr.SessionRunner()
        runner2._proc = FakeProc()
        st = runner2.status(auto_probe=False)
        check("浏览器关了 → status.browser_gone", st.get("browser_gone") is True, str(st.get("browser_gone")))
        fake_wp.alive = True
        st = runner2.status(auto_probe=False)
        check("浏览器回来 → browser_gone 复位", st.get("browser_gone") is False, str(st.get("browser_gone")))

        # 僵尸会话（浏览器关了、进程还在）不许跑批：否则每行都报「搜索词没有可用结果」
        runner3 = tr.SessionRunner()
        runner3._proc = FakeProc()
        runner3._browser_gone = True
        try:
            runner3.start_batch([{"line_no": 1}], resume=False)
            check("浏览器关了 → 批量被拦住", False, "没有抛错")
        except tr.SessionError as exc:
            check("浏览器关了 → 批量被拦住", exc.code == "BROWSER_GONE", exc.code)
    finally:
        tr._window_pos = orig_wp

    # 会话进程还活着但浏览器没了：点「启动」要真的重建，而不是直接 return
    src_start = inspect.getsource(tr.SessionRunner.start)
    check("僵尸会话点启动会先停再起", "self.stop(timeout=5)" in src_start and "_browser_unreachable" in src_start)

    # 契约：_run_batch 开跑就藏窗口（源码级，避免真跑一轮）
    src = inspect.getsource(tr.SessionRunner._run_batch)
    check("_run_batch 第一件事是藏窗口", "self.hide_browser()" in src)
    check("_run_batch 藏窗口在循环之前", src.index("self.hide_browser()") < src.index("for i, line in enumerate(lines)"))

    src_send = inspect.getsource(tr.SessionRunner.send)
    check("send 里包了 _guard_begin", "_guard_begin()" in src_send)
    check("send 里包了 _guard_end", "_guard_end()" in src_send)

    src_stop = inspect.getsource(tr.SessionRunner._reset_state)
    check("会话停止/重启会收掉守卫", "_stop_guard()" in src_stop)


def test_browser_gone_no_spawn():
    """浏览器关掉后不许再查进程：每查一次就多一个控制台窗口（用户看到的黑框）。"""
    calls = {"pids": 0, "windows": 0}

    orig = (wg.wp.debug_browser_alive, wg.wp.debug_edge_pids_for, wg.wp.windows_of,
            wg.BROWSER_GONE_BACKOFF_S, wg.NO_WINDOW_BACKOFF_S)
    wg.wp.debug_browser_alive = lambda port=9222, timeout=0.8: False
    wg.wp.debug_edge_pids_for = lambda p: calls.__setitem__("pids", calls["pids"] + 1) or {1}
    wg.wp.windows_of = lambda pids: calls.__setitem__("windows", calls["windows"] + 1) or {1: (0, 0, 10, 10)}
    try:
        g = wg.FocusGuard(pids=set(), tail_s=0.0)
        for _ in range(20):  # 4 秒 × 5 次/秒
            g.edge_windows()
        check("浏览器关了：不查进程、不枚举窗口", calls == {"pids": 0, "windows": 0}, str(calls))
        check("浏览器关了：退避已生效", g._next_lookup > 0)
        check("浏览器关了：标记为 gone", g._browser_gone)

        # 端口回来以后能自己恢复
        wg.wp.debug_browser_alive = lambda port=9222, timeout=0.8: True
        g._next_lookup = 0.0
        wins = g.edge_windows()
        check("浏览器回来：重新枚举窗口", calls["windows"] == 1 and 1 in wins, str(calls))
        check("浏览器回来：清掉 gone 标记", not g._browser_gone)

        # 端口活着但没窗口（只有后台页）：也要退避，别每轮刷
        g2 = wg.FocusGuard(pids={1}, tail_s=0.0)
        wg.wp.windows_of = lambda pids: {}
        g2.edge_windows()
        first = g2._next_lookup
        g2.edge_windows()
        check("没窗口时也退避", first > 0 and g2._next_lookup == first, f"{first} {g2._next_lookup}")
    finally:
        (wg.wp.debug_browser_alive, wg.wp.debug_edge_pids_for, wg.wp.windows_of,
         wg.BROWSER_GONE_BACKOFF_S, wg.NO_WINDOW_BACKOFF_S) = orig


def test_no_console_flag():
    """所有会被站点拉起来的子进程都必须带 CREATE_NO_WINDOW（否则一闪一个黑框）。"""
    src = inspect.getsource(wp._query_edge_pids)
    check("查 pid 的 powershell 不建控制台", "CREATE_NO_WINDOW" in src)
    check("查 pid 有 TTL 缓存，不会连续起进程", "_PID_TTL" in src and "_PID_CACHE" in src)

    src_runner = inspect.getsource(tr.SessionRunner._move_debug_window)
    check("window_pos 子进程不建控制台", "CREATE_NO_WINDOW" in src_runner)
    src_session = inspect.getsource(tr.SessionRunner.start)
    check("会话子进程不建控制台", "CREATE_NO_WINDOW" in src_session)
    src_guard_pids = inspect.getsource(tr.SessionRunner._guard_pids)
    check("查 pid 前先探端口（浏览器关了就不查）", "debug_browser_alive" in src_guard_pids)


def test_alive_probe():
    """debug_browser_alive：端口没人听 → False；有谁听 → True。端口要真连。"""
    import http.server
    import socket
    import threading

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    check("端口没开 → False", wp.debug_browser_alive(port, timeout=0.5) is False)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        check("端口开着 → True", wp.debug_browser_alive(port, timeout=1.0) is True)
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    print("== 纯判断 ==")
    test_pure()
    print("== 守卫线程 ==")
    test_guard_tick()
    print("== 浏览器关闭后不许起子进程 ==")
    test_browser_gone_no_spawn()
    print("== 不建控制台窗口 ==")
    test_no_console_flag()
    print("== 端口探活 ==")
    test_alive_probe()
    print("== 站点执行层 ==")
    test_runner()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())



