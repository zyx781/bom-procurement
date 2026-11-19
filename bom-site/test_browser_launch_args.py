#!/usr/bin/env python3
"""调试浏览器启动参数：绝不能同时恢复上次标签又传淘宝 URL。

2026-11-19 真人复现：点「启动淘宝」出来两个淘宝页。根因是
ensure_debug_browser 同时传了 --restore-last-session（恢复上次标签）
和 https://www.taobao.com（命令行新开一个），两个叠在一起。

本测试用假 Popen 捕获真实调用参数，不拉浏览器、不写真实 runs。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).parent
sys.path.insert(0, str(BASE))

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


# ---- 隔离：假浏览器、假 CDP、临时 profile ----
tmp = pathlib.Path(tempfile.mkdtemp(prefix="bom_launch_args_"))
tr.RUNS = tmp / "runs"
tr.RUNS.mkdir(parents=True, exist_ok=True)
tr.CDP_PROFILE_DIR = tmp / "profile"
tr.find_browser_exe = lambda: "C:/fake/msedge.exe"
tr.cdp_endpoint_ok = lambda url, timeout=1.2: False

captured: dict[str, list[str]] = {}
real_popen = tr.subprocess.Popen


class FakePopen:
    def __init__(self, args, **kwargs):
        captured["args"] = [str(a) for a in args]


tr.subprocess.Popen = FakePopen  # type: ignore[assignment]
try:
    # wait_seconds 很小：CDP 永远不就绪会抛错，但参数已经被捕获了
    try:
        tr.ensure_debug_browser("http://127.0.0.1:9333", wait_seconds=0.05)
    except Exception:
        pass
finally:
    tr.subprocess.Popen = real_popen  # type: ignore[assignment]

args = captured.get("args") or []
check("捕获到启动命令", bool(args), repr(args))
check("不恢复上次标签", "--restore-last-session" not in args, repr(args))
check("不传独立淘宝 URL", not any("taobao.com" in a for a in args), repr(args))
check("保留防还原气泡参数", "--hide-crash-restore-bubble" in args, repr(args))
check("仍带调试端口", any(a.startswith("--remote-debugging-port=") for a in args), repr(args))
check("仍带独立 profile", any(a.startswith("--user-data-dir=") for a in args), repr(args))

# ---- launcher.ps1 的手动 edge 入口同样不能开两个 ----
ps1 = (BASE / "launcher.ps1").read_text(encoding="utf-8")
arg_lines = [ln for ln in ps1.splitlines() if "$argLine" in ln and "remote-debugging-port" in ln]
check("launcher 有启动参数行", len(arg_lines) == 1, repr(arg_lines))
line = arg_lines[0] if arg_lines else ""
check("launcher 不恢复上次标签", "--restore-last-session" not in line, line)
check("launcher 不传独立淘宝 URL", "taobao.com" not in line, line)
check("launcher 保留防还原气泡参数", "--hide-crash-restore-bubble" in line, line)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
