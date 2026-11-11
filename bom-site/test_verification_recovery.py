#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证自恢复测试（2026-11-08 规则变更）。

规则：遇到验证（滑块 / 图片点选 / 风控中间页）先自己刷新恢复；
      **刷新后还能出商品就接着干**，只有刷新完仍然没有商品才升级为人工验证。

用假 Page 覆盖执行层的分支，用纯函数覆盖批量层的重试策略，不碰真浏览器。
"""

import os
import pathlib
import sys

BASE = pathlib.Path(__file__).parent
SKILL = pathlib.Path(r"D:\测试\Taobao-Search-Skill\scripts")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(SKILL))

import browser_adapter as BA  # noqa: E402
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


SEARCH_URL = "https://s.taobao.com/search?page=1&q=x&tab=all"
CART_URL = "https://cart.taobao.com/cart.htm"


class FakePage:
    def __init__(self, url=SEARCH_URL):
        self.url = url
        self.reloads = 0
        self.gotos = 0

    def reload(self, **_kw):
        self.reloads += 1
        return None

    def goto(self, url, **_kw):
        self.url = url
        self.gotos += 1
        return None

    def wait_for_load_state(self, *_a, **_kw):
        return None


class FakeAdapter(BA.BrowserAdapter):
    """只实现恢复逻辑用到的那几个钩子，不启动浏览器。"""

    def __init__(self, blocked, cards=None, url=SEARCH_URL, cards_ms=123):
        self._page = FakePage(url)
        self._blocked = list(blocked)
        self._cards_seq = list(cards or [])
        self._cards_ms = cards_ms
        self.waited = 0

    # —— 被替换的钩子 ——
    def _ensure_page(self):
        return self._page

    def _looks_access_blocked(self, _page=None):
        return self._blocked.pop(0) if self._blocked else False

    def risk_middleware_hit(self):
        return False

    def _main_has_cards(self, _page):
        return self._cards_seq.pop(0) if self._cards_seq else True

    def _human_wait(self, _a=0.0, _b=0.0):
        self.waited += 1

    def wait_for_cards(self, timeout_ms=0, poll_ms=0, settle_ms=0):
        return self._cards_seq.pop(0) if self._cards_seq else self._cards_ms


print("== A. 执行层：刷新自恢复 ==")

a = FakeAdapter(blocked=[False], cards=[120])
check("刷新后不拦且出卡 → 恢复成功", a.recover_from_verification() is True)
check("只刷新 1 次", a._page.reloads == 1, a._page.reloads)

b = FakeAdapter(blocked=[True, True], cards=[True, True])
check("刷两次都还拦着 → 判失败（交给人工）", b.recover_from_verification() is False)
check("第二次改用整页导航（不是 reload）", b._page.gotos == 1 and b._page.reloads == 1,
      (b._page.reloads, b._page.gotos))

c = FakeAdapter(blocked=[False, False], cards=[-1, -1])
check("刷新后不再弹验证但**没有商品** → 仍判失败（这正是要人工的那种）",
      c.recover_from_verification() is False)

c2 = FakeAdapter(blocked=[False, False], cards=[-1, 120])
check("第一次刷完没商品、第二次刷出商品 → 恢复成功（多刷一次是对的）",
      c2.recover_from_verification() is True)

c3 = FakeAdapter(blocked=[False], cards=[-2])
check("刷新后是「明确空结果」页 → 算正常页面（不是验证）",
      c3.recover_from_verification() is True)

d = FakeAdapter(blocked=[False], cards=[], url=CART_URL)
check("购物车页：不看商品卡，只看验证弹层", d.recover_from_verification() is True)
check("购物车页也刷新了 1 次", d._page.reloads == 1)

print("== B. 执行层：_handle_captcha_if_present 先自恢复再决定 ==")

e = FakeAdapter(blocked=[False], cards=[])
e._handle_captcha_if_present(e._page)
check("没验证 → 不刷新、不抛异常", e._page.reloads == 0)

f = FakeAdapter(blocked=[True, False], cards=[120])
f._handle_captcha_if_present(f._page)
check("有验证但刷新能恢复 → 不抛异常（接着干）", f._page.reloads == 1, f._page.reloads)

g = FakeAdapter(blocked=[True, True, True], cards=[-1, -1])
raised = False
try:
    g._handle_captcha_if_present(g._page)
except BA.VerificationRequired:
    raised = True
check("刷新后仍无商品 → 才抛 VerificationRequired", raised)
check("抛之前已经刷/导航够次数", g._page.reloads + g._page.gotos == BA.BrowserAdapter.RECOVER_TRIES,
      (g._page.reloads, g._page.gotos))

print("== C. 执行层：_recover_cards（search 里 -3 之后走这条）==")

h = FakeAdapter(blocked=[False], cards=[120, 777])
check("中间页后刷新恢复 → 拿到全新卡片数", h._recover_cards(h._page, 5000) == 777)

i = FakeAdapter(blocked=[True, True], cards=[True, True])
check("中间页刷新两次仍在 → 返回 -3（=仍要人工）", i._recover_cards(i._page, 5000) == -3)

print("== D. 批量层：撞验证先自动重试，用完才等人工 ==")

check("第一次撞验证 → auto（立刻重试，不等人）", tr.verification_next_step(False, 0, 0) == "auto")
check("自动重试还有额度 → auto", tr.verification_next_step(False, 1, 0) == "auto")
check("自动重试用完 → human（刷新后仍无商品，才等人工）",
      tr.verification_next_step(False, tr.VERIFY_AUTO_RETRY, 0) == "human")
check("人工额度还有 → human", tr.verification_next_step(False, tr.VERIFY_AUTO_RETRY, 2) == "human")
check("人工也用完 → stop", tr.verification_next_step(False, tr.VERIFY_AUTO_RETRY, tr.VERIFY_HUMAN_RETRY) == "stop")
check("已点过加购 → 一律 stop（绝不重跑，防重复下单）", tr.verification_next_step(True, 0, 0) == "stop")

check("自动重试次数默认 >=1", tr.VERIFY_AUTO_RETRY >= 1, tr.VERIFY_AUTO_RETRY)
check("自动重试间隔有下限（别把风控逼急）", tr.VERIFY_RETRY_GAP_S >= 5, tr.VERIFY_RETRY_GAP_S)

print("== E. 文案契约：暂停原因要说清「刷新后仍无商品」 ==")
src = (BASE / "taobao_runner.py").read_text(encoding="utf-8")
check("批量暂停原因含『刷新后仍无商品』", "刷新后仍无商品" in src)
check("等待时的 phase 也说清了原因", "刷新后仍无商品，等人工验证" in src)
skill_src = (SKILL / "browser_adapter.py").read_text(encoding="utf-8")
check("执行层不再「一碰验证就 raise」", skill_src.count("raise VerificationRequired()") <= 3,
      skill_src.count("raise VerificationRequired()"))
check("执行层有恢复函数", "def recover_from_verification" in skill_src)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
