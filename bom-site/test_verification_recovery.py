#!/usr/bin/env python3
"""验证自恢复测试（2026-11-08 规则变更）。

规则：遇到验证（滑块 / 图片点选 / 风控中间页）先自己刷新恢复；
      **刷新后还能出商品就接着干**，只有刷新完仍然没有商品才升级为人工验证。

用假 Page 覆盖执行层的分支，用纯函数覆盖批量层的重试策略，不碰真浏览器。
"""

import os
import pathlib
import sys
import time

BASE = pathlib.Path(__file__).parent
# 执行层脚本在同仓库的 skills/taobao-search/scripts（以前这里写死 D:\测试\…，
# 换机器/CI 直接 ModuleNotFoundError）。允许用 TAOBAO_SKILL_DIR 覆盖。
SKILL = pathlib.Path(os.environ.get("TAOBAO_SKILL_DIR") or (BASE.parent / "skills" / "taobao-search" / "scripts"))
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

print("== F. 自动过滑块：先拖，拖不过再刷（2026-11-18）==")

from slider_solver import SliderSolver, drag_distance_for, pick_mode  # noqa: E402

check("模式：有背景图 + 按钮 → 拼图", pick_mode(True, True) == "puzzle")
check("模式：只有按钮 → 拖到最右（阿里 nc）", pick_mode(False, True) == "drag_end")
check("模式：连按钮都没有 → 不解（不能对着普通页面乱拖）", pick_mode(False, False) is None)
check("拖到最右：算到轨道右端留 2px", drag_distance_for(100, 300, 108, 30) == 275,
      drag_distance_for(100, 300, 108, 30))
ok_av, why_av = SliderSolver.available()
check("可用性探测返回 (bool, 原因)", isinstance(ok_av, bool) and bool(why_av), (ok_av, why_av))


class SolveAdapter(FakeAdapter):
    """能记录「解滑块」调用的适配器。"""

    def __init__(self, blocked, cards=None, solved=True):
        super().__init__(blocked, cards)
        self.solved = solved
        self.solver_calls = 0
        self._slider_uses = 0

    def try_solve_verification(self, page=None):
        self.solver_calls += 1
        return self.solved


f1 = SolveAdapter(blocked=[True, False], cards=[])
f1._handle_captcha_if_present(f1._page)
check("滑块拖过 → 不再刷新页面", f1._page.reloads == 0 and f1.solver_calls == 1,
      (f1._page.reloads, f1.solver_calls))

f2 = SolveAdapter(blocked=[True, False], cards=[120], solved=False)
f2._handle_captcha_if_present(f2._page)
check("滑块没拖过 → 退回刷新自恢复", f2.solver_calls == 1 and f2._page.reloads == 1,
      (f2.solver_calls, f2._page.reloads))

f3 = SolveAdapter(blocked=[True, True, False], cards=[120], solved=True)
f3._handle_captcha_if_present(f3._page)
check("解完仍判拦截（弹层没消）→ 继续刷新，不误判成功",
      f3._page.reloads == 1, f3._page.reloads)

f4 = FakeAdapter(blocked=[])
check("假 Page（无 evaluate）→ 直接跳过，不炸", f4.try_solve_verification() is False)

f5 = FakeAdapter(blocked=[])
f5._slider_uses = BA.BrowserAdapter.SLIDER_MAX_PER_SESSION
check("本会话次数用完 → 转人工", f5.try_solve_verification() is False)

f6 = FakeAdapter(blocked=[])
_old_switch = BA.BrowserAdapter.SLIDER_SOLVE
BA.BrowserAdapter.SLIDER_SOLVE = False
try:
    check("开关关掉 → 一次都不试", f6.try_solve_verification() is False)
finally:
    BA.BrowserAdapter.SLIDER_SOLVE = _old_switch

f7 = FakeAdapter(blocked=[])
f7._slider_cooldown = time.monotonic() + 30
check("拖失败后的冷却期内不再重试（防 17 轮死循环）", f7.try_solve_verification() is False)
check("冷却时长可配", "TAOBAO_SLIDER_COOLDOWN" in skill_src)

_src_handle = skill_src.split("def _handle_captcha_if_present", 1)[1][:1200]
check("顺序契约：先试滑块，再刷新",
      _src_handle.find("try_solve_verification") < _src_handle.find("recover_from_verification")
      and "try_solve_verification" in _src_handle)
check("命令循环里也先试滑块", "auto slider" in (SKILL / "taobao.py").read_text(encoding="utf-8"))
_slider_src = (SKILL / "slider_solver.py").read_text(encoding="utf-8")
check("求解器缺包不影响 import（重依赖只在函数里加载）",
      "import ddddocr" not in _slider_src.split("def _load_ddddocr", 1)[0])
check("求解器有总时限，不会挂死", "deadline" in _slider_src)

print("== G. 启动不阻塞 + auto-hide 不闪退（2026-11-18 启动卡死修复）==")

# 启动导航里不再解滑块：源码层契约 —— navigate_to_taobao 函数体内不能出现
# _handle_captcha_if_present / try_solve_verification（否则启动又被卡几分钟）。
_nav_body = skill_src.split("def navigate_to_taobao(self) -> None:", 1)[1]
_nav_body = _nav_body.split("\n    def ", 1)[0]
# 剥掉注释行，只看真实代码（注释里会提到这些函数名）
_nav_code = "\n".join(ln for ln in _nav_body.splitlines() if not ln.lstrip().startswith("#"))
check("启动导航不在这里解滑块（先把 ready 发出去）",
      "_handle_captcha_if_present(" not in _nav_code
      and "try_solve_verification(" not in _nav_code, _nav_code[:200])
check("启动导航仍有验证检测（只是不就地求解）", "_looks_access_blocked" in _nav_body)

# 求解器用 page 级超时收紧，防止 bounding_box/evaluate 卡满 30s。
check("求解器有超时收紧上下文", "_tight_timeout" in _slider_src
      and "set_default_timeout" in _slider_src)
check("超时时长可配", "TAOBAO_SLIDER_OP_MS" in _slider_src)

# 假 Page 没有 set_default_timeout：收紧上下文要安静跳过，不能炸。
class NoTimeoutPage:
    def set_default_timeout(self, _ms):
        raise AttributeError("fake page")

with SliderSolver._tight_timeout(NoTimeoutPage(), 2000):
    _tight_ok = True
check("假 Page 无 set_default_timeout → 跳过且不炸", _tight_ok)

# 窗口策略（2026-11-19 简化）：不自动藏，点「显示淘宝窗口」就是一次显式操作。
_runner_src = (BASE / "taobao_runner.py").read_text(encoding="utf-8")
_show_body = _runner_src.split("def show_browser", 1)[1].split("def ", 1)[0]
check("显示窗口不抢前台（window_pos.show）", "window_pos.show(" in _show_body, _show_body[:300])
check("不再有「自动藏」标记", "_autohide_done" not in _runner_src)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
