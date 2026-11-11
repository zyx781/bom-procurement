from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from models import MatchedItem
from taobao_selectors import (
    ACCESS_BLOCKED_SIGNALS, ADD_TO_CART_BUTTONS, CAPTCHA_BG_SELECTORS,
    CAPTCHA_PANEL_SELECTORS, CAPTCHA_SLIDER_BTN_SELECTORS,
    CART_CONFIRM_POPUP, CART_ITEM_SELECTORS, LOGGED_IN_INDICATORS,
    LOGIN_COOKIE_NAMES, LOGIN_PAGE_URL_SIGNALS, MIDDLEWARE_OVERLAY_HIDE_JS,
    NOT_LOGGED_IN_TEXT, POPUP_CLOSE_BUTTONS, PRICE_SELECTORS,
    PRODUCT_CARD_CLIMB_SELECTORS, PRODUCT_LINK_SELECTORS, RATING_SELECTORS,
    SALES_COUNT_SELECTORS, SEARCH_INPUT, SEARCH_SUBMIT,
    SKU_GROUP_LABEL_SELECTORS, SKU_OPTION_SELECTORS, SKU_VALUE_SELECTOR,
)
from session_manager import SessionSnapshot

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_PROFILE_DIR = Path(
    os.environ.get("TAOBAO_PROFILE_DIR")
    or (_SCRIPTS_DIR / ".cache" / "taobao-search-skill" / "browser-profile")
)

# Safety cap for the "+" fallback. More clicks than this means the widget is
# not a normal quantity control; ask the user to set the number manually.
_MAX_QTY_PLUS_CLICKS = 20

# ──────────────────────────────────────────────
# Pace profile (batch speed vs. human-like pacing)
# ──────────────────────────────────────────────
# TAOBAO_SPEED scales every simulated human pause. 1.0 keeps the original
# pace; 0.35 is the "fast batch" profile. Only sleep durations change — the
# click / verification logic is identical, so correctness never depends on it.
SPEED = max(0.05, float(os.environ.get("TAOBAO_SPEED") or 1.0))

# TAOBAO_SHOTS: all | fail
#   all  - screenshot after every step (default, best for one-off runs)
#   fail - only keep screenshots that explain a failure (plus the cart-add
#          proof shot). Screenshots are the slowest single step on Taobao
#          pages, so a 25-line batch saves dozens of seconds this way.
SHOT_MODE = (os.environ.get("TAOBAO_SHOTS") or "all").strip().lower()

# Cap for "networkidle" waits. Taobao pages rarely go idle; the full cap is
# pure dead time, so batches use a shorter one.
IDLE_TIMEOUT_MS = int(float(os.environ.get("TAOBAO_IDLE_TIMEOUT") or 10) * 1000)

# Human-like shopping simulation (multi-stage scrolling, mouse meandering).
# It costs several seconds per page and is pointless in a batch.
BROWSE_SIM = (os.environ.get("TAOBAO_BROWSE_SIM") or "1").strip().lower() not in (
    "0", "false", "off", "no",
)

# Screenshot names that are kept even in "fail" mode: the failure forensics
# plus the single proof shot that the item really went into the cart.
_SHOT_KEEP = lambda name: (  # noqa: E731
    SHOT_MODE != "fail"
    or "fail" in name
    or name.startswith("cart_add")
    or name.startswith("cart")
)


class VerificationRequired(RuntimeError):
    """Raised when Taobao shows a CAPTCHA/slider/verification page.

    The session must stop and hand control back to the user. This class is
    intentionally NOT retried and NOT auto-solved.
    """

    code = "VERIFICATION_REQUIRED"

    def __init__(
        self,
        message: str = (
            "Taobao requires manual verification (CAPTCHA/slider). "
            "Complete it in the browser window, then retry the command."
        ),
    ) -> None:
        super().__init__(message)


class QuantityError(RuntimeError):
    """Raised when the requested purchase quantity cannot be set/confirmed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkuAmbiguousError(RuntimeError):
    """Raised when one SKU target matches more than one option."""

    code = "SKU_AMBIGUOUS"

    def __init__(self, label: str, value: str, candidates: list[str]) -> None:
        super().__init__(
            f"SKU option '{value}' under group '{label}' matches "
            f"{len(candidates)} values: {candidates}"
        )
        self.label = label
        self.value = value
        self.candidates = candidates


class BrowserAdapter:
    def __init__(self, browser_name: str = "chromium", headless: bool = False, artifact_dir: str = ".cache/taobao-search-skill/artifacts") -> None:
        self.browser_name = browser_name
        self.headless = headless
        artifact_path = Path(artifact_dir)
        if not artifact_path.is_absolute():
            artifact_path = _SCRIPTS_DIR / artifact_path
        self.artifact_dir = artifact_path
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp = False

    # ──────────────────────────────────────────────
    # Browser Lifecycle
    # ──────────────────────────────────────────────

    def open(self) -> None:
        if self._browser is not None or self._context is not None:
            return

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # Plain Playwright start: no stealth patch, no navigator overrides.
        self._playwright = sync_playwright().start()

        browser_type = getattr(self._playwright, self.browser_name, None)
        if browser_type is None:
            raise ValueError(f"Unsupported browser type: {self.browser_name}")

        # Usability flags only; no automation-hiding flags.
        launch_args = [
            "--no-first-run",
            "--window-size=1920,1080",
        ]

        # Attach to an already-running normal browser when a CDP endpoint is
        # configured. This is the human-in-the-loop path: the user logs in and
        # solves verification in a normal browser; we only drive the same pages.
        cdp_url = os.environ.get("TAOBAO_CDP_URL")
        if cdp_url:
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = list(self._browser.contexts)
                self._context = contexts[0] if contexts else self._browser.new_context()
                pages = [p for p in self._context.pages if not p.is_closed()]
                self._page = self._pick_page(pages) or self._context.new_page()
                if len(pages) > 1:
                    print(
                        "[browser] %d live pages on attach: %s"
                        % (len(pages), " | ".join((x.url or "about:blank")[:60] for x in pages)),
                        file=sys.stderr,
                    )
                self._cdp = True
                print(
                    f"[browser] attached via CDP {cdp_url} ({len(pages)} page(s))",
                    file=sys.stderr,
                )
                return
            except Exception as exc:
                print(f"[browser] CDP attach failed: {exc}", file=sys.stderr)
                # 不能在 CDP 模式下换一个自己的浏览器顶上：换浏览器 = 换设备指纹，
                # 淘宝会把已经扫码的登录判成陌生设备，于是每次都要重新扫码。
                raise RuntimeError(
                    "CDP 端点连不上（默认 http://127.0.0.1:9222）。请先用 "
                    '--remote-debugging-port=9222 --user-data-dir="<同一 profile>" 启动 Edge，'
                    "或者把 settings.json 里的 cdp_url 删掉再改用内置浏览器。"
                ) from exc

        # Preferred: a persistent profile. Login survives restarts, and human
        # interaction behaves like a normal browser (closing a tab does not
        # invalidate the whole automation session).
        if self._launch_persistent(browser_type, launch_args):
            return

        launch_errors: list[str] = []
        launch_attempts: list[dict[str, Any]] = [
            {"headless": self.headless, "args": launch_args},
            {"headless": self.headless, "channel": "msedge", "args": launch_args},
            {"headless": self.headless, "channel": "chrome", "args": launch_args},
        ]

        for launch_kwargs in launch_attempts:
            try:
                self._browser = browser_type.launch(**launch_kwargs)
                print(f"[browser] open {self.browser_name} browser session", file=sys.stderr)
                return
            except Exception as exc:
                launch_errors.append(f"{launch_kwargs}: {exc}")

        raise RuntimeError("Unable to launch a browser backend. Attempts: " + " | ".join(launch_errors))

    def _launch_persistent(self, browser_type, launch_args: list[str]) -> bool:
        """Launch a persistent profile; fall back to an ephemeral context on failure."""
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        base_kwargs: dict[str, Any] = {
            "user_data_dir": str(_PROFILE_DIR),
            "headless": self.headless,
            "args": launch_args,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        errors: list[str] = []
        for extra in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                context = browser_type.launch_persistent_context(**base_kwargs, **extra)
                self._context = context
                self._browser = context.browser
                pages = [p for p in context.pages if not p.is_closed()]
                self._page = self._pick_page(pages) or context.new_page()
                print(
                    f"[browser] persistent profile {_PROFILE_DIR} ({extra or 'bundled chromium'})",
                    file=sys.stderr,
                )
                return True
            except Exception as exc:
                errors.append(f"{extra or 'default'}: {exc}")
        print("[browser] persistent profile unavailable, falling back: " + " | ".join(errors), file=sys.stderr)
        return False

    def _create_context(self, storage_state: dict[str, Any] | None = None) -> None:
        if self._context is not None:
            # Persistent profile / CDP-attached browser is already active.
            # NEVER overwrite the browser's own login cookies with an old
            # snapshot: a stale _tb_token_ / cookie2 / unb gets written back on
            # every start and Taobao then asks for the QR code again — the
            # "扫一次码之后一直让我重新扫" loop.
            self._merge_missing_cookies(storage_state)
            return

        if self._browser is None:
            raise RuntimeError("Browser is not opened")

        # Fixed viewport. No random fingerprint, no user-agent override.
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state

        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(15000)
        self._page = self._context.new_page()

    def _ensure_page(self) -> Page:
        if self._context is None:
            self._create_context()
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = self._pick_page(pages) or self._context.new_page()
            print(f"[browser] page reattached/recreated ({len(pages)} live pages)", file=sys.stderr)
        return self._page

    @staticmethod
    def _pick_page(pages: list[Page]) -> Page | None:
        """多个标签页时挑「最像淘宝」的那个，别抓到用户的空白页/文档页。

        CDP 模式接的是用户自己的浏览器，标签页数量和顺序都不受我们控制：
        浏览器恢复上次会话、或用户随手开了新标签，`pages[0]` 很可能是一张
        about:blank。以前直接取 pages[0]，就会出现「首屏永远没卡」——
        真正的搜索结果在另一张标签里，我们却在空白页上截图、探测、换词。
        这里只**选**页，不关用户的标签。
        """
        if not pages:
            return None
        for page in pages:
            url = (page.url or "").lower()
            if "taobao.com" in url or "tmall.com" in url:
                return page
        return pages[0]

    def close(self) -> None:
        if self._cdp:
            # The browser belongs to the user; only drop our connection.
            with suppress(Exception):
                if self._playwright is not None:
                    self._playwright.stop()
            self._context = None
            self._browser = None
            self._page = None
            self._playwright = None
            self._cdp = False
            return
        with suppress(Exception):
            if self._context is not None:
                self._context.close()
        with suppress(Exception):
            if self._browser is not None:
                self._browser.close()
        with suppress(Exception):
            if self._playwright is not None:
                self._playwright.stop()
        self._context = None
        self._browser = None
        self._page = None
        self._playwright = None

    # ──────────────────────────────────────────────
    # Session Management
    # ──────────────────────────────────────────────

    def _merge_missing_cookies(self, storage_state: dict[str, Any] | None) -> None:
        """Only fill in cookies the live browser does not already have.

        快照是「补登录态」用的，不是「覆盖登录态」用的：同名同域同路径的 cookie
        一律以浏览器里的为准，过期的一律丢掉。
        """
        if not storage_state or self._context is None:
            return
        cookies = storage_state.get("cookies") or []
        if not cookies:
            return
        try:
            existing = {
                (c.get("name"), c.get("domain"), c.get("path")) for c in self._context.cookies()
            }
        except Exception:  # noqa: BLE001
            existing = set()
        now = time.time()
        fresh: list[dict[str, Any]] = []
        kept = dropped = 0
        for c in cookies:
            key = (c.get("name"), c.get("domain"), c.get("path"))
            exp = c.get("expires")
            if key in existing:
                kept += 1
                continue
            if isinstance(exp, (int, float)) and 0 < exp < now:
                dropped += 1
                continue
            fresh.append(c)
        if fresh:
            with suppress(Exception):
                self._context.add_cookies(fresh)
        print(
            f"[browser] session restore: added={len(fresh)} kept_browser={kept} dropped_expired={dropped}",
            file=sys.stderr,
        )

    def restore_session(self, snapshot: SessionSnapshot) -> bool:
        self.open()
        self._create_context(storage_state=snapshot.storage_state)
        print("[browser] restore session from persisted state", file=sys.stderr)
        return True

    def capture_session(self) -> SessionSnapshot:
        context = self._context
        if context is None:
            raise RuntimeError("Browser context is not initialized")
        print("[browser] capture current browser session", file=sys.stderr)
        state = context.storage_state()
        keep = ("taobao", "tmall", "alicdn", "alipay")
        now = time.time()
        cookies: list[dict[str, Any]] = []
        for c in state.get("cookies") or []:
            dom = str(c.get("domain") or "")
            if not any(k in dom for k in keep):
                continue
            exp = c.get("expires")
            if not isinstance(exp, (int, float)) or exp <= 0:
                # 会话 cookie（unb / _tb_token_ / cookie2 常常就是这种）：
                # 落盘时必须给个过期时间，否则重启浏览器后拿不回来，
                # 于是每次启动都被要求重新扫码。
                c = dict(c)
                c["expires"] = now + 30 * 24 * 3600
            cookies.append(c)
        origins = [
            o
            for o in (state.get("origins") or [])
            if any(k in str(o.get("origin") or "") for k in keep)
        ]
        return SessionSnapshot(storage_state={"cookies": cookies, "origins": origins})

    # ──────────────────────────────────────────────
    # Login
    # ──────────────────────────────────────────────

    def navigate_to_taobao(self) -> None:
        page = self._ensure_page()
        page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=30000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5000)
        # Auto-solve CAPTCHA if triggered on navigation
        self._handle_captcha_if_present(page)

    def is_logged_in(self) -> bool:
        page = self._ensure_page()
        if self._looks_logged_in(page):
            print("[browser] check login status => logged in", file=sys.stderr)
            return True
        print("[browser] check login status => not logged in", file=sys.stderr)
        return False

    def ensure_login(self, manual_approval_required: bool, force_manual: bool = False) -> str:
        page = self._ensure_page()
        if not force_manual and self._looks_logged_in(page):
            return "success"
        if manual_approval_required:
            print("[browser] waiting for human takeover or approved login flow", file=sys.stderr)
            self._wait_for_user_login(page)
            return "success" if self._looks_logged_in(page) else "waiting_manual"
        return "success"

    def _looks_logged_in(self, page: Page) -> bool:
        # Cookies are shared by every tab in the context. Check the login cookie
        # first: if the user logged in from a new tab, the original Playwright
        # page can still show the guest navigation ("亲，请登录"), and the old
        # order returned a false negative before ever reaching the cookie check.
        with suppress(Exception):
            for cookie in page.context.cookies():
                if cookie.get("name") == "unb" and cookie.get("value"):
                    return True
        url = page.url.lower()
        if any(signal in url for signal in LOGIN_PAGE_URL_SIGNALS):
            return False
        with suppress(Exception):
            for sel in NOT_LOGGED_IN_TEXT:
                if page.locator(sel).first.is_visible():
                    return False
        for selector in LOGGED_IN_INDICATORS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible(timeout=2000):
                    return True
        with suppress(Exception):
            cookies = page.context.cookies()
            for cookie in cookies:
                if cookie.get("name") in LOGIN_COOKIE_NAMES and cookie.get("value"):
                    if "login" not in url:
                        return True
        return False

    def _wait_for_user_login(self, page: Page) -> None:
        print("[browser] ============================================", file=sys.stderr)
        print("[browser] 请在弹出的浏览器窗口中手动完成淘宝登录", file=sys.stderr)
        print("[browser] 登录完成后脚本会自动检测并继续", file=sys.stderr)
        print("[browser] ============================================", file=sys.stderr)

        with suppress(Exception):
            login_link = page.locator(NOT_LOGGED_IN_TEXT[0]).first
            if login_link.is_visible():
                self._human_click(page, login_link)
                page.wait_for_timeout(2000)

        check_interval = 30
        max_checks = 7  # up to ~3.5 min
        total_slept = 0

        # Immediate first check — user might already be logged in from a popup
        if self._looks_logged_in(page):
            print("[browser] 登录成功! (立即检测到)", file=sys.stderr)
            return

        for i in range(max_checks):
            time.sleep(check_interval)
            total_slept += check_interval
            print(f"[browser] 等待登录中... 第 {i+1}/{max_checks} 次检测 (已等待 {total_slept}s/{max_checks * check_interval}s)", file=sys.stderr)

            # Check current page without navigating — don't interrupt user's login flow
            if self._looks_logged_in(page):
                print(f"[browser] 登录成功! (等待约 {total_slept}s)", file=sys.stderr)
                return

            # If we're still on a login domain, user hasn't finished — keep waiting
            url = page.url.lower()
            if "login.taobao.com" in url or "login.tmall.com" in url or "login" in url:
                continue

            # On a non-login page but not detected: gently refresh to sync state
            with suppress(Exception):
                page.reload(wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            if self._looks_logged_in(page):
                print(f"[browser] 登录成功! (等待约 {total_slept}s, 刷新后检测到)", file=sys.stderr)
                return

        print("[browser] 登录超时，请重试", file=sys.stderr)

    # ──────────────────────────────────────────────
    # Search
    # ──────────────────────────────────────────────

    def search(self, keyword: str) -> str:
        page = self._ensure_page()

        # Direct search URL: reliable from the homepage, i.taobao.com, or a
        # login redirect, and independent of the page's search-box behavior.
        # I1：必须带 page=1&tab=all。只拼 ?q= 时淘宝的 SPA 会「不挂载结果容器」——
        # 页面上骨架永远转圈、anchors=105 但 item_links=0，白等 13–15s；
        # 被规范化成 page=1&q=…&tab=all 的请求都正常出卡。
        search_url = f"https://s.taobao.com/search?page=1&q={quote(keyword)}&tab=all"
        print(f"[browser] search keyword: {keyword}", file=sys.stderr)
        # commit：响应一到就返回，剩下的交给 wait_for_cards 轮询。
        # 搜索页要等广告/埋点才 DOMContentLoaded，白等 0.5~1.5 秒没意义。
        _t0 = time.monotonic()
        page.goto(search_url, wait_until="commit", timeout=30000)
        _t1 = time.monotonic()
        self._human_wait(0.15, 0.35)

        # Auto-solve CAPTCHA if triggered by search navigation
        self._handle_captcha_if_present(page)

        # 结果页有埋点长连接，networkidle 基本必然超时，白等 2~3 秒。
        # 商品卡一出现就能解析，直接轮询它；三态返回见 wait_for_cards。
        _cards = self.wait_for_cards(timeout_ms=int(6000 * max(SPEED, 0.6)))
        _renav = 0
        if _cards == -3:
            # 风控中间页接管了结果接口（新规则）：先自己刷新一次，刷出商品就继续；
            # 刷新完还是没商品，才算真被拦住，交回人工。
            _renav += 1
            _cards = self._recover_cards(page, int(6000 * max(SPEED, 0.6)))
            if _cards == -3:
                print("[browser] middleware persists after refresh → manual takeover", file=sys.stderr)
                raise VerificationRequired()
        if _cards == -1:
            # 首屏没挂载结果容器：多半是 SPA 首次渲染丢了。重新导航一次基本必好，
            # 比让上层换词便宜（换词会换掉本来正确的词，还多一次导航）。
            _renav += 1
            print("[browser] no cards on first load → one re-navigation", file=sys.stderr)
            with suppress(Exception):
                page.reload(wait_until="commit", timeout=20000)
            self._human_wait(0.15, 0.3)
            _cards = self.wait_for_cards(timeout_ms=int(5000 * max(SPEED, 0.6)))
            if _cards == -3:
                _cards = self._recover_cards(page, int(5000 * max(SPEED, 0.6)))
                if _cards == -3:
                    print("[browser] middleware persists after refresh → manual takeover", file=sys.stderr)
                    raise VerificationRequired()
        _t2 = time.monotonic()

        # Human-like scroll to trigger lazy loading（只为凑够候选卡）
        # 已经够 16 张卡就不用滚：滚动本身要 0.2–0.5s，而首屏就是我们要的前几张。
        if 0 <= _cards < 10:
            self._human_scroll(page, target_y=random.randint(800, 1200))

        print(
            f"[browser] timing search: goto={_t1 - _t0:.2f}s cards={_t2 - _t1:.2f}s "
            f"state={'cards' if _cards >= 0 else ('empty' if _cards == -2 else 'timeout')} "
            f"renav={_renav} total={time.monotonic() - _t0:.2f}s",
            file=sys.stderr,
        )
        print(f"[browser] current url: {page.url}", file=sys.stderr)
        return "success"

    def wait_for_results(self) -> str:
        page = self._ensure_page()
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
        if not self._find_candidate_links(page):
            print("[browser] results page ready but no candidates found yet", file=sys.stderr)
        else:
            print("[browser] wait for results page ready", file=sys.stderr)
        return "success"

    # 风控中间页标记：接口被重定向到 _____tmd_____ 时页面不弹窗，
    # 只是永远「加载中…」。这是最容易被误判成「首屏没渲染」的一种。
    _TMD_MARK = "_____tmd_____"

    # 页面「明确说没有结果」的文案（I2）。只有命中这些才快速判空 ——
    # 慢渲染绝不能判空，否则本来有货的词被丢掉，整行白跑。
    EMPTY_RESULT_SIGNALS = (
        "没有找到", "抱歉，没有", "暂无相关", "无搜索结果",
        "没有相关的宝贝", "0个宝贝", "没找到与", "换个词试试",
    )

    _MAIN_CARDS_JS = """(sels) => sels.some((s) => {
        try { return document.querySelectorAll(s).length > 0; } catch (e) { return false; }
    })"""

    def _main_has_cards(self, page: Page) -> bool:
        """主文档当前能不能数出商品卡（判断 tmd 惩罚帧是否只是残留）。"""
        try:
            return bool(page.evaluate(self._MAIN_CARDS_JS, PRODUCT_LINK_SELECTORS))
        except Exception:
            return False

    def risk_middleware_hit(self) -> bool:
        """当前页面是否有风控中间页（接口被 _____tmd_____ 接管）。

        tmd 帧残留但主文档已有商品卡时不算（惩罚已解除，P2-1）。
        """
        try:
            page = self._ensure_page()
            if any(self._TMD_MARK in (f.url or "") for f in page.frames):
                return not self._main_has_cards(page)
            return False
        except Exception:
            return False

    _CARD_PROBE_JS = """(links) => {
      const n = (s) => { try { return document.querySelectorAll(s).length } catch (e) { return 0 } };
      if (links.some((s) => n(s) > 0)) return { cards: 1 };
      // 只认「看得见」的节点：淘宝页面里藏着大量 display:none 的模板
      // （空的「没有找到相关宝贝」、骨架屏），textContent 照样有字。
      // P1-4：不算可见性的话，正常结果页 busy 恒 ≥1（-1 早退永远走不到），
      // 加载中也可能误读出隐藏的空结果模板（-2 误判换词）。
      const vis = (el) => {
        try { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
        catch (e) { return false; }
      };
      let empty = "";
      const nodes = document.querySelectorAll(
        '[class*="empty"],[class*="Empty"],[class*="noData"],[class*="nodata"],[class*="noResult"]'
      );
      for (const el of nodes) {
        if (!vis(el)) continue;
        const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
        if (t) { empty = t.slice(0, 160); break; }
      }
      let busy = 0;
      for (const el of document.querySelectorAll(
        '[class*="loading"],[class*="Loading"],[class*="skeleton"],[class*="Skeleton"],[class*="spinner"]'
      )) {
        if (vis(el)) busy += 1;
      }
      return { cards: 0, empty: empty, busy: busy };
    }"""

    def wait_for_cards(self, timeout_ms: int = 12000, poll_ms: int = 200,
                       settle_ms: int = 3000) -> int:
        """等待商品卡出现。四态返回：

        >= 0 : 已有商品卡，值是耗时 ms
        -1   : 没有卡、也没有「明确空结果」→ 慢渲染或页面异常，上层先重导航再换词
        -2   : 页面明确写了没有结果，通常 ≤2s 就返回，上层直接换词
        -3   : 风控中间页（_____tmd_____）接管了结果接口 —— 页面不弹窗、永远
               「加载中…」。上层**先刷新自恢复**（v0.8 起，见 `_recover_cards` /
               `recover_from_verification`，默认 2 次）：刷出商品就继续，
               仍无商品才转人工，不许换词硬撞。

        以前只有 -1，而且 `_cmd_search` 还会再叠一次 `wait_for_results()`
        （domcontentloaded 10s + networkidle 10s），一条真空结果行白烧 12–22s。
        """
        page = self._ensure_page()
        started = time.monotonic()
        _tick = 0
        while (time.monotonic() - started) * 1000 < timeout_ms:
            probe = None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _tick += 1
            if _tick % 5 == 0 and elapsed_ms >= 1000:
                # 每 ~1s 探一次风控中间页，命中就立刻上报，别等满 6 秒
                if self.risk_middleware_hit():
                    print("[browser] risk-control middleware on results API", file=sys.stderr)
                    return -3
            try:
                # 一次 evaluate 同时拿到「有卡 / 空结果文案 / 是否还在骨架屏」，
                # 比分别调三次选择器便宜得多。
                probe = page.evaluate(self._CARD_PROBE_JS, PRODUCT_LINK_SELECTORS)
            except Exception:
                probe = None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if probe:
                if probe.get("cards"):
                    return elapsed_ms
                empty_text = (probe.get("empty") or "").strip()
                if empty_text and any(sig in empty_text for sig in self.EMPTY_RESULT_SIGNALS):
                    print(f"[browser] empty result page: {empty_text[:60]}", file=sys.stderr)
                    return -2
                if not probe.get("busy") and elapsed_ms >= settle_ms:
                    ready = "loading"
                    with suppress(Exception):
                        ready = page.evaluate("() => document.readyState")
                    if ready != "loading":
                        # 加载态结束、没有骨架屏、3s 还是 0 卡：再给 1.2s 短窗口，
                        # 仍无卡就放行（-1 只代表「换词」，不代表这行失败）。
                        page.wait_for_timeout(1200)
                        try:
                            again = page.evaluate(self._CARD_PROBE_JS, PRODUCT_LINK_SELECTORS)
                        except Exception:
                            again = None
                        if again and again.get("cards"):
                            return int((time.monotonic() - started) * 1000)
                        return -1
            page.wait_for_timeout(poll_ms)
        return -1

    def ensure_search_access(self, manual_approval_required: bool) -> bool:
        page = self._ensure_page()
        if not self._looks_access_blocked(page):
            return True
        print("[browser] access blocked by Taobao risk control", file=sys.stderr)

        if not manual_approval_required:
            return False
        print("[browser] waiting for user to pass manual verification", file=sys.stderr)
        self._wait_for_access_recovery(page)
        return not self._looks_access_blocked(page)

    # ──────────────────────────────────────────────
    # Candidate Collection & Cart
    # ──────────────────────────────────────────────

    def collect_candidates(self, keyword: str, max_candidates: int,
                           price_min: float | None = None, price_max: float | None = None,
                           min_sales: int | None = None, require_free_shipping: bool = False,
                           require_tmall: bool | None = None) -> list[MatchedItem]:
        page = self._ensure_page()
        import time as _t
        _c0 = _t.monotonic()
        links = self._find_candidate_links(page)
        _c1 = _t.monotonic()
        candidates: list[MatchedItem] = []
        seen_urls: set[str] = set()
        keyword_tokens = self._build_keyword_tokens(keyword)
        skipped_price = 0
        skipped_sales = 0
        skipped_shipping = 0
        skipped_tmall = 0

        if not links:
            anchor_count = page.locator("a").count()
            item_count = page.locator(PRODUCT_LINK_SELECTORS[0]).count()
            tmall_count = page.locator(PRODUCT_LINK_SELECTORS[1]).count()
            print(f"[browser] candidate diagnostics => url={page.url}, anchors={anchor_count}, item_links={item_count}, tmall_links={tmall_count}", file=sys.stderr)
            with suppress(Exception):
                samples = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a'))
                        .slice(0, 12)
                        .map((a) => ({ href: a.getAttribute('href') || '', text: (a.textContent || '').trim().slice(0, 40) }))"""
                )
                print(f"[browser] anchor samples => {samples}", file=sys.stderr)

        for link in links:
            if len(candidates) >= max_candidates:
                break
            href = link.get("href")
            title = (link.get("title") or link.get("text") or "").strip()
            if not href or href in seen_urls or not title:
                continue
            text = link.get("text", "")
            card_text = link.get("card_text", text)
            combined_text = f"{title}\n{text}\n{card_text}"
            if keyword_tokens and not self._matches_keyword(title, text, keyword_tokens):
                continue
            absolute_href = urljoin(page.url, href)
            seen_urls.add(absolute_href)

            rating = self._extract_rating(combined_text)
            price_str, price_val = self._extract_price(card_text)
            sales = self._extract_sales_count(card_text)
            free_shipping = self._check_free_shipping(card_text)
            is_tmall = self._check_is_tmall(absolute_href, card_text)

            # Apply filters
            if require_tmall is True and not is_tmall:
                skipped_tmall += 1
                continue
            if require_tmall is False and is_tmall:
                skipped_tmall += 1
                continue
            if price_min is not None and price_val is not None and price_val < price_min:
                skipped_price += 1
                continue
            if price_max is not None and price_val is not None and price_val > price_max:
                skipped_price += 1
                continue
            if require_free_shipping and not free_shipping:
                skipped_shipping += 1
                continue
            if min_sales is not None and (sales is None or sales < min_sales):
                skipped_sales += 1
                continue

            item = MatchedItem(
                title=title, spec_text=card_text or "", url=absolute_href, rating=rating,
                price=price_str, price_value=price_val,
                sales_count=sales, free_shipping=free_shipping, is_tmall=is_tmall,
            )
            candidates.append(item)
            # 原来是每 3 个候选等 0.3–1s：16 个候选 = 5 次 = 2–4 秒纯空转，
  # 而且这一步不产生任何请求，对防风控毫无作用。改成每 8 个等 0.1–0.25s。
            if len(candidates) % 8 == 0:
                self._human_wait(0.1, 0.25)

        filter_msgs = []
        if skipped_price: filter_msgs.append(f"price={skipped_price}")
        if skipped_sales: filter_msgs.append(f"sales={skipped_sales}")
        if skipped_shipping: filter_msgs.append(f"shipping={skipped_shipping}")
        if skipped_tmall: filter_msgs.append(f"tmall={skipped_tmall}")
        filter_info = f", skipped({', '.join(filter_msgs)})" if filter_msgs else ""

        print(
            f"[browser] collect up to {max_candidates} candidates, found={len(candidates)}{filter_info} "
            f"links={(_c1 - _c0) * 1000:.0f}ms rest={(_t.monotonic() - _c1) * 1000:.0f}ms",
            file=sys.stderr,
        )
        return candidates

    def _extract_detail_price(self, page: Page) -> float | None:
        price_text = page.evaluate("""(selectors) => {
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return null;
        }""", PRICE_SELECTORS)
        if not price_text:
            return None
        match = re.search(r'([\d,]+(?:\.\d{2})?)', price_text.replace(',', ''))
        if not match:
            return None
        try:
            val = float(match.group(1))
            return val if val >= 0.01 else None
        except ValueError:
            return None

    def enrich_item_rating(self, item: MatchedItem) -> float | None:
        page = self._ensure_page()
        if not item.url:
            return None
        try:
            self._human_wait(0.5, 1.5)
            self._random_mouse_move(page)
            self._human_wait(0.2, 0.5)

            page.goto(item.url, wait_until="domcontentloaded", timeout=20000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self._handle_captcha_if_present(page)

            self._simulate_browsing(page, max_scroll=600)

            # Extract item_id from URL query param
            if not item.item_id:
                parsed = urlparse(item.url)
                params = parse_qs(parsed.query)
                id_vals = params.get("id", [])
                if id_vals:
                    item.item_id = id_vals[0]

            # Extract price from detail page
            if not item.price or item.price_value is None:
                detail_price = self._extract_detail_price(page)
                if detail_price is not None:
                    item.price = f"¥{detail_price:.2f}"
                    item.price_value = detail_price

            # Extract sales count from detail page if not found on search page
            if item.sales_count is None:
                sales_text = page.evaluate("""(selectors) => {
                    let text = '';
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) text += ' ' + (el.textContent || '');
                    }
                    return text || document.body.innerText;
                }""", SALES_COUNT_SELECTORS)
                item.sales_count = self._extract_sales_count(sales_text)

            text = page.evaluate("""(selectors) => {
                const body = document.body.innerText;
                const ratingEls = document.querySelectorAll(selectors.join(','));
                let extra = '';
                ratingEls.forEach(el => { extra += ' ' + el.textContent; });
                return body + extra;
            }""", RATING_SELECTORS)

            rating = self._extract_rating(text)
            if rating is not None:
                item.rating = rating
                print(f"[browser] enriched rating for [{item.title[:30]}]: {rating*100:.0f}%", file=sys.stderr)
            else:
                print(f"[browser] no rating found for [{item.title[:30]}]", file=sys.stderr)
            return rating
        except Exception as e:
            print(f"[browser] enrich rating failed for [{item.title[:30]}]: {e}", file=sys.stderr)
            return None

    def add_to_cart(self, item: MatchedItem, sku_keywords: str | None = None,
                    price_min: float | None = None, price_max: float | None = None) -> bool | None:
        """Add item to cart. Returns True on success, False on SKU/price mismatch, None on error."""
        page = self._ensure_page()
        if item.url:
            self._human_wait(2, 5)
            self._random_mouse_move(page)
            self._human_wait(0.3, 0.8)

            page.goto(item.url, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self._handle_captcha_if_present(page)

            self._simulate_browsing(page, max_scroll=1000)

        sku_ok = self._select_default_sku(page, sku_keywords=sku_keywords)
        if sku_ok is False:
            print(f"[browser] SKU mismatch for [{item.title[:30]}]: no option matching '{sku_keywords}'", file=sys.stderr)
            return False

        # Verify actual SKU price against constraints
        if price_min is not None or price_max is not None:
            detail_price = self._extract_detail_price(page)
            if detail_price is not None:
                item.price = f"¥{detail_price:.2f}"
                item.price_value = detail_price
                if price_min is not None and detail_price < price_min:
                    print(f"[browser] SKU price {detail_price:.2f} < min {price_min:.2f} for [{item.title[:30]}]", file=sys.stderr)
                    return False
                if price_max is not None and detail_price > price_max:
                    print(f"[browser] SKU price {detail_price:.2f} > max {price_max:.2f} for [{item.title[:30]}]", file=sys.stderr)
                    return False

        button = self._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
        if button is None:
            print(f"[browser] add item to cart failed: {item.title}", file=sys.stderr)
            return False

        self._human_click(page, button)
        self._human_wait(1, 2.5)
        item.cart_added = True
        print(f"[browser] add item to cart: {item.title}", file=sys.stderr)
        return True

    def _select_default_sku(self, page: Page, sku_keywords: str | None = None) -> bool | None:
        """Select SKU value items. Returns True/None/False."""
        # Wait for individual SKU value elements to appear
        has_sku = False
        with suppress(Exception):
            page.wait_for_selector(SKU_VALUE_SELECTOR, timeout=8000)
            has_sku = True
        if not has_sku:
            with suppress(Exception):
                page.wait_for_selector('[class*="skuItem"]', timeout=5000)
                has_sku = True
        if not has_sku:
            return None

        tokens = None
        if sku_keywords:
            tokens = [t.strip().upper() for t in sku_keywords.split() if t.strip()]
            print(f"[browser] SKU keywords: {tokens}", file=sys.stderr)

        # Find individual value elements (not group wrappers)
        value_items = page.locator(SKU_VALUE_SELECTOR).all()
        if not value_items:
            for sel in SKU_OPTION_SELECTORS:
                value_items = page.locator(sel).all()
                if value_items:
                    break

        if not value_items:
            return None

        # Build list of (text, element) for visible value items
        visible_items: list[tuple[str, any]] = []
        for item in value_items:
            with suppress(Exception):
                if item.is_visible(timeout=800):
                    text = (item.inner_text() or '').strip()
                    if text and len(text) < 40:  # skip multi-line group wrappers
                        visible_items.append((text, item))

        if not visible_items:
            return None

        if tokens:
            matched = 0
            for token in tokens:
                for i, (text, el) in enumerate(visible_items):
                    if token in text.upper():
                        if self._is_sku_selected(el):
                            matched += 1
                            print(f"[browser] SKU already selected '{token}' -> '{text}'", file=sys.stderr)
                            break
                        with suppress(Exception):
                            self._human_click(page, el)
                            self._human_wait(0.3, 0.6)
                            matched += 1
                            print(f"[browser] SKU matched '{token}' -> '{text}'", file=sys.stderr)
                            break
            if matched == 0:
                print(f"[browser] SKU match failed: none of {tokens} found in {[t for t,_ in visible_items]}", file=sys.stderr)
                return False
            print(f"[browser] SKU matched {matched}/{len(tokens)} keywords", file=sys.stderr)
        else:
            el = visible_items[0][1]
            if not self._is_sku_selected(el):
                with suppress(Exception):
                    self._human_click(page, el)
                    self._human_wait(0.3, 0.6)
            print(f"[browser] SKU default selected: '{visible_items[0][0]}'", file=sys.stderr)

        return True

    @staticmethod
    def _is_sku_selected(locator) -> bool:
        """Check if a SKU value item is already selected (has isSelected class)."""
        with suppress(Exception):
            cls = locator.get_attribute("class") or ""
            return "isSelected" in cls or "selected" in cls.lower() or "active" in cls.lower()
        return False

    # ──────────────────────────────────────────────
    # Purchase Quantity
    # ──────────────────────────────────────────────

    def set_quantity(self, qty: int) -> None:
        """Set the purchase quantity on the current item page.

        Priority: fill the quantity input; otherwise click "+" (qty-current)
        times. Never falls back to 1 silently: raises QuantityError when the
        control is missing, unreadable, or does not reach the requested value.
        """
        page = self._ensure_page()
        self._human_wait(0.3, 0.8)
        control = self._find_quantity_control(page)
        if control is None:
            raise QuantityError(
                "QTY_CONTROL_UNAVAILABLE",
                f"No quantity control found; refusing to add {qty} as 1. Set the quantity manually.",
            )
        kind, locator = control
        if kind == "input":
            self._set_quantity_via_input(page, locator, qty)
        else:
            self._set_quantity_via_plus(page, locator, qty)
        actual = self._read_quantity(page)
        if actual != qty:
            raise QuantityError(
                "QTY_SET_FAILED",
                f"Quantity did not reach {qty} (read back {actual!r}).",
            )

    def _find_quantity_control(self, page: Page) -> tuple[str, Any] | None:
        from taobao_selectors import QUANTITY_INPUT_SELECTORS, QUANTITY_PLUS_SELECTORS

        for selector in QUANTITY_INPUT_SELECTORS:
            locator = self._find_first_visible_locator(page, [selector])
            if locator is None:
                continue
            editable = False
            with suppress(Exception):
                editable = locator.is_editable()
            if editable:
                return ("input", locator)

        for selector in QUANTITY_PLUS_SELECTORS:
            locator = self._find_first_visible_locator(page, [selector])
            if locator is not None:
                return ("plus", locator)
        return None

    def _read_quantity(self, page: Page) -> int | None:
        from taobao_selectors import QUANTITY_CONTAINER_SELECTORS, QUANTITY_INPUT_SELECTORS

        for selector in QUANTITY_INPUT_SELECTORS:
            locator = self._find_first_visible_locator(page, [selector])
            if locator is None:
                continue
            with suppress(Exception):
                raw = (locator.input_value() or "").strip()
                if raw.isdigit():
                    return int(raw)

        for selector in QUANTITY_CONTAINER_SELECTORS:
            locator = self._find_first_visible_locator(page, [selector])
            if locator is None:
                continue
            with suppress(Exception):
                match = re.search(r"(?<!\d)(\d{1,5})(?!\d)", locator.inner_text() or "")
                if match:
                    return int(match.group(1))
        return None

    def _set_quantity_via_input(self, page: Page, locator: Any, qty: int) -> None:
        with suppress(Exception):
            locator.scroll_into_view_if_needed()
        focused = False
        try:
            # Short timeout: a stuck focus must not burn 30s of a batch.
            locator.click(timeout=3000)
            focused = True
        except Exception:
            with suppress(Exception):
                locator.focus(timeout=2000)
                focused = True
        if not focused:
            raise QuantityError("QTY_SET_FAILED", "Cannot focus quantity input")
        with suppress(Exception):
            locator.fill("", timeout=3000)
        with suppress(Exception):
            locator.fill(str(qty), timeout=3000)
        with suppress(Exception):
            locator.press("Enter", timeout=2000)
        with suppress(Exception):
            locator.press("Tab", timeout=2000)
        self._human_wait(0.4, 0.9)

    def _set_quantity_via_plus(self, page: Page, locator: Any, qty: int) -> None:
        current = self._read_quantity(page)
        if current is None:
            raise QuantityError(
                "QTY_UNREADABLE",
                "Quantity value is not readable; refusing to guess. Set the quantity manually.",
            )
        if current > qty:
            raise QuantityError(
                "QTY_TOO_HIGH",
                f"Current quantity {current} is larger than requested {qty}; set it manually.",
            )
        steps = qty - current
        if steps > _MAX_QTY_PLUS_CLICKS:
            raise QuantityError(
                "QTY_CONTROL_UNAVAILABLE",
                f"Need {steps} '+' clicks, above the safety cap ({_MAX_QTY_PLUS_CLICKS}). Set the quantity manually.",
            )
        try:
            for _ in range(steps):
                self._human_click(page, locator)
                self._human_wait(0.2, 0.5)
        except Exception as exc:
            raise QuantityError("QTY_SET_FAILED", f"Failed to click '+': {exc}") from exc
        self._human_wait(0.3, 0.8)

    def read_quantity(self) -> int | None:
        """Read-only accessor used by cart-add for post-add verification."""
        return self._read_quantity(self._ensure_page())

    def confirm_cart_state(self) -> str:
        page = self._ensure_page()
        try:
            page.goto("https://cart.taobao.com/cart.htm", wait_until="domcontentloaded", timeout=20000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self._handle_captcha_if_present(page)
            self._human_wait(1.5, 3)

            for sel in CART_ITEM_SELECTORS:
                with suppress(Exception):
                    locator = page.locator(sel).first
                    if locator.is_visible(timeout=3000):
                        count = page.locator(sel).count()
                        print(f"[browser] confirm cart state => success ({count} items)", file=sys.stderr)
                        return "success"

            if "cart.taobao.com" in page.url or "cart.tmall.com" in page.url:
                print("[browser] confirm cart state => empty (on cart page but no items)", file=sys.stderr)
                return "empty"
            else:
                print("[browser] confirm cart state => error (did not reach cart page)", file=sys.stderr)
                return "error"
        except Exception as e:
            print(f"[browser] confirm cart state => error: {e}", file=sys.stderr)
            return "error"

    def capture_evidence(self, name: str) -> str:
        page = self._ensure_page()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"[browser] capture evidence: {path}", file=sys.stderr)
        return str(path)

    # ──────────────────────────────────────────────
    # Visual Agent: Perception (Screenshot + DOM)
    # ──────────────────────────────────────────────

    def capture_viewport_screenshot(self, name: str, jpeg: bool = False) -> str | None:
        if not _SHOT_KEEP(name):
            return None
        page = self._ensure_page()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        # 批量取证用 jpeg：详情页 png 编码要 2–3s，jpeg(q=45) 约 0.3s，
        # 119 行就是省 4 分钟。压缩伪影对「人工核对买了啥」毫无影响。
        path = self.artifact_dir / (f"{name}.jpg" if jpeg else f"{name}.png")
        try:
            if jpeg:
                page.screenshot(path=str(path), full_page=False, timeout=8000,
                                type="jpeg", quality=45)
            else:
                page.screenshot(path=str(path), full_page=False, timeout=8000)
        except Exception as exc:  # a missing screenshot must never fail a step
            print(f"[browser] screenshot skipped for {name}: {exc}", file=sys.stderr)
            return None
        print(f"[browser] capture viewport screenshot: {path}", file=sys.stderr)
        return str(path)

    def get_page_text(self, max_chars: int = 2000) -> str:
        """Cheap page text for diagnostics / substring checks.

        innerText 需要整页布局计算，详情页上万节点时要几百毫秒；textContent
        没有换行但同样能拿到「全部商品(85)」「已加入购物车」这类关键片段，
        批量跑时快一个数量级。
        """
        page = self._ensure_page()
        with suppress(Exception):
            text = page.evaluate("""() => {
                const body = document.body;
                if (!body) return '';
                return (body.textContent || '').replace(/\\s+/g, ' ').trim();
            }""")
            if text:
                return text[:max_chars]
        return ""

    def get_page_text_rendered(self, max_chars: int = 2000) -> str:
        page = self._ensure_page()
        with suppress(Exception):
            text = page.evaluate("""() => {
                const body = document.body;
                if (!body) return '';
                const clone = body.cloneNode(true);
                clone.querySelectorAll('script, style, noscript, [hidden], [aria-hidden="true"]').forEach(el => el.remove());
                return (clone.innerText || '').trim();
            }""")
            if text:
                return text[:max_chars]
        return ""

    def get_visible_dom(self) -> dict[str, Any]:
        """Extract visible DOM with semantic annotations: buttons, links, inputs, SKU options.

        Returns a compact representation the AI can use alongside screenshots to identify
        clickable elements and understand page structure.
        """
        page = self._ensure_page()
        with suppress(Exception):
            return page.evaluate("""() => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > viewportH) return false;
                    if (rect.right < 0 || rect.left > viewportW) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                    // Check if obscured by another element at center
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl)) return false;
                    return true;
                }

                function getSemanticRole(el) {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'button' || role === 'button') return 'button';
                    if (tag === 'a' && el.getAttribute('href')) return 'link';
                    if (tag === 'input' && (type === 'text' || type === 'search')) return 'text_input';
                    if (tag === 'input' && type === 'submit') return 'button';
                    if (tag === 'select') return 'select';
                    if (tag === 'textarea') return 'text_input';
                    return tag;
                }

                function getSkuInfo(el) {
                    const cls = (el.getAttribute('class') || '').toLowerCase();
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
                    if (cls.includes('sku') || cls.includes('prop')) return 'sku_option';
                    if (cls.includes('valueitem') || cls.includes('skuvalue')) return 'sku_value';
                    return null;
                }

                const result = {url: window.location.href, title: document.title, elements: []};

                // Buttons
                document.querySelectorAll('button, [role="button"], a.btn, span.btn').forEach(el => {
                    if (!isVisible(el)) return;
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 80);
                    if (!text) return;
                    result.elements.push({
                        role: 'button',
                        text: text,
                        selector: el.id ? '#' + el.id : null,
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Links
                document.querySelectorAll('a[href]').forEach(el => {
                    if (!isVisible(el)) return;
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 80);
                    const href = el.getAttribute('href') || '';
                    if (!text && !href) return;
                    const rect = el.getBoundingClientRect();
                    result.elements.push({
                        role: 'link',
                        text: text,
                        href: href.slice(0, 120),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Inputs
                document.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(el => {
                    if (!isVisible(el)) return;
                    const rect = el.getBoundingClientRect();
                    const placeholder = el.getAttribute('placeholder') || '';
                    result.elements.push({
                        role: getSemanticRole(el),
                        placeholder: placeholder.slice(0, 60),
                        selector: el.id ? '#' + el.id : (el.name ? '[name="' + el.name + '"]' : null),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // SKU options (Taobao-specific)
                document.querySelectorAll('[class*="skuItem"], [class*="valueItem"], li[class*="sku"], .J_TSaleProp li, .tb-prop li').forEach(el => {
                    if (!isVisible(el)) return;
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 40);
                    if (!text || text.length > 40) return;
                    const rect = el.getBoundingClientRect();
                    const cls = el.getAttribute('class') || '';
                    const disabled = cls.includes('disabled') || cls.includes('out-of');
                    const selected = cls.includes('selected') || cls.includes('active') || cls.includes('isSelected');
                    result.elements.push({
                        role: 'sku_option',
                        text: text,
                        disabled: disabled,
                        selected: selected,
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                });

                // Price elements
                const priceSelectors = [
                    '#J_StrPr498', '.tm-price', '.tb-rmb-num',
                    '[class*="price"]:not([class*="price-"])',
                    '.tm-promo-price .tm-price', '.tb-item-price',
                    '.sku-price .price-value', '.J_original_price'
                ];
                for (const sel of priceSelectors) {
                    const el = document.querySelector(sel);
                    if (el && isVisible(el)) {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').trim().slice(0, 30);
                        if (text) {
                            result.elements.push({
                                role: 'price',
                                text: text,
                                selector: sel,
                                box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                            });
                        }
                    }
                }

                return result;
            }""")
        return {"url": "", "title": "", "elements": []}

    def get_sku_structure(self) -> list[dict[str, Any]]:
        """Extract SKU groups with labels and selectable values from the current page."""
        page = self._ensure_page()
        with suppress(Exception):
            page.wait_for_selector('[class*="sku"]', timeout=2500)
        with suppress(Exception):
            groups = page.evaluate("""() => {
                const groups = [];
                // Find SKU property groups (dl.prop structure or skuGroup containers)
                const groupEls = document.querySelectorAll('dl[class*="prop"], [class*="skuGroup"], .J_TSaleProp, [class^="skuItem--"]');
                for (const g of groupEls) {
                    const labelEl = g.querySelector('dt, [class*="label"], [class*="title"]');
                    const label = labelEl ? labelEl.textContent.trim().replace(/[:：]$/, '').trim() : '';
                    const values = [];
                    const valueEls = g.querySelectorAll('dd a, li a, [class*="valueItem"]:not([class*="label"]):not([class*="Text"]), [class^="valueItem--"]');
                    for (const v of valueEls) {
                        const text = (v.innerText || v.textContent || '').trim();
                        if (!text || text.length > 40) continue;
                        const cls = v.getAttribute('class') || '';
                        values.push({
                            text: text,
                            disabled: cls.includes('disabled') || cls.includes('out-of') || (v.getAttribute('data-disabled') === 'true'),
                            selected: cls.includes('selected') || cls.includes('active') || cls.includes('isSelected') || (v.getAttribute('aria-checked') === 'true')
                        });
                    }
                    if (label || values.length) groups.push({label, values});
                }
                return groups;
            }""")
            if groups:
                return groups
        return []

    def get_element_state(self, text: str) -> dict[str, Any] | None:
        """Check if an element with given text is visible, clickable, and not obscured."""
        page = self._ensure_page()
        with suppress(Exception):
            return page.evaluate("""(searchText) => {
                const candidates = [];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT,
                    {acceptNode: n => n.innerText && n.innerText.trim().includes(searchText) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP}
                );
                let node;
                while ((node = walker.nextNode()) && candidates.length < 10) {
                    const rect = node.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    candidates.push({
                        tag: node.tagName.toLowerCase(),
                        text: (node.innerText || '').trim().slice(0, 60),
                        visible: true,
                        clickable: topEl === node || node.contains(topEl),
                        box: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                    });
                }
                return candidates.length > 0 ? candidates[0] : null;
            }""", text)
        return None

    # ──────────────────────────────────────────────
    # Visual Agent: Atomic Actions
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # SKU Matching
    # ──────────────────────────────────────────────

    @staticmethod
    def _norm_sku_text(text: Any) -> str:
        """Normalize SKU text so 10K, 10kΩ, 10KΩ and 10kΩ±1% compare predictably."""
        value = unicodedata.normalize("NFKC", str(text or "")).lower()
        value = re.sub(r"\s+", "", value)
        value = value.replace("ω", "").replace("ohm", "")
        value = value.replace("±", "").replace("+/-", "")
        return value

    @classmethod
    def _sku_match_kind(cls, option: str, target: str) -> str:
        """Return 'exact', 'prefix' or '' for a normalized option/target pair."""
        option_n = cls._norm_sku_text(option)
        target_n = cls._norm_sku_text(target)
        if not target_n:
            return ""
        if option_n == target_n:
            return "exact"
        if option_n.startswith(target_n):
            rest = option_n[len(target_n):]
            if rest and rest[0].isdigit() and not re.match(r"\d+%", rest):
                return ""
            return "prefix"
        return ""

    def _choose_sku_hit(
        self,
        matches: list[dict[str, Any]],
        label_keyword: str,
        value_keyword: str,
    ) -> dict[str, Any] | None:
        """Pick exactly one visible, enabled match, or raise on ambiguity."""
        label_n = self._norm_sku_text(label_keyword)
        if not label_n:
            return None
        usable = [
            match
            for match in matches
            if match.get("visible")
            and not match.get("disabled")
            and self._sku_match_kind(match.get("text", ""), value_keyword)
            and (
                label_n in self._norm_sku_text(match.get("label", ""))
                or self._norm_sku_text(match.get("label", "")) in label_n
            )
        ]
        if not usable:
            return None
        if len(usable) > 1:
            raise SkuAmbiguousError(
                label_keyword,
                value_keyword,
                [match.get("text", "") for match in usable],
            )
        return usable[0]

    def _find_sku_matches(self, page: Page) -> list[dict[str, Any]]:
        with suppress(Exception):
            matches = page.evaluate("""() => {
                document.querySelectorAll('[data-dsh-sku-token]').forEach(el => el.removeAttribute('data-dsh-sku-token'));
                const out = [];
                let g = 0;
                for (const group of document.querySelectorAll('dl[class*="prop"], [class*="skuGroup"], .J_TSaleProp, [class^="skuItem--"]')) {
                    const labelEl = group.querySelector('dt, [class*="label"], [class*="title"]');
                    const label = labelEl ? labelEl.textContent.trim().replace(/[:：]$/, '').trim() : '';
                    let v = 0;
                    for (const el of group.querySelectorAll('dd a, li a, [class*="valueItem"]:not([class*="label"]):not([class*="Text"]), [class^="valueItem--"]')) {
                        const text = (el.innerText || el.textContent || '').trim();
                        if (!text || text.length > 40) { v++; continue; }
                        const token = 'g' + g + 'v' + v;
                        el.setAttribute('data-dsh-sku-token', token);
                        const rect = el.getBoundingClientRect();
                        const cls = el.getAttribute('class') || '';
                        out.push({
                            token: token,
                            label: label,
                            text: text,
                            disabled: cls.includes('disabled') || cls.includes('out-of') || (el.getAttribute('data-disabled') === 'true'),
                            selected: cls.includes('selected') || cls.includes('isSelected') || cls.includes('active'),
                            visible: rect.width > 0 && rect.height > 0,
                        });
                        v++;
                    }
                    g++;
                }
                return out;
            }""")
            if isinstance(matches, list):
                return matches
        return []

    def _click_sku_token(self, page: Page, token: str) -> bool:
        with suppress(Exception):
            clicked = page.evaluate("""(token) => {
                const el = document.querySelector('[data-dsh-sku-token="' + token + '"]');
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                el.removeAttribute('data-dsh-sku-token');
                return true;
            }""", token)
            return bool(clicked)
        return False

    def _expand_sku_lists(self, page: Page) -> bool:
        """展开被折叠的 SKU 长列表。

        淘宝详情页一个 SKU 组超过 ~12 个值时会折叠成「展开/更多」，被折叠的值
        根本不在 DOM 里 —— 于是「120Ω 明明有货」被判定成「这家没这个档」，
        整行白跑还换店重来。点一次展开再重扫，成本 ~0.3s。
        """
        with suppress(Exception):
            n = page.evaluate(
                """() => {
                  const re = /^(展开|更多|查看全部|展开更多|展开全部|全部)$/;
                  let hit = 0;
                  for (const el of document.querySelectorAll('span,div,a,i,button')) {
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 8 || !re.test(t)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    if (r.top < 0 || r.top > window.innerHeight) continue;
                    try { el.click(); hit++; } catch (e) {}
                    if (hit >= 4) break;
                  }
                  return hit;
                }"""
            )
            if n:
                print(f"[browser] expand sku lists: clicked={n}", file=sys.stderr)
                self._human_wait(0.15, 0.3)
                return True
        return False

    def select_sku(self, label_keyword: str, value_keyword: str) -> bool:
        """Select a SKU option using normalized exact/prefix matching.

        Raises SkuAmbiguousError when more than one option matches, instead of
        silently clicking the first one.
        """
        page = self._ensure_page()
        matches = self._find_sku_matches(page)
        hit = self._choose_sku_hit(matches, label_keyword, value_keyword)
        if hit is not None and hit.get("selected"):
            print(
                f"[browser] select_sku: already selected label='{hit['label']}' value='{hit['text']}'",
                file=sys.stderr,
            )
            return True
        if hit is None:
            # 值被折叠起来时先展开再找一次，别急着判「这家没这个档」
            if self._expand_sku_lists(page):
                matches = self._find_sku_matches(page)
                hit = self._choose_sku_hit(matches, label_keyword, value_keyword)
            if hit is None:
                print(f"[browser] select_sku: no match for label='{label_keyword}' value='{value_keyword}'", file=sys.stderr)
                return False
        clicked = self._click_sku_token(page, hit["token"])
        if clicked:
            self._human_wait(0.5, 1.0)
            print(f"[browser] select_sku: clicked label='{hit['label']}' value='{hit['text']}'", file=sys.stderr)
        return clicked

    def select_skus(self, selections: list[dict[str, str]]) -> tuple[bool, list[dict[str, Any]]]:
        """Select multiple SKU options sequentially.

        Args:
            selections: [{"label": "芯片", "value": "M4"}, {"label": "内存", "value": "16G"}, ...]

        Returns:
            (all_ok, results) where results = [{"label":"...", "value":"...", "ok": True/False}, ...]
        """
        results: list[dict[str, Any]] = []
        for sel in selections:
            label = sel.get("label", "")
            value = sel.get("value", "")
            ok = self.select_sku(label, value)
            results.append({"label": label, "value": value, "ok": ok})
            if ok:
                self._human_wait(0.8, 1.5)
        all_ok = all(r["ok"] for r in results)
        return all_ok, results

    def click_element_by_text(self, text: str, role: str = "any") -> bool:
        """Find and click a visible element containing the given text."""
        page = self._ensure_page()
        # Try Playwright locator first (handles shadow DOM, iframes)
        with suppress(Exception):
            locator = page.locator(f"text={text}").first
            if locator.is_visible(timeout=3000):
                self._human_click(page, locator)
                self._human_wait(0.3, 0.8)
                return True
        # Try role-specific locators
        roles = [role] if role != "any" else ["button", "link"]
        for r in roles:
            with suppress(Exception):
                locator = page.locator(f"{r}:has-text('{text}')").first
                if locator.is_visible(timeout=2000):
                    self._human_click(page, locator)
                    self._human_wait(0.3, 0.8)
                    return True
        # Fallback: evaluate click by text match with proper visibility check
        with suppress(Exception):
            clicked = page.evaluate("""(t) => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > viewportH) return false;
                    if (rect.right < 0 || rect.left > viewportW) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    if (parseFloat(style.opacity) === 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl)) return false;
                    return true;
                }

                const interactiveSelectors = 'button, a[href], input, select, textarea, [role="button"], [role="link"], [onclick]';
                const els = document.querySelectorAll(interactiveSelectors);
                for (const el of els) {
                    if ((el.innerText || el.textContent || '').trim().includes(t)) {
                        if (!isVisible(el)) continue;
                        el.scrollIntoView({behavior: "instant", block: "center"});
                        el.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
                        el.dispatchEvent(new MouseEvent("mouseup", {bubbles: true, cancelable: true}));
                        el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
                        return true;
                    }
                }
                return false;
            }""", text)
            if clicked:
                self._human_wait(0.3, 0.8)
                return True
        return False

    def scroll_page(self, direction: str = "down", amount: int = 500,
                    selector: str | None = None) -> None:
        """Scroll the page or a specific container. direction: 'up' or 'down'."""
        page = self._ensure_page()
        sign = -1 if direction == "up" else 1
        if selector:
            with suppress(Exception):
                page.evaluate(
                    """(s, amt) => {
                        const el = document.querySelector(s);
                        if (el) { el.scrollBy(0, amt); return true; }
                        return false;
                    }""",
                    [selector, sign * amount]
                )
        else:
            self._human_scroll(page, page.evaluate("window.scrollY") + sign * amount)

    def wait_for_element(self, text: str | None = None, selector: str | None = None,
                         timeout_ms: int = 10000) -> bool:
        """Wait for an element to appear on the page."""
        page = self._ensure_page()
        if selector:
            with suppress(Exception):
                page.wait_for_selector(selector, timeout=timeout_ms)
                return True
        if text:
            with suppress(Exception):
                page.wait_for_selector(f"text={text}", timeout=timeout_ms)
                return True
        return False

    # ──────────────────────────────────────────────
    # Human-Like Behavior: Bezier Mouse Movement
    # ──────────────────────────────────────────────

    def _bezier_curve(self, start: tuple[float, float], end: tuple[float, float], steps: int = 20) -> list[tuple[float, float]]:
        """Generate a cubic Bezier curve path from start to end with random control points."""
        sx, sy = start
        ex, ey = end
        dist = math.hypot(ex - sx, ey - sy)

        # Random control points — create natural curvature
        jitter = max(dist * 0.3, 30)
        cp1 = (
            sx + (ex - sx) * random.uniform(0.1, 0.4) + random.uniform(-jitter, jitter),
            sy + (ey - sy) * random.uniform(0.1, 0.4) + random.uniform(-jitter, jitter),
        )
        cp2 = (
            sx + (ex - sx) * random.uniform(0.6, 0.9) + random.uniform(-jitter, jitter),
            sy + (ey - sy) * random.uniform(0.6, 0.9) + random.uniform(-jitter, jitter),
        )

        points = []
        for i in range(steps + 1):
            t = i / steps
            # Ease-in-out: accelerate then decelerate
            t_eased = t * t * (3 - 2 * t)

            x = (1 - t_eased) ** 3 * sx + 3 * (1 - t_eased) ** 2 * t_eased * cp1[0] + 3 * (1 - t_eased) * t_eased ** 2 * cp2[0] + t_eased ** 3 * ex
            y = (1 - t_eased) ** 3 * sy + 3 * (1 - t_eased) ** 2 * t_eased * cp1[1] + 3 * (1 - t_eased) * t_eased ** 2 * cp2[1] + t_eased ** 3 * ey

            # Add slight random jitter
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)

            points.append((x, y))

        return points

    def _human_click(self, page: Page, locator, timeout: int = 5000) -> bool:
        """Click a locator with human-like mouse movement.

        返回 True = 点击**确实落在目标元素上**；False = 没点成（禁用/被遮挡/看不见），
        调用方可以据此重试或报错，而不是傻等确认信号超时。

        2026-11-09 复盘：加购「静默失败」的三个根因一次性堵在这里 ——
          1) bounding_box 给的是视口坐标：按钮在折叠线以下时按坐标点会打到别的
             元素（或页面空白），没有 XHR、没有 toast、角标不动，看起来就像
             「点了但没加上」。先 scroll_into_view 再取坐标。
          2) 按钮禁用（规格没选全 / 无货）时点击毫无反应：先判 is_enabled。
          3) 优惠券弹层/引导遮罩盖住按钮：用 elementFromPoint 做命中测试，
             不对就换个点再试，都不行就返回 False 交上层处理。
        """
        _t0 = time.monotonic()
        with suppress(Exception):
            locator.scroll_into_view_if_needed(timeout=timeout)
        with suppress(Exception):
            if not locator.is_enabled():
                print("[browser] hclick disabled target", file=sys.stderr)
                return False
        try:
            box = locator.bounding_box(timeout=timeout)
            _t1 = time.monotonic()
            print(f"[browser] hclick box={_t1 - _t0:.2f}s", file=sys.stderr)
            if box is None:
                locator.click(timeout=timeout)
                print(f"[browser] hclick fallback={time.monotonic() - _t1:.2f}s", file=sys.stderr)
                return True

            # Click at a random point within the element (not always center)
            target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

            # 命中测试：点必须真的落在目标上（含其子元素），否则换点再试。
            def _hits(px: float, py: float) -> bool:
                try:
                    return bool(
                        locator.evaluate(
                            "(el, pt) => { const e = document.elementFromPoint(pt[0], pt[1]);"
                            " return !!(e && (e === el || el.contains(e) || e.contains(el))); }",
                            [px, py],
                        )
                    )
                except Exception:  # noqa: BLE001
                    return False

            picked = None
            for px, py in (
                (target_x, target_y),
                (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2),
                (box["x"] + 4, box["y"] + box["height"] / 2),
                (box["x"] + box["width"] - 4, box["y"] + box["height"] / 2),
            ):
                if _hits(px, py):
                    picked = (px, py)
                    break
            if picked is None:
                print("[browser] hclick blocked by overlay", file=sys.stderr)
                return False
            target_x, target_y = picked

            # 原来按贝塞尔曲线逐点移动 15–30 步模拟人手：每次 mouse.move 都要
            # 触发一次页面命中测试，在淘宝详情页上一次约 1 秒 —— 单是这段就 24 秒，
            # 占了整行耗时 70%。风控看的是行为轨迹的频率与节奏，不是鼠标中间点数量；
            # 改成两次移动（先到中点再到目标）保留一点轨迹，成本降到 ~2 次事件。
            #
            # 2026-11-08 实测（P0-1 复测）：上一行刚点过按钮，鼠标**已经在**目标
            # 位置上时，`mouse.move(同一坐标)` 不产生 pointermove 事件 —— 淘宝的
            # 「加入购物车」对这种零位移点击直接静默丢弃：没有 XHR、没有 toast、
            # 角标不动。日志特征就是 `hclick move=0.00s`。20 行里 11 行零位移，
            # 11 行全部加购失败；有位移的 9 行成功 7 行。
            # 所以先挪到目标旁边的随机一点（保证pointer真的动），再移回目标点。
            away_x = target_x + random.choice((-1, 1)) * random.randint(60, 180)
            away_y = target_y + random.choice((-1, 1)) * random.randint(40, 120)
            page.mouse.move(max(1.0, away_x), max(1.0, away_y))
            time.sleep(random.uniform(0.04, 0.10))
            page.mouse.move(target_x, target_y, steps=3)

            _t2 = time.monotonic()
            # Small pause before clicking
            time.sleep(random.uniform(0.05, 0.15))
            page.mouse.click(target_x, target_y)
            print(
                f"[browser] hclick move={_t2 - _t1:.2f}s click={time.monotonic() - _t2:.2f}s",
                file=sys.stderr,
            )
            return True

        except Exception:
            # Fallback to standard click（Playwright 自带可操作性检查与自动滚动）
            try:
                locator.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                return False

    def _human_type(self, page: Page, text: str) -> None:
        """Type text with human-like variable delays and occasional pauses."""
        for i, char in enumerate(text):
            page.keyboard.type(char)

            # Variable delay between keystrokes
            base_delay = random.uniform(0.03, 0.12)

            # Occasional longer pause (simulating thinking)
            if random.random() < 0.08:
                base_delay = random.uniform(0.2, 0.5)

            time.sleep(base_delay)

    def _human_scroll(self, page: Page, target_y: int = 800) -> None:
        """Scroll the page with human-like variable speed and slight back-scrolling."""
        if not BROWSE_SIM:
            # Batch mode: one positioned jump is enough to trigger lazy load.
            with suppress(Exception):
                page.evaluate(f"window.scrollTo(0, {int(target_y)})")
            return
        current_y = page.evaluate("window.scrollY")
        remaining = target_y - current_y

        while abs(remaining) > 50:
            # Variable scroll increment
            step = min(abs(remaining), random.randint(80, 350))
            direction = 1 if remaining > 0 else -1

            # Occasionally scroll back slightly
            if random.random() < 0.15 and abs(remaining) > 200:
                step = -random.randint(20, 60)
                direction = -direction

            page.evaluate(f"window.scrollBy(0, {step * direction})")
            time.sleep(random.uniform(0.3, 1.0))

            current_y = page.evaluate("window.scrollY")
            remaining = target_y - current_y

    def _human_wait(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        """Wait a random duration to simulate human pause (scaled by SPEED)."""
        time.sleep(random.uniform(min_s, max_s) * SPEED)

    def _random_mouse_move(self, page: Page) -> None:
        """Move mouse to a random viewport position (simulating user looking around)."""
        viewport = page.viewport_size
        if viewport is None:
            return
        target_x = random.randint(100, viewport["width"] - 100)
        target_y = random.randint(200, viewport["height"] - 200)
        start_x = random.randint(200, 600)
        start_y = random.randint(200, 500)
        points = self._bezier_curve(
            (start_x, start_y), (target_x, target_y), steps=random.randint(10, 20)
        )
        for px, py in points:
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.008, 0.025))

    def _simulate_browsing(self, page: Page, max_scroll: int = 1200) -> None:
        """Simulate a user browsing the page: scroll in stages, pause, look around."""
        if not BROWSE_SIM:
            self._human_scroll(page, random.randint(200, max(300, max_scroll // 2)))
            self._human_wait(0.2, 0.4)
            return
        scroll_targets = [
            random.randint(200, 400),
            random.randint(500, 800),
            random.randint(800, max_scroll),
        ]
        for target in scroll_targets:
            self._human_scroll(page, target)
            self._human_wait(0.5, 1.5)
            if random.random() < 0.4:
                self._random_mouse_move(page)
        self._human_scroll(page, random.randint(0, 300))
        self._human_wait(0.3, 1)

    # ──────────────────────────────────────────────
    # Access Control Detection
    # ──────────────────────────────────────────────

    def _wait_for_access_recovery(self, page: Page) -> None:
        if not self._looks_access_blocked(page):
            return
        check_interval = 30
        max_checks = 7
        for i in range(max_checks):
            time.sleep(check_interval)
            total_slept = (i + 1) * check_interval
            print(f"[browser] 等待手动验证通过... 第 {i+1}/{max_checks} 次检测 (已等待 {total_slept}s)", file=sys.stderr)
            if not self._looks_access_blocked(page):
                print(f"[browser] 验证通过! (等待约 {total_slept}s)", file=sys.stderr)
                return

    def _looks_access_blocked(self, page: Page) -> bool:
        """真验证码才拦。

        判定依据（按可靠性排序）：滑块按钮/弹层元素（geetest 或淘宝 baxia）→ 明确的
        滑块文案 → 标题关键词。**不能**用「页面里出现『验证』两个字」：商品页的
        「正品验证」「验证码」会把整个批量误判成撞验证并卡死。
        """
        selectors = list(CAPTCHA_PANEL_SELECTORS) + list(CAPTCHA_SLIDER_BTN_SELECTORS)
        try:
            hit = page.evaluate(
                """(sels) => sels.some((s) => {
                    try {
                        return Array.from(document.querySelectorAll(s)).some((el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0
                                && st.visibility !== 'hidden' && st.display !== 'none';
                        });
                    } catch (e) { return false; }
                })""",
                selectors,
            )
            if hit:
                return True
        except Exception:
            pass
        # 淘宝的验证弹层在 iframe 里（punish / baxia），主文档的 text= 选择器
        # 和 class 选择器都看不到它 —— 实测滑块明明糊在屏幕上，verification_pending()
        # 却回 False，于是批量不暂停、继续拿「0 条结果」去撞下一行，白烧一整轮。
        with suppress(Exception):
            for frame in page.frames:
                fu = (frame.url or "").lower()
                if "punish" in fu or "baxia" in fu or "captcha" in fu or "x5secdata" in fu:
                    # 2026-11-08 复测：验证帧在挑战解除后**不会立刻从 DOM 消失**。
                    # 主文档已经出商品卡、且上面那圈「可见弹层」探测没命中，
                    # 就说明只是残留帧 —— 再当拦截会让批量反复「刷新无效」白等。
                    if not self._main_has_cards(page):
                        return True
                # 风控中间页：淘宝把接口请求重定向到 _____tmd_____ 下面，
                # 页面本身不弹窗、只是永远「加载中…」。这是最隐蔽的一种拦截，
                # 不认出来就会当成「首屏没渲染」反复换词，一条行白烧一两分钟。
                if self._TMD_MARK in fu:
                    # P2-1：惩罚解除后 tmd iframe 有时残留在 DOM 里。主文档已经
                    # 数得出商品卡就说明接口恢复了，不能再当拦截 —— 否则
                    # verification_pending() 恒 True，批量原地白等满 30 分钟。
                    if not self._main_has_cards(page):
                        return True
        for selector in ACCESS_BLOCKED_SIGNALS:
            with suppress(Exception):
                if page.locator(selector).first.is_visible():
                    return True
        title = ""
        with suppress(Exception):
            title = page.title().lower()
        return "access denied" in title or "安全验证" in title or "异常流量" in title

    # ── 验证自恢复（2026-11-08 规则变更）────────────────────────────────
    # 旧规则：一见到验证（滑块/图片点选/风控中间页）就交回人工，整批挂着等 30 分钟。
    # 新规则：**先自己刷一下**。淘宝这类验证绝大多数是「这一个请求被扣了」，
    # 刷新一次就恢复，出商品就接着干；只有刷新完仍然没有商品，才算真被拦住、才要人工。
    RECOVER_TRIES = int(os.environ.get("TAOBAO_RECOVER_TRIES", "2"))
    RECOVER_PAUSE_S = float(os.environ.get("TAOBAO_RECOVER_PAUSE", "1.2"))

    def recover_from_verification(self, page: Page | None = None,
                                  tries: int | None = None) -> bool:
        """刷新当前页把验证刷掉。True = 已恢复（可以接着干）。

        判定「恢复」的两个条件缺一不可：
          1. 页面上再看不到验证弹层/中间页帧；
          2. 若是搜索页，主文档能数出商品卡（刷新后仍无商品 = 没恢复，见下）。
        刷不出来就返回 False，由调用方升级为「需要人工验证」。
        """
        page = page or self._ensure_page()
        tries = self.RECOVER_TRIES if tries is None else tries
        for attempt in range(1, max(1, tries) + 1):
            before = page.url or ""
            try:
                if attempt == 1:
                    page.reload(wait_until="commit", timeout=25000)
                else:
                    # 第二次改成「整页重新导航」而不是 reload：实测卡在
                    # `_____tmd_____` 时 reload 会把同一个坏页面重新拿回来，
                    # 换成 goto 才会重新走一次完整请求。
                    page.goto(before, wait_until="commit", timeout=25000)
            except Exception:
                with suppress(Exception):
                    page.goto(before, wait_until="commit", timeout=25000)
            # 刷新后给页面一点时间重新渲染（别用 networkidle：结果页有埋点长连接）
            self._human_wait(self.RECOVER_PAUSE_S * 0.6, self.RECOVER_PAUSE_S)
            with suppress(Exception):
                page.wait_for_load_state("domcontentloaded", timeout=6000)
            blocked = True
            with suppress(Exception):
                blocked = self._looks_access_blocked(page) or self.risk_middleware_hit()
            is_search = "search" in (page.url or "")
            # 2026-11-08 复测修正：刚 reload 完就**瞬时**数卡会把「还在渲染」
            # 误判成「刷新后仍无商品」→ 立刻升级人工。这里要给页面一次机会，
            # 用 wait_for_cards 等 4 s；明确空结果（-2）也算正常页面。
            if not blocked and is_search:
                state = self.wait_for_cards(timeout_ms=4000, settle_ms=1500)
                has_cards = state >= 0 or state == -2
                if state == -3:
                    blocked = True
            else:
                has_cards = True
            if not blocked and has_cards:
                print(
                    f"[browser] verification cleared by refresh (try {attempt}/{tries})",
                    file=sys.stderr,
                )
                return True
            print(
                f"[browser] refresh {attempt}/{tries} did not help "
                f"(blocked={blocked} cards={has_cards})",
                file=sys.stderr,
            )
            if attempt < tries:
                self._human_wait(1.5, 2.8)
        return False

    def _recover_cards(self, page: Page, timeout_ms: int) -> int:
        """中间页/验证之后：刷新一次再数商品卡。刷不出来返回 -3（=仍要人工）。"""
        if not self.recover_from_verification(page):
            return -3
        return self.wait_for_cards(timeout_ms=timeout_ms)

    def _handle_captcha_if_present(self, page: Page) -> None:
        """检测到验证：先刷新自恢复，恢复不了才交回人工。

        2026-11-08 规则：验证不再是「一碰就停」的硬停点 —— 只有「页面刷新后
        仍然出不来商品」才需要人工。能用刷新刷掉的（绝大多数）直接继续干。
        """
        if not self._looks_access_blocked(page):
            return
        print("[browser] verification detected → self-recovery (refresh)", file=sys.stderr)
        if self.recover_from_verification(page):
            print("[browser] recovered, continuing without human", file=sys.stderr)
            return
        print("[browser] still no products after refresh → manual takeover needed", file=sys.stderr)
        raise VerificationRequired()

    def verification_pending(self) -> bool:
        """True while the current page still shows CAPTCHA/verification."""
        return self._looks_access_blocked(self._ensure_page())

    # ──────────────────────────────────────────────
    # DOM Helpers
    # ──────────────────────────────────────────────

    def _find_first_visible_locator(self, page: Page, selectors: Iterable[str]):
        for selector in selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if locator.is_visible():
                    return locator
        return None

    def _find_candidate_links(self, page: Page) -> list[dict[str, str]]:
        with suppress(Exception):
            results = page.evaluate("""(selectors, cardSelectors) => {
                const results = [];
                const seen = new Set();
                for (const sel of selectors) {
                    const anchors = document.querySelectorAll(sel);
                    for (const a of anchors) {
                        const href = (a.getAttribute('href') || '').trim();
                        if (!href || seen.has(href) || seen.size >= 16) continue;
                        seen.add(href);
                        const text = (a.innerText || '').trim();
                        const title = text.split('\\n')[0].trim();
                        if (!title) continue;
                        let card = null;
                        for (const cs of cardSelectors) {
                            card = a.closest(cs);
                            if (card) break;
                        }
                        if (!card) {
                            let p = a.parentElement;
                            for (let i = 0; i < 3 && p; i++) { p = p.parentElement; }
                            card = p || a.parentElement;
                        }
                        // card.innerText 要上万节点做布局计算，26 张卡就是 1 秒；
                        // textContent 便宜一个数量级，价格/销量/包邮这些正则照样命中。
                        const cardText = card ? (card.textContent || '').trim() : text;
                        results.push({ href, text, title, card_text: cardText });
                        if (results.length >= 16) break;
                    }
                    if (results.length > 0) break;
                }
                return results;
            }""", [PRODUCT_LINK_SELECTORS, PRODUCT_CARD_CLIMB_SELECTORS])
            if isinstance(results, list) and results:
                print(f"[browser] found {len(results)} candidate links via evaluate", file=sys.stderr)
                return results

        # Fallback to Playwright locators
        links: list[dict[str, str]] = []
        for selector in PRODUCT_LINK_SELECTORS:
            with suppress(Exception):
                for locator in page.locator(selector).all()[:40]:
                    with suppress(Exception):
                        href = locator.get_attribute("href") or ""
                        text = (locator.inner_text() or "").strip()
                        title = text.split("\n")[0].strip() if text else ""
                        if href and title:
                            links.append({"href": href, "text": text, "title": title, "card_text": text})
                if links:
                    break
        return links

    # ──────────────────────────────────────────────
    # Text Processing
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # Search Result Card Field Extractors
    # ──────────────────────────────────────────────

    def _extract_price(self, card_text: str) -> tuple[str | None, float | None]:
        patterns = [
            r'¥\s*([\d,]+(?:\.\d{1,2})?)',
            r'￥\s*([\d,]+(?:\.\d{1,2})?)',
            r'([\d,]+(?:\.\d{1,2})?)\s*元',
            r'([\d,]+(?:\.\d{1,2})?)\s*起',
            r'价格[：:]\s*([\d,]+(?:\.\d{1,2})?)',
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text)
            if match:
                raw = match.group(1).replace(",", "")
                try:
                    val = float(raw)
                    if 0.01 <= val <= 999999:
                        return f"¥{val:.2f}", val
                except ValueError:
                    continue

        # Try ranges like "299-499"
        range_match = re.search(r'¥?\s*([\d,]+(?:\.\d{1,2})?)\s*[-~—]\s*¥?\s*([\d,]+(?:\.\d{1,2})?)', card_text)
        if range_match:
            try:
                low = float(range_match.group(1).replace(",", ""))
                return f"¥{low:.2f}", low
            except ValueError:
                pass
        return None, None

    def _extract_sales_count(self, card_text: str) -> int | None:
        patterns = [
            r'([\d.]+)\s*万?\+\s*人付款',
            r'([\d.]+)万\+?\s*人付款',
            r'月销\s*([\d.]+)\s*万?\s*',
            r'已售\s*([\d.]+)\s*万?\s*',
            r'销量\s*([\d.]+)\s*万?\s*',
            r'收货\s*([\d.]+)\s*万?\s*',
            r'付款\s*([\d.]+)\s*万?\s*',
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text)
            if match:
                raw = match.group(1)
                try:
                    val = float(raw)
                except ValueError:
                    continue
                # Check for "万" unit
                span = card_text[match.start():match.end()]
                if "万" in span:
                    val *= 10000
                return int(val)

        # Pattern: "1000+人付款" or "1.2万人付款"
        pattern2 = r'([\d.]+)\s*w?\s*人付款'
        match = re.search(pattern2, card_text)
        if match:
            try:
                val = float(match.group(1))
                if "万" in card_text[max(0, match.start()-5):match.end()]:
                    val *= 10000
                return int(val)
            except ValueError:
                pass
        return None

    def _check_free_shipping(self, card_text: str) -> bool:
        return bool(re.search(r'包邮|免邮|运费\s*[0０]|免运费', card_text))

    def _check_is_tmall(self, url: str, card_text: str) -> bool:
        if "tmall.com" in url.lower():
            return True
        return bool(re.search(r'天猫|Tmall|TMALL', card_text))

    # ──────────────────────────────────────────────

    def _build_keyword_tokens(self, keyword: str) -> list[str]:
        normalized = "".join(keyword.split())
        if len(normalized) < 2:
            return [normalized] if normalized else []
        tokens = {normalized}
        for index in range(len(normalized) - 1):
            chunk = normalized[index : index + 2]
            if len(chunk) == 2:
                tokens.add(chunk)
        return [token for token in tokens if token]

    def _matches_keyword(self, title: str, text: str, tokens: list[str]) -> bool:
        haystack = f"{title}\n{text}"
        return any(token in haystack for token in tokens)

    def _extract_rating(self, text: str) -> float | None:
        patterns = [
            r"好评率\s*([\d.]+)%",
            r"([\d.]+)%\s*好评",
            r"好评\s*([\d.]+)%",
            r"好评率[：:]\s*([\d.]+)%",
            r"好评率[：:]\s*([\d.]+)(?![.\d])",
            r"好评[率]?[：:]\s*([\d.]+)%",
            r"用户评价[：:]\s*([\d.]+)%",
            r"([\d.]+)%\s*[正好评]",
            r"好评.*?([\d.]+)%",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    val = float(match.group(1))
                    if 0 < val <= 1.0:
                        return val
                    elif val > 1.0:
                        return val / 100.0
                except ValueError:
                    continue
        return None
