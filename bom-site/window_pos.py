# -*- coding: utf-8 -*-
"""控制「调试版 Edge」（remote-debugging-port=9222）的窗口位置与 Z 序。

为什么要这个模块：淘宝自动化是 CDP 驱动一个**用户自己的浏览器窗口**，Chromium
在某些输入/聚焦路径上会把窗口提到前台，于是挑商品的时候浏览器会盖住你正在用的
软件。解决思路是「平时藏起来 + 抢了就压回去」，全程不激活、不抢焦点：

* ``hide()``：窗口移到屏幕外 (-2000, 0)。**不最小化** —— 最小化的窗口 Chromium
  会停渲染，截图会变白；移到屏幕外渲染照常，截图/CDP 都正常。
  挪完还要**复核**：Chromium 的「还原页面」气泡依附主窗口，主窗口一离开桌面，
  它会被夹回屏幕左上角（实测 x 从 -2000 变成 0），屏幕边上就露一条。这种
  挪不动的气泡直接 ``SW_HIDE``（不是页面，藏掉不影响截图），``show()`` 时放回来。
* ``show()``：移回屏幕内（需要人工点图时），用 SW_SHOWNOACTIVATE，不抢前台。
* 移动走 ``GetWindowPlacement`` / ``SetWindowPlacement``：``MoveWindow`` 对最大化
  的窗口是无效的（很多人习惯把浏览器最大化，那样藏了个寂寞），而 SetWindowPlacement
  能在不激活窗口的前提下改到位。

CLI：

    python window_pos.py               # 藏到屏幕外
    python window_pos.py hide          # 同上
    python window_pos.py show          # 移回屏幕内 (80, 80)
    python window_pos.py 300 200       # 移到指定坐标
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes

MARK = "remote-debugging-port=9222"
OFFSCREEN = (-2000, 0)
SCREEN_POS = (80, 80)
SIZE = (1500, 1000)

# 子进程一律不建控制台窗口：站点是 cmd 拉起来的无窗口进程，子进程（powershell）
# 默认会**新开一个控制台**——表现就是「命令窗口一闪一闪」。以前关掉浏览器后守卫
# 每轮都查一次进程，用户看到的就是不停地弹黑框。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 查 pid 要起 powershell（1~1.5s），绝不能高频调用：1.5s 内复用上次结果
_PID_CACHE: dict[int, tuple[float, set[int]]] = {}
_PID_TTL = 1.5

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWMAXIMIZED = 3
SW_SHOWMINIMIZED = 2
HWND_BOTTOM = 1
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

# hide 之前的原始摆放（尺寸 / 是否最大化），show 时原样还回去
_SAVED: dict[int, "WINDOWPLACEMENT"] = {}
# 挪不走、只好 SW_HIDE 的窗口（Chromium 的「还原页面」气泡会依附父窗口被夹回屏幕
# 左上角）。记下句柄，show 时用 SW_SHOWNOACTIVATE 放出来。
_HIDDEN: dict[int, int] = {}


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


def _user32():
    u = ctypes.windll.user32
    if not getattr(u, "_dsh_protos", False):  # 只设一次 argtypes，避免句柄被截成 32 位
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetForegroundWindow.argtypes = []
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.BringWindowToTop.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.AttachThreadInput.restype = wintypes.BOOL
        u.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
        u.GetWindowPlacement.restype = wintypes.BOOL
        u.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
        u.SetWindowPlacement.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        u.SetWindowPos.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u._dsh_protos = True
    return u


def _query_edge_pids(port: int) -> set[int]:
    """起一次 powershell 查 pid（静默、带 TTL 缓存）。"""
    now = time.monotonic()
    hit = _PID_CACHE.get(port)
    if hit and now - hit[0] < _PID_TTL:
        return set(hit[1])
    mark = f"remote-debugging-port={int(port)}"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             f"Where-Object {{ $_.CommandLine -match '{mark}' }}).ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        out = ""
    pids = {int(x) for x in out.split() if x.strip().isdigit()}
    _PID_CACHE[port] = (now, pids)
    return pids


def debug_edge_pids() -> set[int]:
    """调试版 Edge 的进程号（命令行里带 remote-debugging-port=9222）。"""
    return _query_edge_pids(9222)


def debug_edge_pids_for(port: int) -> set[int]:
    return _query_edge_pids(port)


def close(pids: set[int] | None = None, force: bool = True) -> bool:
    """结束调试版 Edge。

    只杀命令行带 remote-debugging-port 的进程树（taskkill /T 连子进程一起），
    用户自己开的普通 Edge 不受影响。UI 的「结束会话」就靠这个兑现
    「会关闭淘宝浏览器窗口」的承诺。
    """
    pids = debug_edge_pids() if pids is None else pids
    if not pids:
        return False
    killed = False
    for pid in pids:
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.append("/F")
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15)
            killed = killed or r.returncode == 0
        except Exception:
            pass
    _PID_CACHE.clear()  # 进程号全变了，别让缓存骗到下一次判断
    _SAVED.clear()
    _HIDDEN.clear()
    return killed


def debug_browser_alive(port: int = 9222, timeout: float = 0.8) -> bool:
    """调试浏览器还在不在（HTTP 探一下 CDP 端口，纯 socket，不起进程、不弹窗）。

    关掉浏览器后没必要再去 powershell 查进程：先问这个，端口都死了就直接返回。

    必须绕开系统代理：这台机器上 HTTP_PROXY 指向本地代理，127.0.0.1 的请求
    被丢给代理后一律 502，会把「浏览器好好的」误判成「已关闭」，整批被中止。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout) as resp:
            return getattr(resp, "status", 200) == 200
    except Exception:
        return False


def windows_of(pids: set[int]) -> dict[int, tuple[int, int, int, int]]:
    """pids 对应的可见顶层窗口 → rect。"""
    if not pids:
        return {}
    u = _user32()
    found: dict[int, tuple[int, int, int, int]] = {}
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            r = RECT()
            if u.GetWindowRect(hwnd, ctypes.byref(r)):
                found[int(hwnd)] = (r.left, r.top, r.right, r.bottom)
        return True

    u.EnumWindows(CB(cb), 0)
    return found


def _placement(hwnd: int) -> WINDOWPLACEMENT | None:
    u = _user32()
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not u.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
        return None
    return wp


def remember(hwnd: int) -> None:
    """藏起来之前先记住窗口原来的大小/是否最大化，show 的时候原样还回去。

    已经是屏幕外的窗口不记 —— 否则站点重启后（内存里的记录没了）会把「屏幕外」
    当成用户的原始位置记下来，show 就还到屏幕外去了。
    """
    if hwnd in _SAVED:
        return
    r = RECT()
    if _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        if is_offscreen((r.left, r.top, r.right, r.bottom)):
            return
    wp = _placement(hwnd)
    if wp is not None:
        _SAVED[hwnd] = wp


def _ensure_movable(hwnd: int) -> tuple[int, int, int, int]:
    """把最大化的窗口按「不激活」的方式还原成普通窗口，返回它现在的 rect。

    ``SetWindowPos`` 对最大化的窗口是无效的（位置不会变），所以要先退掉最大化。
    用 ``SetWindowPlacement`` + ``SW_SHOWNOACTIVATE`` 而不是 ``ShowWindow(SW_RESTORE)``：
    后者会把窗口拉到前台，正是我们要避免的。
    """
    u = _user32()
    h = wintypes.HWND(hwnd)
    r = RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    if u.IsIconic(h) or u.IsZoomed(h):
        wp = _placement(hwnd)
        if wp is None:
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if u.IsIconic(h):
            saved = _SAVED.get(hwnd)
            if saved is not None:
                wp.rcNormalPosition = RECT(saved.rcNormalPosition.left, saved.rcNormalPosition.top,
                                           saved.rcNormalPosition.right, saved.rcNormalPosition.bottom)
        else:
            wp.rcNormalPosition = RECT(r.left, r.top, r.right, r.bottom)
        wp.showCmd = SW_SHOWNOACTIVATE
        u.SetWindowPlacement(h, ctypes.byref(wp))
        u.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def place(hwnd: int, x: int, y: int, width: int | None = None, height: int | None = None,
          sweep_to_bottom: bool = False) -> bool:
    """把窗口挪到 (x, y)，不激活窗口。

    注意：**必须用 ``SetWindowPos`` 而不是 ``SetWindowPlacement``** —— 后者会被
    Chromium 夹回最近的工作区（实测 (-2000,0) 变成 (0,0)），藏屏幕外就失效了。
    """
    remember(hwnd)
    u = _user32()
    left, top, right, bottom = _ensure_movable(hwnd)
    w = width or max(200, right - left)
    h_ = height or max(200, bottom - top)
    insert_after = HWND_BOTTOM if sweep_to_bottom else HWND_TOP
    return bool(u.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(insert_after), int(x), int(y), int(w), int(h_),
        SWP_NOACTIVATE,
    ))


def restore(hwnd: int) -> bool:
    """把窗口还回 hide 之前的摆放（尺寸和最大化状态都还原），不激活。"""
    u = _user32()
    saved = _SAVED.get(hwnd)
    if saved is None:
        return False
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    wp.flags = saved.flags
    wp.showCmd = saved.showCmd
    wp.ptMinPosition = POINT(saved.ptMinPosition.x, saved.ptMinPosition.y)
    wp.ptMaxPosition = POINT(saved.ptMaxPosition.x, saved.ptMaxPosition.y)
    wp.rcNormalPosition = RECT(saved.rcNormalPosition.left, saved.rcNormalPosition.top,
                               saved.rcNormalPosition.right, saved.rcNormalPosition.bottom)
    ok = bool(u.SetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)))
    u.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOP), 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return ok


def move(x: int, y: int, pids: set[int] | None = None, sweep_to_bottom: bool = False) -> bool:
    pids = debug_edge_pids() if pids is None else pids
    wins = windows_of(pids)
    moved = False
    for hwnd in wins:
        moved = place(hwnd, x, y, sweep_to_bottom=sweep_to_bottom) or moved
    return moved


def virtual_screen() -> tuple[int, int, int, int]:
    """整个桌面（所有显示器）的 (x, y, w, h)。多屏时左边那块可能是负坐标。"""
    u = _user32()
    return (
        u.GetSystemMetrics(76),
        u.GetSystemMetrics(77),
        u.GetSystemMetrics(78) or u.GetSystemMetrics(0) or 1920,
        u.GetSystemMetrics(79) or u.GetSystemMetrics(1) or 1080,
    )


def on_screen(rect: tuple[int, int, int, int], border: int = 2) -> bool:
    """窗口是否还有像素落在桌面里（纯判断，方便单测）。

    留 ``border`` 像素余量：贴着边缘、只差一两像素的窗口也算「露出来了」。
    """
    x, y, w, h = virtual_screen()
    left, top, right, bottom = rect
    return (
        right > x + border
        and left < x + w - border
        and bottom > y + border
        and top < y + h - border
    )


def _is_tool_window(hwnd: int) -> bool:
    """Chromium 的气泡/提示窗（「还原页面」「下载完成」）带 WS_EX_TOOLWINDOW。"""
    try:
        return bool(_user32().GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
    except Exception:  # noqa: BLE001
        return False


def hide(pids: set[int] | None = None) -> bool:
    """藏到屏幕外并压到 Z 序底部（不最小化、不隐藏，渲染继续，截图照常）。

    挪完必须**复核**：Chromium 的「还原页面」气泡是主窗口的附属窗口，主窗口一离开
    桌面，它就被夹回屏幕左上角（实测 x 从 -2000 被夹成 0），结果浏览器藏好了，
    屏幕边上却一直露一条。这种窗口挪不动，直接 ``SW_HIDE``；它不是页面，藏掉
    不影响截图和自动化，``show`` 时再放出来。
    """
    pids = debug_edge_pids() if pids is None else pids
    if not pids:
        return False
    wins = windows_of(pids)
    ok = False
    for hwnd in wins:
        ok = place(hwnd, OFFSCREEN[0], OFFSCREEN[1], sweep_to_bottom=True) or ok

    u = _user32()
    for hwnd, rect in windows_of(pids).items():
        if not on_screen(rect):
            continue
        if not _is_tool_window(hwnd):
            # 主窗口挪不走只可能是被系统/Chromium 夹住：再试一次（改尺寸，避免
            # 被「保持可见区域」的规则弹回），还不行就只能留着，绝不 SW_HIDE
            # 主窗口 —— 隐藏的 Chromium 会停渲染，截图会变白。
            place(hwnd, OFFSCREEN[0], OFFSCREEN[1], width=600, height=400,
                  sweep_to_bottom=True)
            if on_screen(windows_of(pids).get(hwnd, rect)):
                continue
            ok = True
            continue
        _HIDDEN[hwnd] = SW_SHOWMAXIMIZED if u.IsZoomed(hwnd) else SW_SHOWNOACTIVATE
        u.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
        ok = True
    return ok


def show(pids: set[int] | None = None, pos: tuple[int, int] = SCREEN_POS) -> bool:
    """移回屏幕内（需要人工点验证图时），不抢前台。藏之前的位置记得住就还回去。"""
    pids = debug_edge_pids() if pids is None else pids
    u = _user32()
    # 先把上一轮为「藏干净」而 SW_HIDE 的气泡放出来；页面窗口从来没有被 SW_HIDE。
    for hwnd, cmd in list(_HIDDEN.items()):
        if u.IsWindow(wintypes.HWND(hwnd)):
            u.ShowWindow(wintypes.HWND(hwnd), cmd)
        _HIDDEN.pop(hwnd, None)
    wins = windows_of(pids)
    shown = False
    for hwnd in wins:
        shown = restore(hwnd) or place(hwnd, pos[0], pos[1]) or shown
    return shown


def is_offscreen(rect: tuple[int, int, int, int], screen_w: int = 0) -> bool:
    """窗口是不是已经被藏到桌面外（纯判断，给测试用）。

    不传 ``screen_w`` 时按整个虚拟桌面判断（多显示器时右边/下边也要算），
    传了就沿用老的「主屏左边」语义，方便单测不受机器分辨率影响。
    """
    if screen_w > 0:
        left, _, right, _ = rect
        return right <= 0 or left <= -(screen_w // 2)
    x, y, w, h = virtual_screen()
    left, top, right, bottom = rect
    return right <= x or left >= x + w or bottom <= y or top >= y + h


def _main(argv: list[str]) -> int:
    arg = (argv[1] if len(argv) > 1 else "hide").lower()
    if arg == "hide":
        print("hidden" if hide() else "window not found")
        return 0
    if arg == "show":
        print("shown" if show() else "window not found")
        return 0
    if len(argv) >= 3:
        try:
            x, y = int(argv[1]), int(argv[2])
        except ValueError:
            print("坐标必须是整数：window_pos.py 300 200")
            return 2
        print("moved" if move(x, y) else "window not found")
        return 0
    print("hidden" if hide() else "window not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
