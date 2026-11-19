#!/usr/bin/env python3
"""晶振频率识别：kHz / 丝印写法 / 裸数字都要认出来。

现场问题（2026-11-20）：一颗 32.768kHz 的 RTC 晶振，选型台直接报
「晶振必须有频率」，整行卡成待确认。根因是 `_parse_freq_label` 只写了
`MHz` 一条正则 —— 32.768kHz 这种**最常见**的晶振频率一个都读不到。

这里锁三件事：
1. 各种写法都能读出频率，且不会被封装尺寸（3225 / 5032）骗到；
2. 读到频率的晶振行直接 ready，不再拦人；
3. 真读不到时，报错文案要说「怎么写才认」，而不是只说「必须有频率」。
"""

import sys

import engine
import rules

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


def eq(name, got, want):
    check(name, got == want, f"got={got!r} want={want!r}")


print("== 1. 各种写法都要读出来 ==")
CASES = [
    ("8MHz ABM3", "8MHz"),
    ("50 MHz", "50MHz"),
    ("32.768kHz", "32.768kHz"),
    ("32.768KHZ", "32.768kHz"),
    ("32.768 kHz 12.5pF", "32.768kHz"),
    ("32.768KHz SMD3225", "32.768kHz"),
    ("32768Hz", "32768Hz"),
    ("32K768", "32.768kHz"),
    ("32.768K", "32.768kHz"),
    ("8M", "8MHz"),
    ("8M000", "8MHz"),
    ("32768", "32.768kHz"),
    ("8.000MHZ", "8.000MHz"),
    ("1GHz", "1GHz"),
]
for text, want in CASES:
    eq(f"{text!r} → {want}", engine._parse_freq_label(text), want)

print("== 2. 不能把封装尺寸当频率 ==")
for text in ("XTAL-3225", "HC-49S SMD3225", "SMD 贴片", "20ppm 12.5pF", "", "5032 4P"):
    eq(f"{text!r} 不误判", engine._parse_freq_label(text), "")

print("== 3. 32.768kHz 晶振行要直接可执行 ==")
line = engine.analyze_line(
    {
        "line_no": 7,
        "mpn": "",
        "desc": "32.768kHz 12.5pF 20ppm",
        "package": "SMD-3215",
        "qty": 1,
        "refs": "X1",
    },
    7,
    {},
)
eq("细分是 crystal", line.get("subtype"), "crystal")
eq("频率进参数", (line.get("params") or {}).get("frequency"), "32.768kHz")
eq("状态 ready", line.get("status"), "ready")
eq("不再缺频率", line.get("missing"), [])
eq("硬门槛带频率", any("频率 = 32.768kHz" in g for g in line.get("hard_gates") or []), True)
eq(
    "搜索词带 32.768kHz 兜底词",
    any("32.768kHz" in q for q in line.get("query_plan") or []),
    True,
)
check("不再被读成 8MΩ 那种阻值", not (line.get("params") or {}).get("resistance_ohm"), line.get("params"))

print("== 4. 丝印 32K768 的型号也能锁规格 ==")
line2 = engine.analyze_line(
    {"line_no": 8, "mpn": "32K768-3225", "desc": "SMD", "package": "SMD3225",
     "qty": 2, "refs": "X2"},
    8,
    {},
)
eq("型号里的 32K768 读出 32.768kHz", (line2.get("params") or {}).get("frequency"), "32.768kHz")
eq("状态 ready", line2.get("status"), "ready")

print("== 5. 真读不到时文案要说清怎么写才认 ==")
bad = engine.analyze_line(
    {"line_no": 9, "mpn": "XTAL-3225", "desc": "SMD 贴片", "package": "SMD",
     "qty": 1, "refs": "X1"},
    9,
    {},
)
eq("拦成待确认", bad.get("status"), "needs_user")
eq("缺的是频率", bad.get("missing"), ["频率"])
_block = " ".join(bad.get("blocking") or [])
check("文案提到可识别的写法", "32.768kHz" in _block and "32K768" in _block, _block)
check("文案不再只说「必须有频率」", "必须有频率" not in _block, _block)

print("== 6. 下游 SKU 比对能解析同一个值 ==")
eq("rules 解析 32.768kHz", rules._sku_freq("32.768kHz"), 32768.0)
eq("rules 解析 32.768KHz 大写", rules._sku_freq("32.768KHZ"), 32768.0)
eq("同频率不判冲突", rules._sku_veto(line, "规格", "32.768kHz") or "", "")

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
