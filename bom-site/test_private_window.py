#!/usr/bin/env python3
"""无痕跑批测试：CDP 模式下用独立 incognito context 做工作区。

要守的几条：
- 工作 context 用 browser.new_context() 新建（CDP 下即无痕，历史不落盘），
  而不是直接用带磁盘 profile 的默认 context；
- 登录 cookie 从默认 profile 注入，退出时回写默认 context + 快照，
  保证下次启动不用重新扫码；
- 默认 context（用户的登录态载体）在 close 时绝不 close；
- 任何异常都回退默认 context，隐私增强不能变成跑批故障。

用假 Playwright 对象覆盖 open()/close()/capture_session()，不碰真浏览器。
"""

import os
import pathlib
import sys

BASE = pathlib.Path(__file__).parent
SKILL = pathlib.Path(os.environ.get("TAOBAO_SKILL_DIR") or (BASE.parent / "Taobao-Search-Skill" / "scripts"))
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(SKILL))

import browser_adapter as BA  # noqa: E402

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


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url

    def is_closed(self):
        return False


class FakeContext:
    def __init__(self, cookies=None, pages=None, storage_state=None, fail_add=False):
        self._cookies = cookies or []
        self.pages = pages if pages is not None else [FakePage()]
        self.closed = False
        self.added = []
        self.timeout = None
        self._storage = storage_state
        self.fail_add = fail_add

    def storage_state(self):
        if self._storage is not None:
            return self._storage
        return {"cookies": [dict(c) for c in self._cookies], "origins": []}

    def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    def set_default_timeout(self, ms):
        self.timeout = ms

    def cookies(self):
        return [dict(c) for c in self._cookies]

    def add_cookies(self, cookies):
        if self.fail_add:
            raise RuntimeError("add_cookies boom")
        self.added.extend(cookies)
        self._cookies.extend(cookies)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, default_ctx, private_ctx=None, new_ctx_raises=False):
        self.contexts = [default_ctx]
        self._private_ctx = private_ctx
        self._new_ctx_raises = new_ctx_raises
        self.new_context_calls = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        if self._new_ctx_raises:
            raise RuntimeError("new_context boom")
        return self._private_ctx


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def connect_over_cdp(self, url):
        return self._browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


def make_adapter(default_ctx, private_ctx=None, new_ctx_raises=False, private_flag=True):
    browser = FakeBrowser(default_ctx, private_ctx, new_ctx_raises)
    pw = FakePlaywright(browser)
    adapter = BA.BrowserAdapter()
    old_sp = BA.sync_playwright
    old_pw = BA.PRIVATE_WINDOW
    BA.sync_playwright = lambda: pw
    BA.PRIVATE_WINDOW = private_flag
    adapter._fake = {"pw": pw, "browser": browser, "old_sp": old_sp, "old_pw": old_pw}
    return adapter


def restore(adapter):
    BA.sync_playwright = adapter._fake["old_sp"]
    BA.PRIVATE_WINDOW = adapter._fake["old_pw"]


LOGIN_COOKIES = [
    {"name": "cookie2", "value": "c", "domain": ".taobao.com", "path": "/", "httpOnly": True},
    {"name": "_tb_token_", "value": "t", "domain": ".taobao.com", "path": "/", "httpOnly": True},
    {"name": "unb", "value": "u", "domain": ".taobao.com", "path": "/", "expires": -1},
]


def main():
    old_cdp = os.environ.get("TAOBAO_CDP_URL")
    os.environ["TAOBAO_CDP_URL"] = "http://127.0.0.1:9222"
    try:
        # 1. 正常：建无痕 context、注入 cookie、工作 page 在无痕里
        default_ctx = FakeContext(cookies=LOGIN_COOKIES)
        private_ctx = FakeContext(cookies=[dict(c) for c in LOGIN_COOKIES])
        a = make_adapter(default_ctx, private_ctx)
        a.open()
        check("CDP 下调用了 new_context（无痕工作区）", len(a._fake["browser"].new_context_calls) == 1)
        call = a._fake["browser"].new_context_calls[0]
        check("new_context 注入了默认 profile 的 storage_state",
              len((call.get("storage_state") or {}).get("cookies") or []) == 3)
        check("工作 context 是新建的无痕 context", a._context is private_ctx)
        check("工作 page 属于无痕 context", a._page in private_ctx.pages)
        check("标记 _private=True", a._private is True)
        check("工作 context 设了超时", private_ctx.timeout == 15000)
        restore(a)

        # 2. close 只关无痕 context，默认 context 不动
        default_ctx = FakeContext(cookies=LOGIN_COOKIES)
        private_ctx = FakeContext(cookies=[dict(c) for c in LOGIN_COOKIES])
        a = make_adapter(default_ctx, private_ctx)
        a.open()
        a.close()
        check("close 关掉无痕 context", private_ctx.closed is True)
        check("close 不碰默认 context", default_ctx.closed is False)
        check("close 断开 playwright 连接", a._fake["pw"].stopped is True)
        check("close 后 _cdp 复位", a._cdp is False and a._private is False)
        restore(a)

        # 3. capture_session 把新鲜 cookie 回写默认 context
        # 默认 profile 里得有 unb 才会开无痕窗口（没登录态时故意退回默认窗口，
        # 否则登录页会跑到第二个窗口里，新用户找不到入口 —— 见
        # test_session_startup.py）。这里给默认 profile 一份完整登录态。
        default_ctx = FakeContext(cookies=[dict(c) for c in LOGIN_COOKIES])
        private_ctx = FakeContext(cookies=LOGIN_COOKIES)
        a = make_adapter(default_ctx, private_ctx)
        a.open()
        snap = a.capture_session()
        names = {c["name"] for c in snap.storage_state["cookies"]}
        check("快照含关键登录 cookie", {"cookie2", "_tb_token_", "unb"} <= names)
        check("会话 cookie 被补了过期时间（重启能带回）",
              all(c.get("expires", 1) > 0 for c in snap.storage_state["cookies"]))
        added_names = {c["name"] for c in default_ctx.added}
        check("新鲜 cookie 回写默认 context（下次不用重新扫码）",
              {"cookie2", "_tb_token_", "unb"} <= added_names)
        restore(a)

        # 4. 回写异常不影响主流程
        default_ctx = FakeContext(cookies=[dict(LOGIN_COOKIES[-1])], fail_add=True)
        private_ctx = FakeContext(cookies=LOGIN_COOKIES)
        a = make_adapter(default_ctx, private_ctx)
        a.open()
        try:
            snap = a.capture_session()
            check("cookie 回写异常被吞掉、快照仍返回", len(snap.storage_state["cookies"]) == 3)
        except Exception as exc:  # noqa: BLE001
            check("cookie 回写异常被吞掉、快照仍返回", False, repr(exc))
        restore(a)

        # 5. new_context 失败 → 回退默认 context，不抛
        default_ctx = FakeContext(cookies=LOGIN_COOKIES, pages=[FakePage()])
        a = make_adapter(default_ctx, None, new_ctx_raises=True)
        a.open()
        check("无痕创建失败回退默认 context", a._context is default_ctx and a._private is False)
        check("回退后工作 page 在默认 context", a._page is not None)
        restore(a)

        # 6. 开关关掉 → 直接用默认 context
        default_ctx = FakeContext(cookies=LOGIN_COOKIES)
        a = make_adapter(default_ctx, None, private_flag=False)
        a.open()
        check("TAOBAO_PRIVATE_WINDOW=0 时不建无痕 context",
              len(a._fake["browser"].new_context_calls) == 0)
        check("开关关掉时用默认 context", a._context is default_ctx and a._private is False)
        restore(a)

        # 7. 源码契约：历史不落盘靠 new_context；默认 context 不被 close
        src = (SKILL / "browser_adapter.py").read_text(encoding="utf-8")
        check("PRIVATE_WINDOW 开关有默认值且可配",
              'TAOBAO_PRIVATE_WINDOW' in src and 'PRIVATE_WINDOW =' in src)
        check("无痕默认关闭（单窗口优先）",
              'os.environ.get("TAOBAO_PRIVATE_WINDOW") or "0"' in src)
        check("私有 context 用 new_context 创建",
              'self._browser.new_context(' in src and 'storage_state=state' in src)
        check("close 只关私有 context", 'if self._private and self._context is not None' in src)
        check("cookie 回写给默认 context", 'self._cdp_default_ctx.add_cookies(cookies)' in src)
    finally:
        if old_cdp is None:
            os.environ.pop("TAOBAO_CDP_URL", None)
        else:
            os.environ["TAOBAO_CDP_URL"] = old_cdp

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
