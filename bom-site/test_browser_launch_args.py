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
check("带一个淘宝启动页（避免空白新建标签页）",
      any("taobao.com" in a for a in args), repr(args))
check("淘宝启动页恰好一个",
      sum("taobao.com" in a for a in args) == 1, repr(args))
check("保留防还原气泡参数", "--hide-crash-restore-bubble" in args, repr(args))
check("仍带调试端口", any(a.startswith("--remote-debugging-port=") for a in args), repr(args))
check("仍带独立 profile", any(a.startswith("--user-data-dir=") for a in args), repr(args))
# 窗口必须开在屏幕内：登录/过验证都要人看得见（2026-11-19 现场：窗口开在 -2000，
# 用户只看到「界面一开始就是隐藏的，我该怎么登录」）。
check("窗口位置在屏幕内", "--window-position=100,60" in args, repr(args))
check("不再把窗口放到屏幕外", not any(a == "--window-position=-2000,0" for a in args), repr(args))

# ---- 淘宝历史清理由浏览器里的扩展实时完成（外部进程打不开独占锁） ----
check("带历史清理扩展", any(a.startswith("--load-extension=") and "history_guard" in a for a in args), repr(args))
check("只加载这一个扩展",
      any(a.startswith("--disable-extensions-except=") and "history_guard" in a for a in args), repr(args))
ext_dir = BASE / "browser_ext" / "history_guard"
check("扩展文件随包存在", (ext_dir / "manifest.json").is_file() and (ext_dir / "sw.js").is_file())
check("扩展在淘宝启动页之前（URL 必须是最后一个实参）",
      args.index("https://www.taobao.com") == len(args) - 1, repr(args[-3:]))

# ---- 默认单窗口：无痕工作区默认关，显式开才生效 ----
adapter_src = (SKILL := BASE.parent / "skills" / "taobao-search" / "scripts" / "browser_adapter.py").read_text(encoding="utf-8")
check("无痕默认关闭", 'os.environ.get("TAOBAO_PRIVATE_WINDOW") or "0"' in adapter_src)
check("无痕仍可显式开启", '"0", "false", "off", "no"' in adapter_src)

# ---- launcher.ps1 的手动 edge 入口同样不能开两个 ----
ps1 = (BASE / "launcher.ps1").read_text(encoding="utf-8")
arg_lines = [ln for ln in ps1.splitlines() if "$argLine" in ln and "remote-debugging-port" in ln]
check("launcher 有启动参数行", len(arg_lines) == 1, repr(arg_lines))
line = arg_lines[0] if arg_lines else ""
check("launcher 不恢复上次标签", "--restore-last-session" not in line, line)
check("launcher 带一个淘宝启动页", "https://www.taobao.com" in line, line)
check("launcher 保留防还原气泡参数", "--hide-crash-restore-bubble" in line, line)
check("launcher 窗口开在屏幕内", "--window-position=100,60" in line, line)
check("launcher 不再把窗口放到屏幕外", "--window-position=-2000" not in line, line)
check("launcher 手动入口也带历史清理扩展",
      "--load-extension" in ps1 and "--disable-extensions-except" in ps1 and "{2}" in line, line)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
