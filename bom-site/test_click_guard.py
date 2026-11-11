#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点击守卫单测：滚进视口 / 禁用判定 / 遮挡命中测试 / 返回值语义。

背景（2026-11-09）：加购「有时候点了没加上」，根因之一是按视口坐标硬点 ——
按钮在折叠线以下就被点到别处，页面没有任何反馈，最后变成 uncertain。
这里用假 locator/page 把三条守卫钉死，防止以后改回去。
"""

import sys
import pathlib

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent / "Taobao-Search-Skill" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


from browser_adapter import BrowserAdapter  # noqa: E402


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []

    def move(self, x, y, steps=None):
        self.moves.append((round(x), round(y), steps))

    def click(self, x, y):
        self.clicks.append((round(x), round(y)))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()


class FakeLocator:
    """box=(x,y,w,h)；hits=每次 evaluate 命中测试的结果（按调用顺序取）。"""

    def __init__(self, box=(100, 800, 120, 40), enabled=True, hits=None):
        self.box = box
        self.enabled = enabled
        self.hits = list(hits if hits is not None else [True])
        self.scrolled = False
        self.fallback_clicked = False
        self.hit_queries = []

    def scroll_into_view_if_needed(self, timeout=None):
        self.scrolled = True

    def is_enabled(self):
        return self.enabled

    def bounding_box(self, timeout=None):
        return dict(zip(("x", "y", "width", "height"), self.box))

    def evaluate(self, expr, arg=None):
        self.hit_queries.append(arg)
        if not self.hits:
            return False
        return self.hits.pop(0) if len(self.hits) > 1 else self.hits[0]

    def click(self, timeout=None):
        self.fallback_clicked = True


ba = BrowserAdapter.__new__(BrowserAdapter)  # 不走 __init__，只测纯逻辑

print("== 正常点击：先滚进视口，命中后按坐标点 ==")
page, loc = FakePage(), FakeLocator(box=(100, 800, 120, 40), hits=[True])
ok = ba._human_click(page, loc)
check("返回 True", ok is True, ok)
check("先 scroll_into_view", loc.scrolled is True)
check("确实发出了鼠标点击", len(page.mouse.clicks) == 1, page.mouse.clicks)
check("点击坐标落在按钮框内",
      loc.box[0] <= page.mouse.clicks[0][0] <= loc.box[0] + loc.box[2]
      and loc.box[1] <= page.mouse.clicks[0][1] <= loc.box[1] + loc.box[3],
      page.mouse.clicks)

print("== 按钮禁用：直接返回 False，不发点击 ==")
page, loc = FakePage(), FakeLocator(enabled=False, hits=[True])
ok = ba._human_click(page, loc)
check("返回 False", ok is False, ok)
check("没有发鼠标点击", page.mouse.clicks == [], page.mouse.clicks)
check("没有走兜底 click", loc.fallback_clicked is False)

print("== 被弹层遮住：换点都命中不到 → False，不许瞎点 ==")
page, loc = FakePage(), FakeLocator(hits=[False, False, False, False])
ok = ba._human_click(page, loc)
check("返回 False", ok is False, ok)
check("四次候选点都试过", len(loc.hit_queries) == 4, loc.hit_queries)
check("没有发鼠标点击", page.mouse.clicks == [], page.mouse.clicks)

print("== 第一个点被遮、第二个点命中：照样能点 ==")
page, loc = FakePage(), FakeLocator(hits=[False, True])
ok = ba._human_click(page, loc)
check("返回 True", ok is True, ok)
check("发了点击", len(page.mouse.clicks) == 1, page.mouse.clicks)

print("== bounding_box 取不到：退回 Playwright click（自带可操作性检查）==")
page = FakePage()
loc = FakeLocator()
loc.bounding_box = lambda timeout=None: None
ok = ba._human_click(page, loc)
check("返回 True", ok is True, ok)
check("走了兜底 click", loc.fallback_clicked is True)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
