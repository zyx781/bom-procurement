#!/usr/bin/env python3
"""常驻「清空购物车」按钮的前端契约（读源码）。

背景（2026-11-19）：用户反馈「没点击商品时无法清空购物车」。原来唯一的
整车清空入口藏在台账抽屉的「维护」页里，必须先分析清单、再点开某一行，
抽屉才会出现。本测试锁住新的常驻入口：

- 入口在投放区之外，没上传 BOM、没点商品行也能看到；
- 结果写在页面顶部自己的容器，不写进隐藏的行详情；
- 仍走同一个后端接口，保留 prompt 二次确认和跑批守卫；
- 后端清空分支不依赖分析结果。
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")

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


def section_between(text, start_marker, end_marker):
    if start_marker not in text:
        return ""
    head = text.index(start_marker) + len(start_marker)
    tail = text.find(end_marker, head)
    return text[head:] if tail < 0 else text[head:tail]


# --- HTML：常驻入口不在结果面板/抽屉里 ---
check("有常驻购物车快捷条 #cart-quick", 'id="cart-quick"' in HTML)
check(
    "快捷条在结果面板之前（没上传清单就存在）",
    'id="cart-quick"' in HTML
    and 'id="result-panel"' in HTML
    and HTML.index('id="cart-quick"') < HTML.index('id="result-panel"'),
)
quick = section_between(HTML, 'id="cart-quick"', "</section>")
check("清空按钮在快捷条内", 'id="btn-cart-quick-clear"' in quick)
check("结果容器在快捷条内", 'id="cart-quick-result"' in quick)
check("结果容器可读屏播报", 'role="status"' in quick and 'aria-live="polite"' in quick)
check("清空按钮全页只有一个", HTML.count('id="btn-cart-quick-clear"') == 1)
check("快捷条有样式", ".cart-quick{" in CSS and ".cq-result{" in CSS)

# --- app.js：独立函数、独立容器、独立请求 ---
m = re.search(r"async function cartQuickClear\(\)\s*\{", APP)
check("有 cartQuickClear() 函数", bool(m))
body = APP[m.start():] if m else ""
end = body.find("\n  /** 只删上一批加购进去的商品")
if end > 0:
    body = body[:end]
check("页面顶部按钮绑定到 cartQuickClear", 'addEventListener("click", cartQuickClear)' in APP)
check("结果写进 #cart-quick-result", '$("#cart-quick-result")' in body)
check("不写进行详情里隐藏的结果容器", "#cart-remove-result" not in body and "#exec-add-result" not in body)
check("请求是整车清空", "{ all: true, confirm: \"清空\" }" in body)
check("请求走同一个清空接口", '"/api/taobao/cart-remove"' in body)
check("有会话就绪守卫", "sessionReady" in body)
check("跑批中不允许清空", "batchRunning" in body and "先点「停止」" in body)
check("finally 里恢复按钮文字", "finally" in body and "btn.textContent = oldText" in body)
check("清空按钮有忙碌中反馈", "清空中" in body)
check(
    "不依赖已选商品/分析结果",
    all(token not in body for token in ("state.result", "activeIndex", "ln.line_no", "exec.chosen")),
)

# --- 二次确认：和原有维护页按钮共用，不许绕开 ---
helper = APP[APP.find("function confirmClearCart"):] if "function confirmClearCart" in APP else ""
helper = helper[: helper.find("\n  }") + 5]
check("有统一的二次确认函数", "请输入：清空" in helper and 't !== "清空"' in helper)
check("整车清空都走二次确认", APP.count("confirmClearCart()") >= 2)

# --- server.py：清空分支不要求分析结果 ---
idx = SERVER.find('path == "/api/taobao/cart-remove"')
seg = SERVER[idx: idx + 1400] if idx >= 0 else ""
check("后端仍有整车清空分支", "remove_all = bool(body.get(\"all\"))" in seg)
check("后端仍校验确认词", 'body.get("confirm") or "") != "清空"' in seg)
check("清空分支不读 active-bom / 分析结果", all(k not in seg for k in ("active_bom", "state.result", "lines =")))

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
