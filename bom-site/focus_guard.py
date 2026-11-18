"""前台守卫：自动化跑批时，别让被驱动的 Edge 窗口盖住用户正在用的软件。

背景：执行层是用 CDP 驱动**用户自己的** Edge（不是 Playwright 拉起来的），
Chromium 在部分输入/聚焦路径上会 ``SetForegroundWindow``，于是「挑商品」的时候
浏览器窗口反复跳到你正在看的软件上面。用户明确反馈过这个问题。

这个模块不去改执行层（那里每次操作都可能触发），而是在 OS 层兜住：

* 只在**自动化正在持行命令**时生效（``begin/end``），命令结束留 ``tail_s`` 尾巴；
  平时用户自己点这个浏览器，守卫完全不管，不会跟用户抢。
* 一旦发现前台窗口是调试版 Edge 的顶层窗口：把它压到 Z 序底部（不激活），
  再把焦点还给**上一个非 Edge 的前台窗口**（也就是用户本来在用的软件）。
* ``pause()``：等人工过验证时关掉守卫 —— 那时候就是要让用户点浏览器。

所有 Win32 调用都在 ``_tick`` 里包了异常，守卫线程自己挂了也绝不拖垮跑批。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import window_pos as wp

DEFAULT_INTERVAL_S = 0.2
DEFAULT_TAIL_S = 2.0
# 浏览器关了 / 端口不通：最长这么久才再探一次（探的是 HTTP，不起进程）
BROWSER_GONE_BACKOFF_S = 15.0
# 端口活着但找不到窗口（只有后台页 / 刚启动）：这么久再查一次进程
NO_WINDOW_BACKOFF_S = 8.0


def should_restore(fg: int | None, edge_windows, active: bool) -> bool:
    """要不要把焦点从 Edge 抢回来（纯判断，方便单测）。

    ``fg`` 前台窗口句柄，``edge_windows`` 调试 Edge 的顶层窗口集合，
    ``active`` 自动化是否正在持行命令。
    """
    if not active or not fg:
        return False
    try:
        return int(fg) in {int(h) for h in edge_windows}
    except (TypeError, ValueError):
        return False


def pick_memory(fg: int | None, edge_windows, current: int | None) -> int | None:
    """记下「用户本来在用的那个窗口」：前台不是 Edge 就更新，是 Edge 就保留旧值。"""
    if not fg:
        return current
    try:
        if int(fg) in {int(h) for h in edge_windows}:
            return current
    except (TypeError, ValueError):
        return current
    return int(fg)


class FocusGuard(threading.Thread):
    """后台轮询前台窗口，把被自动化抢走的前台还给用户。"""

    def __init__(self, pids: set[int] | None = None, port: int = 9222,
                 interval: float = DEFAULT_INTERVAL_S, tail_s: float = DEFAULT_TAIL_S,
                 log=None):
        super().__init__(daemon=True, name="focus-guard")
        self._port = int(port)
        self._pids = set(pids or ())
        self._interval = max(0.05, float(interval))
        self._tail_s = max(0.0, float(tail_s))
        self._log = log
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._depth = 0
        self._tail_until = 0.0
        self._paused = False
        self._keep_hidden = False
        self._user_hwnd: int | None = None
        self._wins: dict[int, tuple[int, int, int, int]] = {}
        self._wins_at = 0.0
        self._next_lookup = 0.0  # 退避到期时间：到点之前不查进程、不起子进程
        self._browser_gone = False
        self.restores = 0
        self.rehides = 0
        self.last_error: str | None = None

    # ---------------- 生命周期 ----------------
    def start(self) -> FocusGuard:  # type: ignore[override]
        super().start()
        return self

    def stop_guard(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self, reason: str = "") -> None:
        with self._lock:
            self._paused = True
        if reason and self._log:
            self._log(f"[guard] 暂停（{reason}）")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._wake.set()

    def set_keep_hidden(self, value: bool) -> None:
        """「这个会话要把浏览器藏起来」的意图。

        一火性的 hide 挡不住后出现的窗口：淘宝新开标签、Chromium 的「还原页面」
        气泡、下载提示，都会在隐藏之后才冒出来，落在屏幕边上。带着这个意图，
        守卫每轮发现还有 Edge 窗口在桌面里就再藏一次；用户点「显示」时清掉。
        """
        with self._lock:
            self._keep_hidden = bool(value)
        self._wake.set()

    def keep_hidden(self) -> bool:
        with self._lock:
            return self._keep_hidden

    # ---------------- 命令区间 ----------------
    def begin(self) -> None:
        with self._lock:
            self._depth += 1

    def end(self) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)
            self._tail_until = time.monotonic() + self._tail_s

    def active(self) -> bool:
        with self._lock:
            if self._paused:
                return False
            return self._depth > 0 or time.monotonic() < self._tail_until

    # ---------------- 窗口枚举 ----------------
    def edge_windows(self, refresh_s: float = 2.0) -> dict[int, tuple[int, int, int, int]]:
        """调试版 Edge 的可见顶层窗口。

        浏览器被关掉以后**绝不能**每轮都去查进程：那要起 powershell，每次都会
        新开一个控制台窗口（用户看到的就是「命令窗口一直弹出又关闭」）。所以：
        先 HTTP 探 CDP 端口（纯 socket），端口都死了就退避，什么都不起。
        """
        now = time.monotonic()
        if now < self._next_lookup:  # 退避中：直接用上次结果，不起进程
            return self._wins
        if self._wins and now - self._wins_at < refresh_s:
            return self._wins
        if not wp.debug_browser_alive(self._port):
            if not self._browser_gone:
                self._browser_gone = True
                if self._log:
                    self._log("[guard] 调试浏览器已关闭，守卫转入待机（不起子进程、不弹窗）")
            self._wins = {}
            self._wins_at = now
            self._next_lookup = now + BROWSER_GONE_BACKOFF_S
            return {}
        if self._browser_gone:
            self._browser_gone = False
            if self._log:
                self._log("[guard] 调试浏览器回来了，守卫恢复")
        pids = wp.debug_edge_pids_for(self._port)
        wins = wp.windows_of(pids)
        self._pids = pids
        self._wins = wins
        self._wins_at = now
        if wins:
            self._next_lookup = 0.0
        else:
            # 端口活着但没窗口（浏览器刚起 / 只有后台页面）：慢点再查，别刷屏
            self._next_lookup = now + NO_WINDOW_BACKOFF_S
        return self._wins

    # ---------------- 主循环 ----------------
    def known_pids(self) -> set[int]:
        """当前认定的调试 Edge 进程号（顺带刷新一次缓存）。"""
        self.edge_windows()
        return set(self._pids)

    def _tick(self) -> None:
        u = wp._user32()
        fg = int(u.GetForegroundWindow() or 0)
        wins = self.edge_windows()
        # 「藏干净」：隐藏之后才冒出来的窗口（新标签、还原页面气泡、下载提示）
        # 会落在桌面里，尤其气泡会被 Chromium 夹到屏幕左上角露一条。带意图的
        # 会话里发现一个就整体再藏一次；暂停（等人工过验证）时不动。
        if self.keep_hidden() and not self.paused() and wins:
            if any(wp.on_screen(r) for r in wins.values()):
                wp.hide(self._pids)
                self._wins = wp.windows_of(self._pids)
                self._wins_at = time.monotonic()
                wins = self._wins
                self.rehides += 1
        if should_restore(fg, wins, self.active()):
            self._give_back(fg)
            return
        self._user_hwnd = pick_memory(fg, wins, self._user_hwnd)

    def _give_back(self, edge_hwnd: int) -> None:
        u = wp._user32()
        # 先压 Z 序：即使还给用户失败，至少不再盖住他
        u.SetWindowPos(int(edge_hwnd), wp.HWND_BOTTOM, 0, 0, 0, 0,
                       wp.SWP_NOMOVE | wp.SWP_NOSIZE | wp.SWP_NOACTIVATE)
        target = self._user_hwnd
        if target and u.IsWindow(int(target)):
            force_foreground(target)
        self.restores += 1

    def run(self) -> None:  # pragma: no cover - 线程体，逻辑靠 _tick 单测
        while not self._stop_evt.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop_evt.is_set():
                return
            try:
                # 注意：**不活动的轮次也要跑 _tick** —— 它顺带记住「用户本来在用的
                # 窗口」。以前只在活动时跑，结果第一次抢前台时还不知道该还给谁，
                # 白白让浏览器占着前台（实测 restores=0、前台一直是 Edge）。
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"


def force_foreground(hwnd: int) -> bool:
    """把前台还给 hwnd。

    Windows 有前台锁：非前台进程直接 SetForegroundWindow 常被拒。这里先把本线程
    和当前前台线程 AttachThreadInput 接一下，再设前台（业界通用做法），最后断开。
    失败还有 BringWindowToTop 兜底。
    """
    u = wp._user32()
    k32 = ctypes.windll.kernel32
    h = int(hwnd)
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = k32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg and tid_fg != tid_me:
            attached = bool(u.AttachThreadInput(wintypes.DWORD(tid_me), wintypes.DWORD(tid_fg), True))
        u.BringWindowToTop(h)
        if u.SetForegroundWindow(h):
            return True
        u.BringWindowToTop(h)
        return False
    finally:
        if attached:
            u.AttachThreadInput(wintypes.DWORD(tid_me), wintypes.DWORD(tid_fg), False)
