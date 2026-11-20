#!/usr/bin/env python3
"""购物车对账：无限滚动读全 + 读不全时误报保护（2026-11-18）。

真机现象：购物车显示 68 件，旧代码只读到 29 行（新版购物车无限滚动，
没有「下一页」按钮），对账把 10 个已加购成功（xhr ok）的 ok 行误判成
「购物车里没找到」→ uncertain。本测试用假状态覆盖两条修复：
  1. cart-view 会先滚动加载全部行，再抽取；
  2. 万一读到的行数远少于显示件数，不逐行降级，只标「未核实」。
"""

import os
import pathlib
import sys
import types

BASE = pathlib.Path(__file__).parent
SKILL = pathlib.Path(os.environ.get("TAOBAO_SKILL_DIR") or (BASE.parent / "skills" / "taobao-search" / "scripts"))
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


print("== A. cart-view 滚动加载全部行 ==")
taobao_src = (SKILL / "taobao.py").read_text(encoding="utf-8")
check("有滚动加载函数", "def _cart_scroll_load_all" in taobao_src)
check("_cart_rows_all 先滚动再抽取",
      taobao_src.split("def _cart_rows_all", 1)[1].split("def ", 1)[0].find(
          "_cart_scroll_load_all") >= 0)
check("滚到行数连续不增为止（不是滚固定次数就完）", "stable >= 2" in taobao_src)
check("滚动只读 DOM 不点按钮（wheel + scrollTo）",
      "window.scrollTo" in taobao_src and "mouse.wheel" in taobao_src)
check("有上限防死循环", "max_rounds" in taobao_src)

# 用假 Page 模拟：初始 29 行，滚动两次到 68 行后稳定。
class ScrollPage:
    def __init__(self):
        self.n = 29
        self.wheels = 0

    def evaluate(self, js, sel=None):
        if "querySelectorAll" in js:
            return self.n
        return None

    def wait_for_timeout(self, _ms):
        # 每次滚动加载 20 行，直到 68
        if self.n < 68:
            self.n = min(68, self.n + 20)

    def mouse(self):
        return self

    def wheel(self, _a, _b):
        self.wheels += 1


# 直接调真方法（它是 SessionHandler 上的方法，用 types.SimpleNamespace 提供 self）
import taobao  # noqa: E402

sp = ScrollPage()
page_obj = types.SimpleNamespace(
    evaluate=sp.evaluate,
    mouse=types.SimpleNamespace(wheel=sp.wheel),
    wait_for_timeout=sp.wait_for_timeout,
)
# .bind 一个假 self 调方法
fake_self = types.SimpleNamespace()
final_n = taobao.SessionHandler._cart_scroll_load_all(fake_self, page_obj, settle=0.01)
check("假 Page：29 → 68 行全部加载", final_n == 68, final_n)
check("稳定后停止滚动（没滚满 12 轮）", sp.wheels <= 6, sp.wheels)

print("== B. 读车不全时不把 ok 行误降级 ==")


class FakeRunner:
    """只带 _reconcile_cart 需要的状态锁和 _batch。"""

    def __init__(self, results, cart_rows, cart_count):
        import threading
        self._state_lock = threading.RLock()
        self._batch = {"results": results}
        self._cart = {
            "rows": [{"title": t} for t in cart_rows],
            "cart_item_count": cart_count,
            "rows_degraded": False,
        }
        self.logs = []

    def _reconcile(self):
        # 直接复用真方法（把 self 换成 FakeRunner 的实例方法绑定）
        tr.SessionRunner._reconcile_cart(self, self._cart)
        return self._batch


# 45 个 ok 行，标题都各不相同；购物车只读到 29 行，显示 68 件
ok_rows = [{"line_no": i, "status": "ok", "candidate": {"title": f"料号TEST{i:03d} 封装0805"}}
           for i in range(1, 46)]
cart_rows = [f"料号TEST{i:03d} 封装0805" for i in range(1, 30)]  # 只有 29 行
r = FakeRunner(ok_rows, cart_rows, 68)
b = r._reconcile()
rec = b["reconcile"]
check("读车不全 → 判为 anomaly（不是逐行降级）", rec["anomaly"] is True, rec)
check("读车不全 → downgraded 为空", rec["downgraded"] == [], rec["downgraded"])
check("读车不全 → missed 行标 cart_unverified 而非 uncertain",
      all(x.get("status") == "ok" and x.get("cart_unverified") for x in b["results"]
          if x.get("line_no") in (30, 40)), "")
check("异常原因说明是滚动加载不全", "滚动加载不全" in rec["anomaly_reason"],
      rec["anomaly_reason"])

# 对照：读到全部 68 行时，正常匹配，不触发 anomaly
cart_full = [f"料号TEST{i:03d} 封装0805" for i in range(1, 69)]
r2 = FakeRunner([dict(x) for x in ok_rows], cart_full, 68)
b2 = r2._reconcile()
check("读全 68 行 → 不判异常", b2["reconcile"]["anomaly"] is False, b2["reconcile"])
check("读全 → 45 个 ok 全部匹配", b2["reconcile"]["matched"] == 45,
      b2["reconcile"]["matched"])
check("读全 → 无降级", b2["reconcile"]["downgraded"] == [])

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
