#!/usr/bin/env python3
"""「查看明细」按钮的结果必须写在自己所在的面板里（前端契约，读源码）。

背景（2026-11-18）：用户反馈维护页的「查看明细」按钮按不动。真机排查发现
按钮点击和后端 API 都正常，但 execCart() 一律把结果写进 #exec-add-result ——
那个容器在「单行执行」标签页里。用户只要跑过一次单行执行，SKU 面板就创建了
#exec-add-result，之后从「维护」页点查看明细，结果就跑到隐藏的另一页，界面
毫无变化，看着就像按不动。

修法：维护页给独立容器 #cart-view-result，点击时显式传给它。
本测试锁住这条契约，防止以后又把结果写回错误的面板。
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f"  [{extra}]" if extra else ""))


# --- HTML：维护页有独立结果容器 ---
check("维护页有 #cart-view-result 容器", 'id="cart-view-result"' in HTML)
check(
    "查看明细按钮仍在维护 pane 内",
    'id="btn-cart-view"' in HTML
    and HTML.index('data-pane="care"') < HTML.index('id="btn-cart-view"'),
)

# --- execCart 接受目标容器参数，且优先用它 ---
m = re.search(r"async function execCart\(([^)]*)\)", APP)
check("execCart 接受 targetSel 参数", bool(m) and "targetSel" in (m.group(1) if m else ""),
      m.group(1) if m else "未找到函数定义")
body = APP[m.start(): m.start() + 1200] if m else ""
check(
    "execCart 优先写 targetSel 指定的容器",
    "targetSel && $(targetSel)" in body and "$(targetSel)" in body,
)

# --- 两个调用点各写各的面板 ---
check(
    "维护页查看明细 → 传 #cart-view-result",
    'execCart("#cart-view-result")' in APP,
)
check(
    "单行执行查看购物车 → 无参（写 #exec-add-result）",
    '() => execCart()' in APP or 'addEventListener("click", execCart)' in APP,
)

# --- 即时反馈：点击后立刻有 loading，不再让用户以为没反应 ---
check("点击后有即时反馈文案", "正在读取购物车" in body)

# --- 旧的 bug 写法不再出现：不能无条件写死 #exec-add-result ---
check(
    "不再无条件写 #exec-add-result",
    'let box = $("#exec-add-result")' not in body,
)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
