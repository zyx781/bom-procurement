#!/usr/bin/env python3
"""启动阶段的三个新用户坑：卡住不报、假登录、藏错窗口。

要守的几条（都是 2026-11-19 真人复现出来的）：
1. 会话进程活着但一直不给 ready / waiting_login，界面不能永远显示「启动中」，
   超时后必须报「启动卡住了」并给出可执行的下一步；
2. 游客（cookie2 + _tb_token_）不能被判成已登录——真实登录证据只有 unb；
3. 没确认登录成功之前，绝不把浏览器窗口藏到屏幕外；
4. 浏览器里还没有登录态时不开无痕窗口，否则登录页跑到第二个窗口里，
   新用户盯着第一个窗口找不到入口。

全部用假会话进程，不碰真浏览器、不写真实 runs。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

BASE = pathlib.Path(__file__).parent
SKILL = pathlib.Path(os.environ.get("TAOBAO_SKILL_DIR") or (BASE.parent / "Taobao-Search-Skill" / "scripts"))
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(SKILL))

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


# ---- 隔离：不拉真浏览器、不写真实 runs、不起前台守卫 ----
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bom_startup_test_"))
tr.RUNS = tmp / "runs"
tr.RUNS.mkdir(parents=True, exist_ok=True)
tr._window_pos = None
tr._focus_guard = None
tr.cdp_endpoint_ok = lambda url, timeout=1.2: True
tr.ensure_debug_browser = lambda *a, **k: None
os.environ["BOM_STARTUP_TIMEOUT"] = "1.2"

SCRIPTS = {
    "silent": """
import sys, time
for line in sys.stdin:
    if "quit" in line:
        break
time.sleep(0.1)
""",
    "login_wait": """
import sys, time
print("[browser] waiting for human takeover or approved login flow", file=sys.stderr, flush=True)
for line in sys.stdin:
    if "quit" in line:
        break
""",
    "ready_logged_in": """
import sys
print("[browser] check login status => logged in", file=sys.stderr, flush=True)
print("[taobao] session ready, waiting for commands", file=sys.stderr, flush=True)
for line in sys.stdin:
    if "quit" in line:
        break
""",
    "ready_login_failed": """
import sys
print("[browser] check login status => not logged in", file=sys.stderr, flush=True)
print("[taobao] login failed or timed out", file=sys.stderr, flush=True)
print("[taobao] session ready, waiting for commands", file=sys.stderr, flush=True)
for line in sys.stdin:
    if "quit" in line:
        break
""",
}

paths = {}
for name, body in SCRIPTS.items():
    p = tmp / f"fake_{name}.py"
    p.write_text(body, encoding="utf-8")
    paths[name] = p


def make_runner(name):
    runner = tr.SessionRunner(
        command=[sys.executable, str(paths[name])],
        repo=BASE,
        artifacts=tmp / "artifacts",
    )
    hidden = []
    runner.hide_browser = lambda *a, **k: hidden.append(time.monotonic()) or True  # type: ignore[assignment]
    return runner, hidden


def wait_for(pred, timeout=6.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


print("== 1. 一直不出声：超时后必须报「卡住了」 ==")
runner, hidden = make_runner("silent")
try:
    st = runner.start()
    check("刚拉起来不算卡住", st["stalled"] is False, st.get("stalled"))
    check("等待登录前是 running 未就绪", st["running"] and not st["ready"])
    ok = wait_for(lambda: runner.status()["stalled"], timeout=6.0)
    check("1.2 秒后判定为卡住", ok)
    st = runner.status()
    check("卡住时仍报 running（进程还活着）", st["running"] is True)
    check("卡住时不冒充 ready", st["ready"] is False)
    log_text = "\n".join(st["logs"])
    check("日志里有「启动卡住了」", "启动卡住了" in log_text, log_text[-200:])
    check("日志里有可执行的建议（结束会话 / 杀毒软件拦截）",
          "结束会话" in log_text and "拦截" in log_text, log_text[-200:])
    # 重复查询不能刷屏：同一次卡住只记一条
    for _ in range(3):
        runner.status()
    again = "\n".join(runner.status()["logs"])
    check("卡住提示只记一次", again.count("启动卡住了") == 1, again.count("启动卡住了"))
    check("端口状态不影响判定", st["stalled"] is True)
finally:
    runner.stop(timeout=3)

print("\n== 2. 进入等待登录：不算卡住 ==")
runner, hidden = make_runner("login_wait")
try:
    runner.start()
    ok = wait_for(lambda: runner.status()["waiting_login"], timeout=5.0)
    check("识别到 waiting_login", ok)
    time.sleep(1.6)  # 超过 BOM_STARTUP_TIMEOUT
    st = runner.status()
    check("等待登录期间不报卡住", st["stalled"] is False, st)
    check("等待登录期间不藏窗口", hidden == [], hidden)
finally:
    runner.stop(timeout=3)

print("\n== 3. 已登录：就绪后自动藏窗口 ==")
runner, hidden = make_runner("ready_logged_in")
try:
    runner.start()
    ok = wait_for(lambda: runner.status()["ready"], timeout=5.0)
    check("会话就绪", ok)
    st = runner.status()
    check("login_state 识别为 ok", st["login_state"] == "ok", st["login_state"])
    check("就绪后触发隐藏", wait_for(lambda: bool(hidden), timeout=3.0), hidden)
    check("就绪后不报卡住", st["stalled"] is False)
finally:
    runner.stop(timeout=3)

print("\n== 4. 就绪但没登录：绝不藏窗口 ==")
runner, hidden = make_runner("ready_login_failed")
try:
    runner.start()
    ok = wait_for(lambda: runner.status()["ready"], timeout=5.0)
    check("会话就绪", ok)
    st = runner.status()
    check("login_state 是 failed", st["login_state"] == "failed", st["login_state"])
    time.sleep(1.0)
    check("没登录成功就不藏窗口（否则人还没扫码浏览器就没了）", hidden == [], hidden)
    check("就绪之后不再报卡住", st["stalled"] is False)
finally:
    runner.stop(timeout=3)

print("\n== 5. 浏览器里没有登录态：不开无痕窗口 ==")
try:
    import browser_adapter as BA
    import test_private_window as tpw

    old_env = os.environ.get("TAOBAO_CDP_URL")
    os.environ["TAOBAO_CDP_URL"] = "http://127.0.0.1:9222"
    try:
        guest = [
            {"name": "cookie2", "value": "c", "domain": ".taobao.com", "path": "/"},
            {"name": "_tb_token_", "value": "t", "domain": ".taobao.com", "path": "/"},
        ]
        default_ctx = tpw.FakeContext(cookies=guest)
        a = tpw.make_adapter(default_ctx, tpw.FakeContext(cookies=guest))
        try:
            a.open()
            check("游客不建新 context（登录页不会跑到第二个窗口）",
                  a._fake["browser"].new_context_calls == [])
            check("直接用默认 context", a._context is default_ctx)
            check("不标记无痕", a._private is False)
        finally:
            tpw.restore(a)

        # 有登录态时照旧用无痕窗口（隐私增强不能回退）
        logged = guest + [{"name": "unb", "value": "123", "domain": ".taobao.com", "path": "/"}]
        default_ctx = tpw.FakeContext(cookies=logged)
        private_ctx = tpw.FakeContext(cookies=logged)
        a = tpw.make_adapter(default_ctx, private_ctx)
        try:
            a.open()
            check("已登录仍用无痕工作区", len(a._fake["browser"].new_context_calls) == 1)
            check("工作 page 在无痕 context", a._context is private_ctx)
        finally:
            tpw.restore(a)
    finally:
        if old_env is None:
            os.environ.pop("TAOBAO_CDP_URL", None)
        else:
            os.environ["TAOBAO_CDP_URL"] = old_env

    print("\n== 6. 游客 cookie 不能算登录 ==")

    class FakeLocator:
        @property
        def first(self):
            return self

        def is_visible(self, timeout=None):
            return False

    class FakeCtx:
        def __init__(self, cookies):
            self._c = cookies

        def cookies(self):
            return list(self._c)

    class FakePage:
        def __init__(self, url, cookies):
            self.url = url
            self.context = FakeCtx(cookies)

        def locator(self, _sel):
            return FakeLocator()

    adapter = BA.BrowserAdapter()
    guest_page = FakePage("https://www.taobao.com/", guest)
    check("游客（cookie2 + _tb_token_）判定未登录", adapter._looks_logged_in(guest_page) is False)

    logged_page = FakePage("https://www.taobao.com/", logged)
    check("有 unb 判定已登录", adapter._looks_logged_in(logged_page) is True)

    login_url_page = FakePage("https://login.taobao.com/havanaone/login/login.htm", guest)
    check("登录页上的游客不算已登录", adapter._looks_logged_in(login_url_page) is False)

    empty_page = FakePage("https://www.taobao.com/", [])
    check("没有 cookie 判定未登录", adapter._looks_logged_in(empty_page) is False)
except Exception as exc:  # noqa: BLE001
    check("无痕/登录判定用例未抛异常", False, repr(exc))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
