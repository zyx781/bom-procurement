#!/usr/bin/env python3
"""Taobao browser automation — session mode.

Launches a persistent browser session that accepts JSON commands via stdin
and returns JSON results via stdout. The browser stays alive across commands,
eliminating repeated startup overhead.

Usage:
    python scripts/taobao.py session [--task-id ID] [--headless]
    python scripts/taobao.py check-session [--session-state-path PATH]
    python scripts/taobao.py clear-session [--session-state-path PATH]

Session protocol (stdin → stdout, one JSON per line):
    → {"cmd": "search", "keyword": "MacBook Air M4"}
    ← {"status": "success", "data": {"items": [...], "items_count": 8}, ...}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def _item_id(url: str) -> str:
    """从商品 URL 里取商品 id（item.taobao.com/item.htm?id=... / detail.tmall.com）。"""
    m = re.search(r"[?&](?:id|itemId)=(\d+)", url or "")
    return m.group(1) if m else ""


def _same_item_url(current: str, target: str) -> bool:
    a, b = _item_id(current), _item_id(target)
    if a and b:
        return a == b
    # 没有 id 参数时退化成「同域 + 同路径」
    ca, cb = urlparse(current or ""), urlparse(target or "")
    return bool(ca.netloc) and ca.netloc == cb.netloc and ca.path == cb.path


def _plan_cart_removal(rows: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]:
    """决定「只删本工具加购的那几件」时，购物车里哪些行该删、哪些该留。

    rows    购物车行（cart-view 的 rows：index / title / qty / url）
    targets 要删的目标（item_id / title / qty）；qty 是当时加购的份数

    返回 {"delete": [...], "skip": [...], "missing": [...]}

    1. 有 item_id 就按 id 精确匹配，同款不同卖家不会混；老记录没存 url，
       退回标题归一化包含匹配。
    2. **数量守卫**：车里这行的件数多于本工具当时加的份数，说明用户自己也加过同款，
       跳过不删并把原因写进 skip —— 这是「不动我其他东西」的关键。
    3. 匹配不到的目标进 missing，不静默吞掉。
    """
    def norm(text: Any) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        iid = _item_id(r.get("url") or "")
        if iid:
            by_id.setdefault(iid, r)

    delete: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    taken: set[Any] = set()

    for t in targets:
        tid = str(t.get("item_id") or "").strip()
        want_qty = int(t.get("qty") or 0)
        row = by_id.get(tid) if tid else None
        occupied = row is not None and row.get("index") in taken
        if occupied:
            row = None

        if row is None and not tid:
            nt = norm(t.get("title"))
            if nt:
                for r in rows:
                    rt = norm(r.get("title"))
                    # 两边都可能被淘宝改写，用「互相包含」或前缀一致做兜底
                    if not (rt and (nt in rt or rt in nt or (len(nt) >= 12 and nt[:12] == rt[:12]))):
                        continue
                    if r.get("index") in taken:
                        # 同款买过多次时购物车会合并成一行：不是丢件，是同一行
                        occupied = True
                        continue
                    row = r
                    break

        if row is None:
            if occupied:
                merged.append({"item_id": tid, "title": str(t.get("title") or "")[:60]})
            else:
                missing.append({"item_id": tid, "title": str(t.get("title") or "")[:60]})
            continue

        taken.add(row.get("index"))
        got_qty = int(row.get("qty") or 0)
        entry = {
            "item_id": _item_id(row.get("url") or "") or tid,
            "title": str(row.get("title") or "")[:80],
            "row_qty": got_qty,
            "expect_qty": want_qty,
        }
        if want_qty and got_qty > want_qty:
            entry["reason"] = (
                f"车里这行有 {got_qty} 件，本工具只加了 {want_qty} 件，"
                "可能你自己也加过同款"
            )
            skip.append(entry)
        else:
            delete.append(entry)

    return {"delete": delete, "skip": skip, "missing": missing, "merged": merged}


# 加购确认策略：fast（默认，点击即记账）/ strict（每行都开购物车页对账）
CART_VERIFY_STRICT = (os.environ.get("TAOBAO_CART_VERIFY") or "fast").strip().lower() in (
    "strict", "1", "true", "on",
)

_SCRIPTS_DIR = Path(__file__).parent.resolve()
_DEFAULT_SESSION_PATH = ".cache/taobao-search-skill/taobao-session.json"
_DEFAULT_ARTIFACT_DIR = ".cache/taobao-search-skill/artifacts"

# Real stdout reference — set in main() before redirecting sys.stdout → stderr.
_JSON_OUT = sys.stdout


def _idle_timeout_ms() -> int:
    """Cap for networkidle waits (same env knob as browser_adapter)."""
    import os
    try:
        return int(float(os.environ.get("TAOBAO_IDLE_TIMEOUT") or 10) * 1000)
    except ValueError:
        return 10000


IDLE_TIMEOUT_MS = _idle_timeout_ms()


def _resolve_path(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = _SCRIPTS_DIR / path
    return path


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────


def _output_json(data: dict[str, Any]) -> None:
    """Write JSON to the real stdout (preserved before redirect)."""
    with suppress(BrokenPipeError):
        json.dump(data, _JSON_OUT, ensure_ascii=False, indent=2)
        _JSON_OUT.write("\n")
        _JSON_OUT.flush()


def _log(msg: str) -> None:
    """Write diagnostic messages to stderr so they never pollute JSON stdout."""
    print(f"[taobao] {msg}", file=sys.stderr)


# ──────────────────────────────────────────────
# Session Handler
# ──────────────────────────────────────────────


class SessionHandler:
    """Persistent browser session that processes JSON commands from stdin."""

    def __init__(
        self,
        task_id: str,
        session_state_path: str = _DEFAULT_SESSION_PATH,
        headless: bool = False,
    ) -> None:
        self.task_id = task_id
        self.session_state_path = session_state_path
        self.headless = headless

        from browser_adapter import BrowserAdapter
        from session_flow import SessionFlow
        from session_manager import SessionManager

        self.browser = BrowserAdapter(
            headless=headless,
            artifact_dir=_DEFAULT_ARTIFACT_DIR,
        )
        session_manager = SessionManager(session_state_path)
        self.session_flow = SessionFlow(self.browser, session_manager)

        # Session state (in-memory, no file serialization)
        self.items: list[dict[str, Any]] = []
        self.current_index: int | None = None
        self.sku_groups: list[dict[str, Any]] = []
        self.last_selections: list[dict[str, str]] = []
        self._verification_blocked = False

    # 这些命令自带导航（cart.taobao.com），与搜索结果接口的风控无关，
    # 因此即使搜索被验证拦住也允许执行。
    _CART_SAFE_CMDS = frozenset({"cart-view", "cart-remove"})

    def run(self) -> None:
        """Main loop: read JSON commands from stdin, execute, write results."""
        from browser_adapter import VerificationRequired

        _log(f"session starting (task_id={self.task_id})")

        try:
            self.browser.open()
            self.session_flow.try_restore()
            self.browser.navigate_to_taobao()

            if not self.browser.is_logged_in():
                _log("login required — waiting for user")
                self.browser.ensure_login(manual_approval_required=True, force_manual=True)
                if self.browser.is_logged_in():
                    self.session_flow.capture_after_login()
                    _log("login successful, session captured")
                else:
                    _log("login failed or timed out")
        except VerificationRequired:
            # No unsolicited stdout line here: the next command gets
            # VERIFICATION_REQUIRED until the user clears the page.
            self._verification_blocked = True
            _log("verification required at startup — complete it in the browser, then retry")

        _log("session ready, waiting for commands")

        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._error(f"Invalid JSON: {exc}")
                    continue

                cmd = request.get("cmd", "")
                handler = self._HANDLERS.get(cmd)
                if handler is None:
                    self._error(f"Unknown command: {cmd}")
                    continue

                if self._verification_blocked and cmd != "quit" and cmd not in self._CART_SAFE_CMDS:
                    if self.browser.verification_pending():
                        # 2026-11-18：先自动拖滑块，拖不过再刷新。
                        # 顺序不能反：刷新会把滑块换一张，白丢一次解法机会。
                        _log("verification pending → auto slider")
                        if self.browser.try_solve_verification() \
                                and not self.browser.verification_pending():
                            _log("verification cleared by slider solver, resuming")
                        else:
                            _log("verification pending → self-recovery (refresh) before refusing")
                            if not self.browser.recover_from_verification():
                                self._error(
                                    "Taobao verification still pending after auto-solve and refresh "
                                    "(no products). Complete it in the browser window, then retry.",
                                    code="VERIFICATION_REQUIRED",
                                )
                                continue
                            _log("verification cleared by refresh, resuming")
                    self._verification_blocked = False
                elif self._verification_blocked:
                    # 购物车页不受搜索风控影响：这两个命令自己 goto cart.taobao.com，
                    # 进页面后会再查一次验证弹层。不放行的话，搜索被扣期间连
                    # 「清空购物车」「导出清单」都做不了，等于把两件事绑死了。
                    _log(f"verification blocked, but '{cmd}' runs on its own page — allowing")

                try:
                    handler(self, request)
                except VerificationRequired as exc:
                    self._verification_blocked = True
                    self._error(str(exc), code=exc.code)
                except Exception as exc:
                    self._error(str(exc))

        finally:
            self.cleanup()

    # ── Response helpers ──

    def _respond(self, **kwargs: Any) -> None:
        data = {
            "status": "success",
            "task_id": self.task_id,
            "screenshot": kwargs.get("screenshot"),
            "page_text_summary": (kwargs.get("page_text") or "")[:500] or None,
            "data": kwargs.get("data", {}),
        }
        _output_json(data)

    def _error(self, message: str, **kwargs: Any) -> None:
        data = {
            "status": "error",
            "task_id": self.task_id,
            "error": {"message": message, **kwargs},
        }
        _output_json(data)

    def _screenshot(self, name: str, light: bool = False) -> str:
        return self.browser.capture_viewport_screenshot(
            f"{name}_{self.task_id}", jpeg=light
        )

    def _page(self):
        return self.browser._ensure_page()

    # ── Command handlers ──

    # 空结果受控重试（I1）：只派 2 个变体，且必须与原词不同。
    _SYN_SUFFIX = (
        ("电阻", "贴片电阻"), ("电容", "贴片电容"),
        ("电感", "贴片电感"), ("晶振", "贴片晶振"),
    )

    @classmethod
    def _empty_retries(cls, keyword: str) -> list[str]:
        """从关键词派生重试词：去掉单位符号（Ω/±/欧姆）→ 换同义后缀。

        淘宝对「Ω」「±」这类符号经常不认（词被切碎 → 0 结果），而
        「0603 10kΩ 电阻」去掉 Ω 就能搜到。第二步把品类换成「贴片xx」，
        覆盖商家标题只写「贴片电阻」的情况。
        """
        kw = (keyword or "").strip()
        if not kw:
            return []
        out: list[str] = []
        stripped = re.sub(r"\s*[ΩΩ]\s*", " ", kw)
        stripped = stripped.replace("±", " ").replace("欧姆", " ").replace("欧", " ")
        stripped = re.sub(r"\s{2,}", " ", stripped).strip()
        if stripped and stripped != kw:
            out.append(stripped)
        for base, alt in cls._SYN_SUFFIX:
            if base in kw and alt not in kw:
                out.append(kw.replace(base, alt))
                break
        seen: set[str] = set()
        uniq: list[str] = []
        for k in out:
            if k and k != kw and k not in seen:
                seen.add(k)
                uniq.append(k)
        return uniq[:2]

    def _cmd_search(self, req: dict[str, Any]) -> None:
        keyword = req.get("keyword", "")
        if not keyword:
            self._error("Missing 'keyword'")
            return

        page = self._page()
        # light：批量跑图快时用。截图 + 整页文本各要 0.3–0.6s，而批量里 99% 的搜索
        # 结果都够用、不需要人看；真失败时 runner 再单独发 "screenshot" 命令补拍。
        light = bool(req.get("light"))

        _q0 = time.monotonic()
        # search() 内部已经是「三态检测 + 首屏未挂载时重导航一次」，
        # 不再需要这里补等 wait_for_results()（那是 12–22s 白等）。
        self.browser.search(keyword)
        screenshot = None if light else self._screenshot("search")
        page_text = None if light else self.browser.get_page_text(2000)

        _q1 = time.monotonic()
        collect_kwargs = dict(
            max_candidates=req.get("max_candidates", 20),
            price_min=req.get("price_min"),
            price_max=req.get("price_max"),
            min_sales=req.get("min_sales"),
            require_free_shipping=req.get("require_free_shipping", False),
            require_tmall=req.get("require_tmall"),
        )
        used_keyword = keyword
        candidates = self.browser.collect_candidates(keyword, **collect_kwargs)
        attempts: list[dict[str, Any]] = [{"keyword": keyword, "found": len(candidates)}]

        # I1：0 候选时受控重试。重试前先确认不是验证码 —— 对着滑块再撞两次
        # 只会让风控更狠，这也是之前「一行白跑 38 秒」的来源。
        if not candidates and req.get("retry_on_empty", True):
            for alt in self._empty_retries(keyword):
                if self.browser.verification_pending():
                    attempts.append({"keyword": alt, "found": 0, "skipped": "verification"})
                    break
                _a0 = time.monotonic()
                self.browser.search(alt)
                state = self.browser.wait_for_cards(timeout_ms=3000)
                if state == -3:
                    # 重试时撞上风控中间页：立刻交回人工，别再试下一个词。
                    # 注意：VerificationRequired 在 run() 里是函数内导入，这里
                    # （另一个方法的作用域）必须自己导，否则抛的是 NameError，
                    # 会被外层 except Exception 吞成一条普通错误，批量层拿不到
                    # VERIFICATION_REQUIRED 码，会继续硬撞下一行。
                    from browser_adapter import VerificationRequired

                    raise VerificationRequired()
                if state < 0:
                    attempts.append({
                        "keyword": alt, "found": 0, "skipped": "no cards",
                        "state": state,
                        "ms": int((time.monotonic() - _a0) * 1000),
                    })
                    continue
                got = self.browser.collect_candidates(alt, **collect_kwargs)
                attempts.append({
                    "keyword": alt, "found": len(got),
                    "ms": int((time.monotonic() - _a0) * 1000),
                })
                if got:
                    candidates, used_keyword = got, alt
                    break

        # 搜到 0 条（含重试后仍 0）再确认一次是不是弹了滑块：弹层是异步渲染的，
        # goto 之后那次检测常常还看不到。不查就换词重搜，等于对着滑块再撞 4 次
        # （实测一行白跑 38 秒，风控只会更狠）。
        if not candidates:
            self.browser._handle_captcha_if_present(page)

        _q2 = time.monotonic()
        print(
            f"[browser] timing search-cmd: search={_q1 - _q0:.2f}s collect={_q2 - _q1:.2f}s "
            f"light={light} attempts={[(a['keyword'], a['found']) for a in attempts]}",
            file=sys.stderr,
        )
        self.items = [
            {
                "index": idx,
                "title": item.title,
                "spec_text": (item.spec_text or "")[:400],
                "url": item.url,
                "price": item.price,
                "price_value": item.price_value,
                "sales_count": item.sales_count,
                "rating": item.rating,
                "is_tmall": item.is_tmall,
                "free_shipping": item.free_shipping,
            }
            for idx, item in enumerate(candidates)
        ]

        # A new search invalidates the previous item/SKU context.
        self.current_index = None
        self.sku_groups = []
        self.last_selections = []

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "keyword": keyword,
                "used_keyword": used_keyword,
                "attempts": attempts,
                "items": self.items,
                "items_count": len(self.items),
            },
        )

    def _cmd_open(self, req: dict[str, Any]) -> None:
        if not self.items:
            self._error("No search results. Run 'search' first.")
            return

        index = req.get("index", 0)
        if index < 0 or index >= len(self.items):
            self._error(f"Index {index} out of range (0-{len(self.items)-1})")
            return

        item = self.items[index]
        url = item.get("url", "")
        if not url:
            self._error("Item has no URL")
            return

        page = self._page()
        _log(f"opening item[{index}]: {item.get('title', '')[:40]}")
        page.goto(url, wait_until="commit", timeout=30000)
        self.browser._handle_captcha_if_present(page)
        # 商品页同样不必等 networkidle（详情页埋点永不 idle）。
        # 价格或 SKU 区块出现就足够解析；单规格商品没有 SKU 区块，所以两个一起等，
        # 否则每条无 SKU 的行都要白等 5 秒。
        from taobao_selectors import PRICE_READY_SELECTORS
        with suppress(Exception):
            page.wait_for_selector(", ".join(PRICE_READY_SELECTORS), timeout=8000)
        self.browser._simulate_browsing(page, max_scroll=800)
        self.browser._human_wait(0.1, 0.3)

        # shot=False：批量逐行跑时不再为每个商品页截图（真正要留证据的是加购那一步）
        shot = req.get("shot", True)
        screenshot = self._screenshot(f"item_{index}") if shot else None
        page_text = self.browser.get_page_text(3000)
        sku_groups = self.browser.get_sku_structure()
        # 值多的组（电阻阻值/容值这类）淘宝会折叠，最后几个值不在 DOM 里。
        # 只有疑似被折叠时才点展开重扫，常见商品不受影响。
        if any(len(g.get("values") or []) >= 10 for g in sku_groups):
            if self.browser._expand_sku_lists(page):
                sku_groups = self.browser.get_sku_structure()

        detail_price = self.browser._extract_detail_price(page)
        if detail_price is not None:
            item["price"] = f"¥{detail_price:.2f}"
            item["price_value"] = detail_price

        self.current_index = index
        self.sku_groups = sku_groups
        self.last_selections = []

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "item": item,
                "sku_groups": sku_groups,
                "detail_price": detail_price,
            },
        )

    def _cmd_select_sku(self, req: dict[str, Any]) -> None:
        if self.current_index is None:
            self._error("No item open. Run 'open' first.")
            return

        # Normalize: single {label,value} or array [{label,value}]
        raw = req.get("selections", req)
        if isinstance(raw, dict):
            selections = [raw]
        elif isinstance(raw, list):
            selections = raw
        else:
            self._error("'selections' must be {label,value} or [{label,value}]", code="INVALID_SELECTIONS")
            return

        selections = [
            sel
            for sel in selections
            if isinstance(sel, dict)
            and str(sel.get("label", "")).strip()
            and str(sel.get("value", "")).strip()
        ]
        if not selections:
            self._error(
                "'selections' entries need non-empty label and value",
                code="INVALID_SELECTIONS",
            )
            return

        item = self.items[self.current_index]
        url = item.get("url", "")
        page = self._page()
        # 刚 open 过的同一个商品不必再整页加载一次（旧逻辑每次都 reload，
        # 还顺带等 networkidle，白扔 2~3 秒/行）。只有页面已经跳到别处才重开。
        if url and not _same_item_url(page.url, url):
            page.goto(url, wait_until="commit", timeout=30000)
            from taobao_selectors import PRICE_READY_SELECTORS
            with suppress(Exception):
                page.wait_for_selector(", ".join(PRICE_READY_SELECTORS), timeout=6000)
            self.browser._human_wait(0.1, 0.25)

        self.last_selections = selections
        from browser_adapter import SkuAmbiguousError

        try:
            all_ok, results = self.browser.select_skus(selections)
        except SkuAmbiguousError as exc:
            self._error(
                str(exc),
                code=exc.code,
                label=exc.label,
                value=exc.value,
                candidates=exc.candidates,
                retryable=False,
            )
            return
        # 原来固定等 1–2s 模拟人手，119 行就是 2 分钟纯等待；0.35–0.7s 实测未增加风控。
        self.browser._human_wait(0.35, 0.7)

        light = bool(req.get("light"))
        screenshot = None if light else self._screenshot(f"sku_{self.current_index}")
        page_text = None if light else self.browser.get_page_text(2000)

        page = self._page()
        final_price = self.browser._extract_detail_price(page)
        if final_price is not None:
            item["price"] = f"¥{final_price:.2f}"
            item["price_value"] = final_price

        self.sku_groups = self.browser.get_sku_structure()

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "all_selected": all_ok,
                "selections": results,
                "final_price": final_price,
                "sku_groups": self.sku_groups,
            },
        )

    @staticmethod
    def _norm_sku(text: Any) -> str:
        """Normalize a SKU label/value so 10K, 10kΩ and 10KΩ compare equal."""
        value = unicodedata.normalize("NFKC", str(text or "")).lower()
        value = re.sub(r"\s+", "", value)
        value = value.replace("ω", "").replace("ohm", "")
        value = value.replace("±", "").replace("+/-", "")
        return value

    @staticmethod
    def _same_item_url(current: str, target: str) -> bool:
        """True when two item URLs point at the same listing (ignoring params)."""

        def identity(url: str) -> str:
            parsed = urlparse(url or "")
            item_id = (parse_qs(parsed.query).get("id") or [""])[0]
            if item_id:
                return f"id:{item_id}"
            return f"{parsed.netloc}{parsed.path}"

        return bool(current) and bool(target) and identity(current) == identity(target)

    def _verify_sku_selection(self) -> tuple[bool, list[str]]:
        """Check every SKU group has a selection and the targets are selected."""
        if not self.last_selections:
            return True, []

        groups = self.browser.get_sku_structure()
        problems: list[str] = []
        targets = [
            (self._norm_sku(sel["label"]), self._norm_sku(sel["value"]))
            for sel in self.last_selections
        ]

        labelled = [g for g in groups if (g.get("label") or "").strip()]

        for group in labelled:
            label = group.get("label") or ""
            values = group.get("values") or []
            if not values:
                continue
            selected = [v.get("text", "") for v in values if v.get("selected")]
            normalized_label = self._norm_sku(label)
            relevant = [
                (tlabel, tvalue)
                for tlabel, tvalue in targets
                if tlabel and (tlabel in normalized_label or normalized_label in tlabel)
            ]
            if not selected:
                problems.append(f"group '{label}' has no selected value")
                continue
            for _tlabel, tvalue in relevant:
                if not any(
                    self._norm_sku(text) == tvalue
                    or self._norm_sku(text).startswith(tvalue)
                    for text in selected
                ):
                    problems.append(f"group '{label}' selected {selected!r}, expected '{tvalue}'")

        for sel in self.last_selections:
            tlabel = self._norm_sku(sel["label"])
            if not any(
                tlabel in self._norm_sku(g.get("label", ""))
                or self._norm_sku(g.get("label", "")) in tlabel
                for g in labelled
            ):
                problems.append(f"target group '{sel['label']}' not found on the page")

        return (not problems), problems

    @staticmethod
    def _read_cart_badge(page) -> int | None:
        """Read the header cart badge ('购物车 86') without navigating away.

        商品页顶栏角标是懒加载的，读到 0 当作「读不到」，否则会把加成功的行判失败。
        直接找「文本形如 购物车+数字」的短节点，比整页 innerText 快一个数量级。
        """
        try:
            n = page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('a, span, div, em, i')) {
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 16) continue;
                    const m = t.match(/购物车\\s*(\\d+)/);
                    if (m) return parseInt(m[1], 10);
                  }
                  return null;
                }"""
            )
            if isinstance(n, int):
                return n if n > 0 else None
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_qty(raw: Any) -> int | None:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            value = int(raw.strip())
        else:
            return None
        if value < 1 or value > 99999:
            return None
        return value

    def _cmd_cart_add(self, req: dict[str, Any]) -> None:
        from browser_adapter import IDLE_TIMEOUT_MS, QuantityError, SkuAmbiguousError
        from taobao_selectors import ADD_TO_CART_BUTTONS

        if self.current_index is None:
            self._error("No item open. Run 'open' first.")
            return

        qty = self._parse_qty(req.get("qty", 1))
        if qty is None:
            self._error(
                f"Invalid 'qty': {req.get('qty')!r}. Use a positive integer (1-99999).",
                code="INVALID_QTY",
            )
            return

        item = self.items[self.current_index]
        url = item.get("url", "")
        page = self._page()
        navigated = False
        if url and not self._same_item_url(page.url, url):
            page.goto(url, wait_until="commit", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self.browser._handle_captcha_if_present(page)
            self.browser._human_wait(1.0, 2.0)
            navigated = True

        # A reload drops the SKU selection. Replay the last select-sku instead
        # of silently adding the default SKU.
        if navigated and self.last_selections:
            try:
                replayed, replay_results = self.browser.select_skus(self.last_selections)
            except SkuAmbiguousError as exc:
                screenshot = self._screenshot("sku_replay_ambiguous")
                self._error(
                    str(exc),
                    code=exc.code,
                    label=exc.label,
                    value=exc.value,
                    candidates=exc.candidates,
                    screenshot=screenshot,
                    retryable=False,
                )
                return
            if not replayed:
                screenshot = self._screenshot("sku_replay_fail")
                self._error(
                    "Reloaded the item page but could not restore the previous SKU selections.",
                    code="SKU_REPLAY_FAILED",
                    screenshot=screenshot,
                    selections=replay_results,
                    retryable=False,
                )
                return
            self.browser._human_wait(0.5, 1.0)

        # Set quantity on the detail page BEFORE adding. When qty is 1 only
        # force it if the widget currently shows something else; when qty > 1
        # the control is mandatory (never add 1 silently).
        current_qty = self.browser.read_quantity()
        need_set = qty > 1 or (current_qty is not None and current_qty != 1)
        if need_set:
            try:
                self.browser.set_quantity(qty)
            except QuantityError as exc:
                screenshot = self._screenshot("qty_fail")
                self._error(
                    str(exc),
                    code=exc.code,
                    screenshot=screenshot,
                    qty=qty,
                    cart_added=False,
                )
                return
            except Exception as exc:
                screenshot = self._screenshot("qty_fail")
                self._error(
                    f"Failed to set quantity: {exc}",
                    code="QTY_SET_FAILED",
                    screenshot=screenshot,
                    qty=qty,
                    cart_added=False,
                )
                return

        # Verify the SKU state before clicking: a lost selection must never be
        # "repaired" by adding the default SKU.
        sku_ok, sku_problems = self._verify_sku_selection()
        if not sku_ok:
            screenshot = self._screenshot("sku_not_selected")
            self._error(
                "SKU selection is not in the expected state; refusing to add.",
                code="SKU_NOT_SELECTED",
                screenshot=screenshot,
                problems=sku_problems,
                retryable=False,
            )
            return

        page = self._page()
        button = self.browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
        if button is None:
            screenshot = self._screenshot("cart_fail")
            self._error(
                "Add-to-cart button not found",
                screenshot=screenshot,
                qty=qty,
            )
            return

        badge_before = self._read_cart_badge(page)
        if badge_before is None:
            # 商品页顶栏角标是懒加载的，刚打开时可能还没有数字，等一下再读一次。
            page.wait_for_timeout(400)
            badge_before = self._read_cart_badge(page)
        # 加购是一次 XHR/JSONP 请求；挂个钩子记下它的返回，比事后去购物车页对账快得多。
        with suppress(Exception):
            page.evaluate(
                """() => {
                  if (window.__cartHook) return;
                  window.__cartHook = [];
                  const push = (u, t) => {
                    if (window.__cartHook.length > 6) return;
                    window.__cartHook.push({url: String(u).slice(0, 120), body: String(t).slice(0, 300)});
                  };
                  const of = window.fetch;
                  if (of) {
                    window.fetch = function () {
                      const args = arguments;
                      const p = of.apply(this, args);
                      try {
                        const u = (args[0] && args[0].url) || args[0] || '';
                        if (/cart/i.test(String(u))) {
                          p.then((r) => r.clone().text()).then((t) => push(u, t)).catch(() => {});
                        }
                      } catch (e) {}
                      return p;
                    };
                  }
                  const ox = window.XMLHttpRequest;
                  const open = ox.prototype.open;
                  const send = ox.prototype.send;
                  ox.prototype.open = function (m, u) { this.__u = u; return open.apply(this, arguments); };
                  ox.prototype.send = function () {
                    try {
                      if (/cart/i.test(String(this.__u || ''))) {
                        this.addEventListener('load', () => push(this.__u, this.responseText));
                      }
                    } catch (e) {}
                    return send.apply(this, arguments);
                  };
                }"""
            )
        _t_click = time.monotonic()
        # 网络侧信号：加购一定会有一次「加购接口」请求，200 才是真成功。
        # 注意必须排除查询类接口（querybagcount = 查购物车数量）——它每次进详情页
        # 都会发，命中 200 会被误判成「加购成功」（2026-11-07 实测 10 ok 里 5 行是假的）。
        _resp_hits: list[tuple[str, int]] = []
        _cart_query_re = re.compile(
            r"querybagcount|querybag|bagcount|getcartcount|cartcount|rendercart|querycart|cart.*count",
            re.I,
        )
        _cart_add_re = re.compile(r"addcart|addcartsuccess|trade\.addcart|cart.*add|ordercart", re.I)

        def _on_resp(resp):
            try:
                u = resp.url or ""
                lu = u.lower()
                if "alicdn" in lu:
                    return
                if not ("cart" in lu or "addcart" in lu or "bag" in lu):
                    return
                if _cart_query_re.search(lu):
                    return  # 查询类：不算加购证据
                if not _cart_add_re.search(lu):
                    return  # 其它 cart 相关请求（如渲染）也不算，宁可不确定也别误报
                _resp_hits.append((u[:100], resp.status))
            except Exception:  # noqa: BLE001
                pass

        with suppress(Exception):
            page.on("response", _on_resp)
        _t_k0 = time.monotonic()
        clicked = self.browser._human_click(page, button)
        if not clicked:
            # 弹层遮挡 / 按钮禁用：先按 Esc 收掉可能的浮层，重新定位再点一次。
            with suppress(Exception):
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            button = self.browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
            clicked = bool(button) and self.browser._human_click(page, button)
        if not clicked:
            screenshot = self._screenshot("cart_click_blocked")
            self._error(
                "加购按钮点不到（被弹层遮挡或按钮被禁用），换下一个候选重试",
                code="CART_CLICK_BLOCKED",
                screenshot=screenshot,
                qty=qty,
                cart_added=False,
                retryable=True,
            )
            return
        _t_k1 = time.monotonic()

        confirmed = None
        verified_via = None
        # 只在页面内观察一次：2.5 秒内出现「已加入购物车」类提示、或顶栏角标 +1，就算成功。
        # 一次 CDP 往返够用，不必每 200ms 把上万节点的详情页 innerText 拉一遍
        # （旧写法一圈 1 秒多，成功的行也要白等 4~9 秒）。命中的 class 名会记进 probe，
        # 以后可以直接加进 CART_CONFIRM_POPUP。
        watch = None
        try:
            watch = page.evaluate(
                """([timeoutMs, b0]) => new Promise((resolve) => {
                     const okRe = /已加入购物车|加入购物车成功|已成功加入|添加成功|已添加到购物车/;
                     const errRe = /失败|错误|下架|库存不足|已售完|不能购买|请稍后再试|请选择|已满|上限/;
                     let badgeEl = null;
                     const findBadge = () => {
                       for (const el of document.querySelectorAll('a,span,div,em,i')) {
                         const t = (el.textContent || '').trim();
                         if (!t || t.length > 16) continue;
                         if (/购物车\\s*\\d+/.test(t)) return el;
                       }
                       return null;
                     };
                     const readBadge = () => {
                       if (!badgeEl || !badgeEl.isConnected) badgeEl = findBadge();
                       if (!badgeEl) return null;
                       const m = (badgeEl.textContent || '').trim().match(/(\\d+)/);
                       return m ? parseInt(m[1], 10) : null;
                     };
                     const sel = '[class*="toast"],[class*="Toast"],[class*="popup"],[class*="Popup"],'
                       + '[class*="success"],[class*="Success"],[class*="dialog"],[class*="Dialog"],'
                       + '[class*="layer"],[class*="Layer"]';
                     const t0 = performance.now();
                     const seen = new Map();
                     const tick = () => {
                       for (const el of document.querySelectorAll(sel)) {
                         const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                         if (!t || t.length > 80) continue;
                         if (okRe.test(t)) return {via: 'dom', cls: (el.className || '').toString().slice(0, 60), text: t};
                         if (errRe.test(t)) return {via: 'err', text: t};
                         if (seen.size < 14) seen.set((el.className || '').toString().slice(0, 40), t.slice(0, 50));
                       }
                       const b = readBadge();
                       if (b0 != null && b != null && b > b0) return {via: 'badge', b0: b0, badge: b};
                       if (performance.now() - t0 > timeoutMs) {
                         return {via: null, b0: b0, badge: b,
                                 seen: Array.from(seen, ([cls, text]) => ({cls: cls, text: text}))};
                       }
                       return null;
                     };
                     let done = false;
                     const fire = () => {
                       if (done) return;
                       const r = tick();
                       if (r) { done = true; clearInterval(timer); resolve(r); }
                     };
                     const timer = setInterval(fire, 100);
                     fire();
                   })""",
                [550, badge_before],
            ) or None
        except Exception as exc:  # noqa: BLE001
            print(f"[browser] cart confirm watch failed: {exc}", file=sys.stderr)
        _t_watch = time.monotonic()
        print(
            f"[browser] timing click={_t_k1 - _t_k0:.2f}s watch={_t_watch - _t_k1:.2f}s",
            file=sys.stderr,
        )
        cart_hooks = []
        # 观察窗只有 550ms：页面忙/网慢的时候加购请求 1~2 s 才回来，
        # 这段补看一次，避免「明明加成功却报无回执」（2026-11-09 真机实测）。
        if watch is None or watch.get("via") is None:
            if not any(200 <= st_ < 400 for _, st_ in _resp_hits):
                with suppress(Exception):
                    page.wait_for_timeout(1200)
                _badge_late = self._read_cart_badge(page)
                if (
                    badge_before is not None
                    and _badge_late is not None
                    and _badge_late > badge_before
                ):
                    watch = {"via": "badge", "b0": badge_before, "badge": _badge_late, "late": True}
                    print("[browser] cart badge late +1 (extended watch)", file=sys.stderr)
        with suppress(Exception):
            cart_hooks = page.evaluate("() => window.__cartHook || []") or []
        if cart_hooks:
            print("[browser] cart hook: " + json.dumps(cart_hooks, ensure_ascii=False)[:700], file=sys.stderr)

        with suppress(Exception):
            page.remove_listener("response", _on_resp)

        # 零证据补点一次：既没有加购请求、角标也没动、也没有任何 toast ——
        # 说明这一下点击根本没进业务逻辑（常见于弹层刚消失、按钮还没挂上事件）。
        # 此时购物车一定没变化，**再点一次是安全的**（不会重复下单），
        # 只补一次，避免和「已加成功但没回执」的情形混淆。
        if (
            confirmed is None
            and not _resp_hits
            and not (watch and watch.get("via") in ("dom", "badge", "err"))
        ):
            print("[browser] cart click had no effect, retrying once", file=sys.stderr)
            _sku_ok2, _ = self._verify_sku_selection()
            _btn2 = self.browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS) if _sku_ok2 else None
            if _btn2 is not None:
                _hits2: list[tuple[str, int]] = []

                def _on_resp2(resp):
                    try:
                        lu = (resp.url or "").lower()
                        if _cart_add_re.search(lu) and not _cart_query_re.search(lu):
                            _hits2.append(((resp.url or "")[:100], resp.status))
                    except Exception:  # noqa: BLE001
                        pass

                with suppress(Exception):
                    page.on("response", _on_resp2)
                if self.browser._human_click(page, _btn2):
                    with suppress(Exception):
                        page.wait_for_timeout(1500)
                    with suppress(Exception):
                        page.remove_listener("response", _on_resp2)
                    _badge2 = self._read_cart_badge(page)
                    if _hits2 and any(200 <= st_ < 400 for _, st_ in _hits2):
                        _resp_hits.extend(_hits2)
                        print(f"[browser] retry cart xhr ok: {_hits2[-1][0][:80]}", file=sys.stderr)
                    elif (
                        badge_before is not None
                        and _badge2 is not None
                        and _badge2 > badge_before
                    ):
                        watch = {"via": "badge", "b0": badge_before, "badge": _badge2, "retry": True}
                        print("[browser] retry cart badge +1", file=sys.stderr)

        if watch and watch.get("via") in ("dom", "badge"):
            confirmed = True
            verified_via = watch.get("via")
        elif _resp_hits and any(200 <= st_ < 400 for _, st_ in _resp_hits):
            # 页面没给任何提示，但确实发了 cart 请求且 200 —— 认这条
            confirmed = True
            verified_via = "xhr"
            print(f"[browser] cart xhr ok: {_resp_hits[-1][0]}", file=sys.stderr)
        elif watch and watch.get("via") == "err":
            _txt = watch.get("text") or ""
            # 规格在点击瞬间被重置（换 SKU 图/库存刷新）→ 页面提示「请选择规格」。
            # 这种情况什么都没加进去，重放选择再点一次是安全的。
            if any(k in _txt for k in ("请选择", "选择规格", "选择商品")):
                print("[browser] cart add said 'please select spec', replaying SKU", file=sys.stderr)
                _replayed = False
                with suppress(Exception):
                    _replayed, _ = self.browser.select_skus(self.last_selections or [])
                    self.browser._human_wait(0.4, 0.8)
                if _replayed:
                    _btn3 = self.browser._find_first_visible_locator(page, ADD_TO_CART_BUTTONS)
                    if _btn3 is not None and self.browser._human_click(page, _btn3):
                        with suppress(Exception):
                            page.wait_for_timeout(1500)
                        _badge3 = self._read_cart_badge(page)
                        if (
                            badge_before is not None
                            and _badge3 is not None
                            and _badge3 > badge_before
                        ):
                            confirmed = True
                            verified_via = "badge"
                            print("[browser] cart add recovered after SKU replay", file=sys.stderr)
                if confirmed is not None:
                    pass  # 走到下面的统一判定
                else:
                    self._error(
                        f"点击加购后页面提示：{_txt}（重选规格后仍失败）",
                        code="SKU_NOT_SELECTED",
                        cart_added=False,
                        retryable=True,
                    )
                    return
            elif any(k in _txt for k in ("已满", "上限", "满了")):
                self._error(
                    f"购物车已满或达到上限：{_txt}",
                    code="CART_FULL",
                    cart_added=False,
                    retryable=False,
                )
                return
            else:
                self._error(
                    f"点击加购后页面提示：{_txt}",
                    code="CART_ADD_REJECTED",
                    cart_added=False,
                    retryable=False,
                )
                return

        # 兜底确认失败时才检查是不是被风控拦了。用 textContent（便宜）而不是 innerText。
        if confirmed is None:
            body_text = self.browser.get_page_text(4000)
            if any(k in body_text for k in ("请拖动滑块", "拖动下方滑块", "请完成验证", "滑块验证")):
                screenshot = self._screenshot("verify_slider")
                self._error(
                    "点击加购后弹出滑块验证，需要人工完成验证后继续。",
                    code="VERIFICATION_REQUIRED",
                    screenshot=screenshot,
                    cart_added=False,
                    retryable=False,
                )
                return

        confirm_probe: list[dict[str, Any]] = list((watch or {}).get("seen") or [])
        if confirmed is None and not confirm_probe:
            # 没能从弹窗/toast/角标确认时，先抓一份现场：真实页面上「成功」提示长什么样。
            # 只在兜底路径花这 ~100ms，正常成功的行不受影响。
            with suppress(Exception):
                confirm_probe = page.evaluate(
                    """() => {
                      const out = [];
                      const els = document.querySelectorAll(
                        '[class*="toast"],[class*="Toast"],[class*="popup"],[class*="Popup"],' +
                        '[class*="dialog"],[class*="Dialog"],[class*="success"],[class*="Success"],' +
                        '[class*="layer"],[class*="Layer"],[class*="cart"],[class*="Cart"]');
                      for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (r.top > window.innerHeight) continue;
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!t) continue;
                        out.push({cls: (el.className || '').toString().slice(0, 60), text: t.slice(0, 60)});
                        if (out.length > 18) break;
                      }
                      return out;
                    }"""
                ) or []
            print("[probe] " + json.dumps(confirm_probe, ensure_ascii=False), file=sys.stderr)

        # 严格模式才去购物车页对账（一次要多开一个标签页，约 4 秒）。
        # 批量的默认策略是「点击 + SKU 已校验」即记账，整批结束再用购物车总数核对。
        if confirmed is None and CART_VERIFY_STRICT:
            with suppress(Exception):
                verify_page = page.context.new_page()
                try:
                    verify_page.goto(
                        "https://cart.taobao.com/cart.htm",
                        wait_until="commit",
                        timeout=15000,
                    )
                    try:
                        verify_page.wait_for_selector(
                            "text=购物车", timeout=8000
                        )
                    except Exception:
                        pass
                    if badge_before is not None:
                        b = self._read_cart_badge(verify_page)
                        if b is not None and b > badge_before:
                            confirmed = True
                            verified_via = "cart_badge"
                finally:
                    with suppress(Exception):
                        verify_page.close()

        if confirmed is None:
            # 点了按钮，但页面没提示、网络侧也没有「加购接口」请求 —— 说明这次
            # 点击根本没触发加购（SKU 没选上、按钮点空、弹层被吞都可能）。
            # 绝不能当成功记账：2026-11-07 实测 10 行 ok 里 5 行购物车里根本没有。
            # 交回上层记 uncertain（已经点过，换候选会重复加购，所以这行到此为止）。
            screenshot = self._screenshot("cart_add_unconfirmed", light=True)
            _ev = ""
            if _resp_hits:
                _ev = f"（加购接口有过响应但状态 {_resp_hits[-1][1]}）"
            elif watch and watch.get("badge") == watch.get("b0"):
                _ev = "（购物车角标没变化）"
            self._error(
                f"已点击「加入购物车」，但页面提示、购物车角标、加购接口三处都没有成功信号{_ev}。",
                code="CART_UNCONFIRMED",
                screenshot=screenshot,
                cart_added=False,
                qty=qty,
                retryable=False,
            )
            return

        _t_sku = time.monotonic()
        # Read the SKU state back after adding; a success popup does not prove
        # the intended SKU was the one that went into the cart.
        sku_ok_after, sku_problems_after = self._verify_sku_selection()
        if not sku_ok_after:
            screenshot = self._screenshot("sku_confirm_fail")
            self._error(
                "Item was added, but the selected SKU could not be confirmed.",
                code="SKU_CONFIRM_FAILED",
                screenshot=screenshot,
                cart_added=confirmed is not None,
                problems=sku_problems_after,
                qty=qty,
                retryable=False,
            )
            return

        qty_confirmed: bool | None = None
        if confirmed is not None:
            actual = self.browser.read_quantity()
            if actual is not None:
                qty_confirmed = actual == qty
            if qty_confirmed is False or (qty_confirmed is None and qty > 1):
                screenshot = self._screenshot("qty_confirm_fail")
                self._error(
                    f"Item was added, but quantity could not be confirmed "
                    f"(expected {qty}, read {actual!r}). Check the cart manually.",
                    code="QTY_CONFIRM_FAILED",
                    screenshot=screenshot,
                    cart_added=True,
                    qty=qty,
                    retryable=False,
                )
                return

        _t_qty = time.monotonic()
        # light：批量跑时用 jpeg 缩略证据图 + 不拉整页文本（详情页 textContent
        # 要 0.5–1s，而这里只是为了把失败现象存下来，成功行根本用不到）。
        light = bool(req.get("light"))
        screenshot = self._screenshot("cart_add", light=light)
        page_text = None if light else self.browser.get_page_text(1000)
        print(
            f"[browser] timing add: watch={_t_watch - _t_click:.2f}s "
            f"sku={_t_qty - _t_sku:.2f}s rest={time.monotonic() - _t_qty:.2f}s "
            f"via={verified_via}",
            file=sys.stderr,
        )

        self._respond(
            screenshot=screenshot,
            page_text=page_text,
            data={
                "cart_added": confirmed is not None,
                "confirmed": confirmed is not None,
                "cart_verified_via": verified_via,
                "confirm_probe": confirm_probe,
                "badge_before": badge_before,
                "watch": watch,
                "qty": qty,
                "qty_confirmed": qty_confirmed,
                "sku_verified": bool(self.last_selections),
                "sku_selection_count": len(self.last_selections),
                "item_index": self.current_index,
            },
        )

    # 购物车行抽取（I4）。选择器全部来自 2026-11-07 真机 DOM：
    #   div[class*="cartItemInfoContainer"]        ← 一行（不是 itemWrapper，那是猜你喜欢）
    #     a[class*="title"]                        ← 标题
    #     div[class*="trade-cart-item-sku-old"]     ← 规格「封装规格：100只/包 颜色分类：30PF」
    #     div[class*="trade-cart-item-price"]       ← 价格（数字被拆成「￥ 0 . 97」几个 span）
    _CART_ROWS_JS = """(selectors) => {
      const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
      const EMPTY = ['空空如也', '购物车还是空的', '购物车暂无商品', '购物车中还没有商品'];
      const isEmpty = () => {
        const t = (document.body && (document.body.innerText || '')) || '';
        return EMPTY.some((m) => t.indexOf(m) >= 0);
      };
      let nodes = [];
      let used = '';
      for (const s of selectors) {
        const got = document.querySelectorAll(s);
        if (got.length) { nodes = Array.from(got); used = s; break; }
      }
      const rows = nodes.map((r, i) => {
        const q = (s) => { try { const e = r.querySelector(s); return e ? clean(e.innerText) : ''; } catch (e) { return ''; } };
        const a = r.querySelector('a[href*="item.htm"]') || r.querySelector('a[href]');
        const priceText = q('[class*="trade-cart-item-price"]') || q('[class*="cartItemPrice"]');
        let price = null;
        const m = priceText.match(/[￥¥]\\s*(\\d+)\\s*(?:\\.\\s*(\\d+))?/);
        if (m) price = parseFloat(m[1] + '.' + (m[2] || '0'));
        let qty = null;
        // 数量控件在各版本购物车里形态不同：老版是 countValue/itemAmount，
        // 2026 版是 antd 的 <input class="ant-input inputWrapper-...">（class 里
        // 根本没有 count/quantity 字样）。所以按「排除勾选框 + 认 ant-input」找。
        const qis = Array.from(r.querySelectorAll('input'));
        const qi = qis.find((e) => {
          if (e.type === 'checkbox' || e.type === 'radio' || e.type === 'hidden') return false;
          if (e.type === 'number') return true;
          return /countValue|quantity|itemAmount|ant-input|inputWrapper|num/i.test(String(e.className));
        });
        if (qi && String(qi.value).trim() !== '') {
          const v = parseInt(String(qi.value).replace(/\\D/g, ''), 10);
          if (!isNaN(v)) qty = v;
        }
        if (qty === null) {
          const qd = r.querySelector('[class*="countValue"], [class*="itemAmount"], [class*="quantity"]');
          if (qd) {
            const m = clean(qd.innerText || qd.textContent || qd.value || '').match(/(\\d+)/);
            if (m) qty = parseInt(m[1], 10);
          }
        }
        if (qty === null) {
          const m2 = clean(r.innerText || '').match(/(?:[x×]\\s*(\\d+)\\s*件?|数量[：:]\\s*(\\d+))/);
          if (m2) qty = parseInt(m2[1] || m2[2], 10);
        }
        return {
          index: i,
          title: q('a[class*="title"]') || q('a[href*="item.htm"]'),
          spec: q('[class*="trade-cart-item-sku-old"]') || q('[class*="trade-cart-item-sku"]'),
          price_text: priceText,
          price: price,
          qty: qty,
          url: a ? (a.getAttribute('href') || '') : '',
        };
      });
      return { selector: used, rows: rows, empty: isEmpty() };
    }"""

    # 空车文案（淘宝/天猫购物车页）。空车时一个行容器都没有，如果只按
    # 「找不到选择器」判，就会误报 degraded=true —— 上层会当成「读不到」，
    # 收尾对账也随之跳过，真出问题反而看不见。
    _CART_EMPTY_MARKS = (
        "空空如也",
        "购物车还是空的",
        "购物车暂无商品",
        "购物车中还没有商品",
    )

    _CART_NEXT_PAGE = (
        ".ant-pagination-next:not(.ant-pagination-disabled) button",
        ".ant-pagination-next:not(.ant-pagination-disabled)",
        '[class*="pagination"] [class*="next"]:not([class*="disabled"])',
    )

    def _cart_rows(self, page) -> dict:
        """抽当前页的购物车行。返回 {selector, rows, degraded, empty}。"""
        from taobao_selectors import CART_ITEM_ROW_SELECTORS

        try:
            data = page.evaluate(self._CART_ROWS_JS, list(CART_ITEM_ROW_SELECTORS))
        except Exception as exc:  # noqa: BLE001
            return {"selector": "", "rows": [], "degraded": True, "empty": False, "error": str(exc)}
        rows = (data or {}).get("rows") or []
        for i, r in enumerate(rows):
            r["index"] = i
            r["title"] = (r.get("title") or "")[:200]
        empty = bool((data or {}).get("empty"))
        return {
            "selector": (data or {}).get("selector") or "",
            "rows": rows,
            "empty": empty,
            # degraded：一个容器都没找到**且页面也没说空车**，说明 DOM 改名了 ——
            # 上层要按「读不到」处理。空车（有「空空如也」文案）是正常状态。
            "degraded": not (data or {}).get("selector") and not empty,
        }

    @classmethod
    def _cart_is_empty(cls, page) -> bool:
        """页面是不是明确说「购物车是空的」（不猜，只认文案）。"""
        try:
            text = page.inner_text("body") or ""
        except Exception:
            return False
        return any(m in text for m in cls._CART_EMPTY_MARKS)

    def _cart_next_page(self, page, settle: float = 1.5) -> bool:
        """点「下一页」。没有可用分页就返回 False。"""
        for sel in self._CART_NEXT_PAGE:
            try:
                loc = page.locator(sel).first
                if not loc.count() or not loc.is_visible():
                    continue
                if (loc.get_attribute("aria-disabled") or "") == "true":
                    continue
                loc.click()
                page.wait_for_timeout(int(settle * 1000))
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _cart_scroll_load_all(self, page, settle: float = 1.0,
                              max_rounds: int = 12) -> int:
        """滚动到底，触发淘宝购物车的无限滚动加载，返回最终 DOM 行数。

        2026-11-18 实测：购物车显示 68 件，但 DOM 初始只渲染 29 行，必须滚动
        才加载后面的（滚 1 次 48 行、2 次 67、3 次 68）。旧代码只找「下一页」
        按钮，新版购物车根本没有分页按钮 → 只读 29 行就去对账，45 个 ok 行里
        10 行被误判「购物车里没找到」。滚动到行数连续 2 轮不增为止。
        只读 DOM、不点任何按钮，不会误删/误选商品。
        """
        from taobao_selectors import CART_ITEM_ROW_SELECTORS

        sel = CART_ITEM_ROW_SELECTORS[0] if CART_ITEM_ROW_SELECTORS else '[class*="cartItemInfoContainer"]'
        stable = 0
        last = -1
        for _ in range(max(1, max_rounds)):
            before = 0
            try:
                before = page.evaluate("(s)=>document.querySelectorAll(s).length", sel)
            except Exception:
                return 0
            try:
                # 同时滚 window 和滚轮：新版是 window 级滚动，个别版本是内层容器
                page.evaluate("()=>window.scrollTo(0, document.body.scrollHeight)")
                page.mouse.wheel(0, 120000)
            except Exception:
                return before
            page.wait_for_timeout(int(settle * 1000))
            try:
                after = page.evaluate("(s)=>document.querySelectorAll(s).length", sel)
            except Exception:
                return before
            if after <= before:
                stable += 1
                if stable >= 2:
                    return after
            else:
                stable = 0
            last = after
        return last

    def _cart_rows_all(self, page, max_pages: int = 8, settle: float = 1.2) -> dict:
        """读完整个购物车：先滚动加载全部行（新版无限滚动），再翻页兜底（老版分页）。"""
        # 第一步：滚动让所有行进入 DOM（新版淘宝购物车）
        self._cart_scroll_load_all(page, settle=0.9)
        # 第二步：读当前（已全量）的行；老版分页的「下一页」逻辑保留作兼容
        first = self._cart_rows(page)
        all_rows = list(first["rows"])
        pages = 1
        while pages < max_pages:
            if not self._cart_next_page(page, settle=settle):
                break
            pages += 1
            got = self._cart_rows(page)
            if not got["rows"]:
                break
            base = len(all_rows)
            for r in got["rows"]:
                r["index"] = base + r["index"]
            all_rows.extend(got["rows"])
        return {
            "rows": all_rows,
            "first_page_rows": len(first["rows"]),
            "pages": pages,
            "selector": first["selector"],
            "degraded": first["degraded"],
        }

    @contextmanager
    def _cart_page(self):
        """购物车命令用哪个页面。

        P2-3：搜索被风控扣住时，人工可能正在主页面解图片点选验证 —— cart-view /
        cart-remove 再 goto 主页面会把验证页冲掉，人工白点 9 张图。所以阻断态下
        开临时标签页操作购物车，用完即关；平时仍用主页面（少一个标签的开销）。
        """
        main = self._page()
        use_temp = self._verification_blocked or self.browser.verification_pending()
        page = main.context.new_page() if use_temp else main
        try:
            yield page
        finally:
            if use_temp:
                with suppress(Exception):
                    page.close()

    def _cmd_cart_view(self, req: dict[str, Any]) -> None:
        with self._cart_page() as page:
            self._cart_view_on(page, req)

    def _cart_view_on(self, page, req: dict[str, Any]) -> None:
        page.goto(
            "https://cart.taobao.com/cart.htm",
            wait_until="commit",
            timeout=20000,
        )
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        self.browser._handle_captcha_if_present(page)
        self.browser._human_wait(1.2, 2.2)

        screenshot = self._screenshot("cart")
        page_text = self.browser.get_page_text(5000)

        item_count = self._wait_cart_count(page, timeout_s=10.0)
        cart_empty = False
        detail = {"rows": [], "pages": 0, "selector": "", "degraded": True}
        if item_count:
            detail = self._cart_rows_all(page)
        else:
            # 空车 ≠ 读不到：页面明确写「空空如也」时是正常状态，
            # 不能再打 degraded（否则收尾对账会跳过、前端显示「明细降级」）。
            cart_empty = self._cart_is_empty(page)
            if cart_empty:
                detail = {"rows": [], "pages": 1, "selector": "", "degraded": False}

        data: dict[str, Any] = {
            "cart_item_count": item_count,
            "cart_empty": cart_empty,
            # P1-3：rows_page = 本页行数，rows_total = 跨页合计（文档 §19.3 的口径）
            "rows_page": detail.get("first_page_rows", len(detail["rows"])),
            "rows_total": len(detail["rows"]),
            "rows": detail["rows"],
            "rows_degraded": bool(detail["degraded"]),
            "rows_selector": detail["selector"],
            "pages_read": detail["pages"],
        }
        if item_count and not detail["rows"]:
            data["code"] = "CART_ROWS_NOT_RENDERED"
            data["message"] = (
                f"购物车角标显示 {item_count} 件，但一行都没读出来（DOM 可能改名）——"
                "已按 degraded 处理，不是空车"
            )

        self._respond(screenshot=screenshot, page_text=page_text, data=data)

    def _wait_cart_count(self, page, timeout_s: float = 15.0) -> int:
        """等权威计数「全部商品(N)」出现 —— 页面刚进来时是 0/未渲染。

        这是唯一可信的件数：顶栏「购物车 N」是懒加载占位，行数只覆盖当前页。
        空车时「全部商品(N)」根本不会出现，只等它会把 15 秒白白烧掉
        （2026-11-09 实测空车 cart-view 要 11.5 s）—— 所以先认「空空如也」。
        """
        deadline = time.monotonic() + timeout_s
        last = 0
        while time.monotonic() < deadline:
            last = self._cart_item_count(page)
            if last:
                return last
            if self._cart_is_empty(page):
                return 0
            page.wait_for_timeout(400)
        return last

    @staticmethod
    def _cart_item_count(page) -> int:
        """Read '全部商品(N)' from the cart page; 0 when it cannot be parsed."""
        try:
            text = page.inner_text("body")
        except Exception:
            return 0
        m = re.search(r"全部商品\s*[（(]\s*(\d+)\s*[）)]", text or "")
        return int(m.group(1)) if m else 0

    def _cmd_cart_remove(self, req: dict[str, Any]) -> None:
        """Delete cart rows whose title contains 'keyword', or everything with 'all'.

        Refuses to run without an explicit keyword or all=true so a stray call
        can never wipe a cart that also holds the user's own items.
        """
        with self._cart_page() as page:
            self._cart_remove_on(page, req)

    def _cart_remove_on(self, page, req: dict[str, Any]) -> None:
        from taobao_selectors import (
            CART_DELETE_CONFIRM_BUTTONS,
            CART_ITEM_ROW_SELECTORS,
            CART_ROW_DELETE_BUTTONS,
        )

        keyword = str(req.get("keyword") or "").strip()
        all_flag = bool(req.get("all"))
        items_req = [x for x in (req.get("items") or []) if isinstance(x, dict)]
        dry = bool(req.get("dry"))
        if not keyword and not all_flag and not items_req:
            self._error(
                "Refusing an implicit cart clear: pass 'keyword' (title substring), "
                "'items' (exact list) or 'all': true.",
                code="CART_REMOVE_AMBIGUOUS",
            )
            return

        page.goto("https://cart.taobao.com/cart.htm", wait_until="commit", timeout=45000)
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        self.browser._handle_captcha_if_present(page)
        self.browser._human_wait(2.0, 3.5)

        def _targets():
            """天猫行和淘宝行的容器类名不一样：逐个 selector 试，取第一个真有命中的。"""
            for sel in CART_ITEM_ROW_SELECTORS:
                loc = page.locator(sel)
                with suppress(Exception):
                    if not loc.count():
                        continue
                t = loc if all_flag else loc.filter(has_text=keyword)
                with suppress(Exception):
                    if t.count():
                        return t
            return None

        rows = None
        for _attempt in range(2):
            for _ in range(20):
                rows = _targets()
                if rows is not None:
                    break
                self.browser._human_wait(0.8, 1.5)
            if rows is not None:
                break
            # 首次进购物车偶尔会渲染出「Oops！页面出错了」，刷新一次再试
            with suppress(Exception):
                page.reload(wait_until="domcontentloaded", timeout=30000)
            self.browser._human_wait(2.5, 4.0)

        before = self._wait_cart_count(page, timeout_s=15.0)

        # 「只删本工具加购的那几件」走独立路径：按商品 id 精确匹配 + 数量守卫，
        # 不污染按关键字删 / 清空这两条老路。
        if items_req and not all_flag and not keyword:
            self._cart_remove_selected(page, items_req, before, dry)
            return

        if rows is None:
            # I5：以前这里不管有没有货都回「购物车里没有可删除的条目」——
            # 页面没渲染出来的时候，人以为删干净了，其实一件没删。
            # 现在把「空车」「没匹配到」「读不到行」三种情况分开报。
            snapshot = self._cart_rows_all(page) if before else {"rows": [], "degraded": True}
            readable = len(snapshot["rows"])
            hit_titles = [
                r.get("title") or "" for r in snapshot["rows"]
                if keyword and keyword in (r.get("title") or "")
            ]
            if before == 0:
                message, code = "购物车是空的", None
            elif readable == 0:
                message, code = (
                    f"购物车里有 {before} 件，但一行都没读到（DOM 可能改名）——"
                    "没有执行删除，请刷新后重试",
                    "CART_ROWS_NOT_RENDERED",
                )
            elif hit_titles:
                message, code = (
                    f"定位到了标题含「{keyword}」的条目（{hit_titles[0][:40]}），"
                    "但行内的删除按钮没点上 —— 没有执行删除",
                    "CART_MATCH_FOUND_BUT_NO_ROW",
                )
            else:
                message, code = (
                    f"购物车里没有标题含「{keyword}」的条目（共 {before} 件，"
                    f"读到 {readable} 行，{snapshot.get('pages')} 页）",
                    "CART_NO_MATCH",
                )
            self._respond(
                screenshot=self._screenshot("cart_remove_empty"),
                page_text=self.browser.get_page_text(1500),
                data={
                    "removed": 0,
                    "before": before,
                    "after": before,
                    "matched": [],
                    "verified": False,
                    "message": message,
                    "code": code,
                },
            )
            return

        matched: list[str] = []
        confirm_clicked = False

        # 清空整个购物车的快路：全选 → 批量删除 → 确认。
        # 一行一行删要 2–4s/个，91 件就是 3–5 分钟；批量删一轮 5–10s。
        if all_flag:
            from taobao_selectors import CART_BATCH_DELETE_BUTTONS, CART_SELECT_ALL_CHECKBOX

            for _round in range(8):
                if _targets() is None:
                    break
                picked = False
                with suppress(Exception):
                    cb = page.locator(CART_SELECT_ALL_CHECKBOX)
                    if cb.count():
                        cb.first.click()
                        picked = True
                if not picked:
                    break
                self.browser._human_wait(0.4, 0.8)
                hit = False
                for sel in CART_BATCH_DELETE_BUTTONS:
                    with suppress(Exception):
                        loc = page.locator(sel)
                        if loc.count() and loc.first.is_visible():
                            loc.first.click()
                            hit = True
                            break
                    if hit:
                        break
                if not hit:
                    break
                self.browser._human_wait(0.6, 1.0)
                for sel in CART_DELETE_CONFIRM_BUTTONS:
                    done = False
                    with suppress(Exception):
                        loc = page.locator(sel)
                        if loc.count() and loc.first.is_visible():
                            loc.first.click()
                            confirm_clicked = True
                            done = True
                    if done:
                        break
                self.browser._human_wait(1.2, 1.8)
                if not matched:
                    matched.append("<全部商品>")
                # 删完一页会自动翻页/重渲染，等它稳定再判断
                with suppress(Exception):
                    page.wait_for_load_state("domcontentloaded", timeout=5000)

        # 逐行删：删完这一页再翻下一页，直到整体件数归零或不再下降（I5）。
        # 以前只处理当前页（85 件 = 3 页），第二页以后根本没动过，
        # 却因为 `after` 从角标读到的是「全部商品(N)」而误以为删干净了。
        page_rounds = 0
        while page_rounds < 8:
            page_rounds += 1
            prior = -1
            for _ in range(40):
                rows = _targets()
                if rows is None:
                    break
                targets = rows
                with suppress(Exception):
                    if targets.count() == 0:
                        break
                    if prior >= 0 and targets.count() >= prior:
                        break  # 删除没有生效，别死循环
                    prior = targets.count()
                row = targets.first
                if all_flag:
                    if not matched:
                        matched.append("<全部商品>")
                else:
                    with suppress(Exception):
                        title = (row.inner_text() or "").split("\n")[0][:40]
                        if title:
                            matched.append(title)

                link = None
                for sel in CART_ROW_DELETE_BUTTONS:
                    with suppress(Exception):
                        cand = row.locator(sel)
                        if cand.count():
                            link = cand.first
                            break
                if link is None:
                    break
                link.click()
                self.browser._human_wait(0.7, 1.2)

                for sel in CART_DELETE_CONFIRM_BUTTONS:
                    clicked = False
                    with suppress(Exception):
                        loc = page.locator(sel)
                        if loc.count() and loc.first.is_visible():
                            loc.first.click()
                            clicked = True
                    if clicked:
                        confirm_clicked = True
                        break
                self.browser._human_wait(1.1, 1.8)

            if self._cart_item_count(page) <= 0:
                break
            if _targets() is not None:
                break  # 本页还有目标却删不动了，别在同一个位置空转
            if not self._cart_next_page(page):
                break
            self.browser._human_wait(0.8, 1.4)

        after = self._wait_cart_count(page, timeout_s=8.0)
        removed = max(0, before - after)
        if before == 0 and after == 0:
            message, code, verified = "购物车是空的", None, False
        elif after < before:
            message = f"删除生效：{before} → {after}"
            code, verified = None, True
        else:
            # 点了删除但件数没变 —— 必须报错，不能回「删了 N 行」让人以为好了
            message = (
                f"删除未生效：{before} → {after}"
                f"（点了 {len(matched)} 行，确认弹窗{'已' if confirm_clicked else '未'}点）"
            )
            code, verified = "CART_DELETE_NOT_EFFECTIVE", False

        self._respond(
            screenshot=self._screenshot("cart_removed"),
            page_text=self.browser.get_page_text(2000),
            data={
                "removed": removed,
                "before": before,
                "after": after,
                "matched": matched,
                "confirm_clicked": confirm_clicked,
                "pages_touched": page_rounds,
                "verified": verified,
                "message": message,
                "code": code,
            },
        )

    def _cart_remove_selected(
        self, page, items: list[dict[str, Any]], before: int, dry: bool
    ) -> None:
        """只删 items 指定的商品，其余（含用户自己加的）一律不碰。

        items 每项：{"item_id": "123", "title": "...", "qty": 2}
        dry=True 只返回计划、不点任何按钮，让用户先看清「会删哪几件」。
        """
        from taobao_selectors import (
            CART_DELETE_CONFIRM_BUTTONS,
            CART_ITEM_ROW_SELECTORS,
            CART_ROW_DELETE_BUTTONS,
        )

        snapshot = self._cart_rows_all(page)
        rows_now = snapshot.get("rows") or []
        degraded = bool(snapshot.get("degraded"))
        plan = _plan_cart_removal(rows_now, items)
        base = {
            "before": before,
            "cart_rows": len(rows_now),
            "cart_degraded": degraded,
            "delete": plan["delete"],
            "skip": plan["skip"],
            "missing": plan["missing"],
            "merged": plan.get("merged") or [],
            "targets": len(items),
        }

        if degraded:
            self._respond(
                screenshot=self._screenshot("cart_remove_degraded"),
                page_text=self.browser.get_page_text(1200),
                data=dict(
                    base,
                    removed=0,
                    after=before,
                    verified=False,
                    dry=dry,
                    code="CART_ROWS_NOT_RENDERED",
                    message=(
                        f"购物车里有 {before} 件，但一行都没读到（DOM 可能改名）"
                        "—— 没有执行删除，请刷新后重试"
                    ),
                ),
            )
            return

        if dry:
            self._respond(
                data=dict(
                    base,
                    removed=0,
                    after=before,
                    verified=False,
                    dry=True,
                    code=None,
                    message=(
                        f"计划删除 {len(plan['delete'])} 件，"
                        f"跳过 {len(plan['skip'])} 件，"
                        f"已合并不重复删 {len(plan.get('merged') or [])} 件，"
                        f"找不到 {len(plan['missing'])} 件"
                    ),
                ),
            )
            return

        ids = [d["item_id"] for d in plan["delete"] if d.get("item_id")]
        if not ids:
            self._respond(
                data=dict(
                    base,
                    removed=0,
                    after=before,
                    verified=False,
                    dry=False,
                    code="CART_NO_MATCH",
                    message="上一批记录里没有能在购物车定位到的商品，没有执行删除",
                ),
            )
            return

        # 扫描可能已经翻过页，回第一页再动手
        page.goto("https://cart.taobao.com/cart.htm", wait_until="commit", timeout=45000)
        with suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        self.browser._human_wait(1.5, 2.5)

        # 用 href 里的商品 id 精确定位行；id 后面一定是 & ? # 或链接结尾
        href_sel = ", ".join(
            f'a[href*="id={i}&"], a[href*="id={i}?"], a[href$="id={i}"]' for i in ids
        )

        def _targets():
            for sel in CART_ITEM_ROW_SELECTORS:
                loc = page.locator(sel)
                with suppress(Exception):
                    if not loc.count():
                        continue
                hit = loc.filter(has=page.locator(href_sel))
                with suppress(Exception):
                    if hit.count():
                        return hit
            return None

        # 回第一页后要等它渲染出行来：购物车首屏 3–5s 才出内容，
        # 直接找会空手而归、然后一声不响地判「没删掉」。
        for _ in range(20):
            if _targets() is not None:
                break
            self.browser._human_wait(0.8, 1.5)

        removed_titles: list[str] = []
        for _ in range(len(ids) + 6):
            hit = _targets()
            if hit is None:
                break
            row = hit.first
            with suppress(Exception):
                title = (row.inner_text() or "").split("\n")[0][:60]
                if title:
                    removed_titles.append(title)
            link = None
            for sel in CART_ROW_DELETE_BUTTONS:
                with suppress(Exception):
                    cand = row.locator(sel)
                    if cand.count():
                        link = cand.first
                        break
            if link is None:
                break
            link.click()
            self.browser._human_wait(0.7, 1.2)
            for sel in CART_DELETE_CONFIRM_BUTTONS:
                clicked = False
                with suppress(Exception):
                    loc = page.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click()
                        clicked = True
                if clicked:
                    break
            self.browser._human_wait(1.0, 1.6)

        after = self._wait_cart_count(page, timeout_s=12.0)
        verified = after < before
        if verified:
            message = (
                f"已删除 {len(removed_titles)} 件本工具加购的商品，"
                f"购物车 {before} → {after} 件"
            )
            code = None
        else:
            message = (
                f"点了删除，但件数没降（{before} → {after}）"
                "—— 可能没删掉，请刷新购物车核对"
            )
            code = "CART_DELETE_NOT_EFFECTIVE"

        self._respond(
            screenshot=self._screenshot("cart_remove_selected"),
            page_text=self.browser.get_page_text(1200),
            data=dict(
                base,
                removed=len(removed_titles),
                removed_titles=removed_titles,
                after=after,
                verified=verified,
                dry=False,
                message=message,
                code=code,
            ),
        )

    def _cmd_dom(self, req: dict[str, Any]) -> None:
        url = req.get("url")
        if url:
            page = self._page()
            page.goto(url, wait_until="commit", timeout=30000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self.browser._human_wait(1.0, 2.0)
        elif self.current_index is not None:
            item_url = self.items[self.current_index].get("url", "")
            if item_url:
                page = self._page()
                page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
                with suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
                self.browser._human_wait(1.0, 2.0)

        dom_data = self.browser.get_visible_dom()
        screenshot = self._screenshot("dom")

        self._respond(
            screenshot=screenshot,
            data={"dom": dom_data},
        )

    def _cmd_raw_dom(self, req: dict[str, Any]) -> None:
        """Return the raw HTML of the current page (read-only, for selector debugging).

        Optional: {"url": "...", "selector": "css"} — the selector limits the dump to
        matching subtrees so the response stays small.
        """
        page = self._page()
        url = req.get("url")
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            with suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            self.browser._human_wait(1.5, 2.5)

        html = ""
        selector = str(req.get("selector") or "").strip()
        try:
            if selector:
                html = "\n<!--SPLIT-->\n".join(page.locator(selector).evaluate_all(
                    "els => els.map(e => e.outerHTML)"
                ))
            else:
                html = page.content()
        except Exception as exc:  # noqa: BLE001
            self._error(f"读取 DOM 失败：{exc}")

        self._respond(
            screenshot=self._screenshot("raw_dom"),
            data={"html": html, "length": len(html), "url": page.url},
        )

    def _cmd_screenshot(self, req: dict[str, Any]) -> None:
        screenshot = self._screenshot("manual")
        page_text = self.browser.get_page_text(2000)
        self._respond(screenshot=screenshot, page_text=page_text, data={})

    def _cmd_ping(self, req: dict[str, Any]) -> None:
        """轻量探活。不搜不点不截图，只确认会话可用。

        放在验证闸门后面是有意的：调用方拿它当「验证是否已解除」的探针 ——
        页面还挂着滑块就回 VERIFICATION_REQUIRED，人工过完滑块再 ping 就会
        走到这里并让闸门自动复位。
        """
        page = self._page()
        self._respond(
            data={"pong": True, "url": page.url if page is not None else None,
                  "verification_pending": False},
        )

    def _cmd_quit(self, req: dict[str, Any]) -> None:
        _log("quit received, closing session")
        self._refresh_session_snapshot()
        self.cleanup()
        sys.exit(0)

    def _refresh_session_snapshot(self) -> None:
        """退出前把当前登录态存回快照。

        以前只在「首次扫码成功」那一刻存一次，之后快照永远是旧的；
        下次启动把旧 cookie 注回浏览器，淘宝就判成未登录 → 又要求扫码。
        """
        try:
            if self.browser.is_logged_in():
                self.session_flow.capture_after_login()
        except Exception as exc:  # noqa: BLE001
            _log(f"session snapshot refresh skipped: {exc}")

    # Command dispatch table
    _HANDLERS = {
        "search": _cmd_search,
        "open": _cmd_open,
        "select-sku": _cmd_select_sku,
        "cart-add": _cmd_cart_add,
        "cart-view": _cmd_cart_view,
        "cart-remove": _cmd_cart_remove,
        "dom": _cmd_dom,
        "raw-dom": _cmd_raw_dom,
        "screenshot": _cmd_screenshot,
        "ping": _cmd_ping,
        "quit": _cmd_quit,
    }

    def cleanup(self) -> None:
        with suppress(Exception):
            self.browser.close()
        _log("session closed")


# ──────────────────────────────────────────────
# CLI commands: check-session, clear-session
# ──────────────────────────────────────────────


def _cmd_check_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)

    if not session_path.exists():
        _output_json({
            "status": "success",
            "session_exists": False,
            "session_path": str(session_path),
            "message": "Session file does not exist. First use requires manual login.",
        })
        return

    try:
        with session_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        cookies = state.get("cookies", []) if isinstance(state, dict) else []
        _output_json({
            "status": "success",
            "session_exists": True,
            "session_path": str(session_path),
            "cookie_count": len(cookies),
            "message": f"Session file exists with {len(cookies)} cookies.",
        })
    except Exception as exc:
        _output_json({
            "status": "error",
            "session_exists": True,
            "session_path": str(session_path),
            "error": {"code": "SESSION_READ_ERROR", "message": str(exc)},
        })


def _cmd_clear_session(args: argparse.Namespace) -> None:
    session_path = _resolve_path(args.session_state_path or _DEFAULT_SESSION_PATH)
    removed = False
    if session_path.exists():
        session_path.unlink()
        removed = True
    _output_json({
        "status": "success",
        "session_removed": removed,
        "session_path": str(session_path),
        "message": "Session cleared." if removed else "No session file to clear.",
    })


# ──────────────────────────────────────────────
# CLI Argument Parser
# ──────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Taobao browser automation — persistent session mode."
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # session
    session_parser = subparsers.add_parser(
        "session",
        help="Start a persistent browser session (reads JSON commands from stdin)",
    )
    session_parser.add_argument(
        "--task-id",
        default=f"taobao-{uuid.uuid4().hex[:8]}",
        help="Task identifier (auto-generated if not provided)",
    )
    session_parser.add_argument(
        "--headless", action="store_true", help="Run browser in headless mode"
    )
    session_parser.add_argument(
        "--session-state-path",
        default=_DEFAULT_SESSION_PATH,
        help="Session file path",
    )

    # check-session
    check_parser = subparsers.add_parser(
        "check-session", help="Check session file status"
    )
    check_parser.add_argument(
        "--session-state-path", default=_DEFAULT_SESSION_PATH
    )

    # clear-session
    clear_parser = subparsers.add_parser(
        "clear-session", help="Clear persisted session"
    )
    clear_parser.add_argument(
        "--session-state-path", default=_DEFAULT_SESSION_PATH
    )

    return parser


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────


def main() -> int:
    global _JSON_OUT

    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    # Capture real stdout before redirecting sys.stdout → stderr.
    # This ensures only _output_json() writes to the real stdout.
    _JSON_OUT = sys.stdout
    sys.stdout = sys.stderr

    try:
        if args.command == "session":
            handler = SessionHandler(
                task_id=args.task_id,
                session_state_path=args.session_state_path,
                headless=args.headless,
            )
            handler.run()
        elif args.command == "check-session":
            _cmd_check_session(args)
        elif args.command == "clear-session":
            _cmd_clear_session(args)
        else:
            parser.print_help()
            return 1
    finally:
        sys.stdout = _JSON_OUT

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
