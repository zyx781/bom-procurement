"""没装 Python 的机器上，启动器不能说错话。

现场：Windows 10/11 自带一个 0 字节的假 python.exe
（%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe，ReparsePoint）。
Get-Command 能找到它，Test-Path 也返回真，但执行它只会打开 Microsoft Store。

踩过的坑一：启动器把它当成真 Python 收进候选，于是
  · 版本探测失败 → 显示「已找到，版本未知」
  · 依赖探测失败 → 报「Python 缺依赖（playwright / openpyxl）」
  · 用户照着提示去装依赖，装完还是起不来，中途还莫名弹出应用商店
踩过的坑二：Get-Command 默认只返回第一个命中。用户级 PATH 里 WindowsApps
常常排在真 Python 前面，于是「明明装了 Python，启动器说没装」。

正确行为：认出占位符并跳过，但继续往后找真 Python。
「安装环境.bat」早就会用 find "WindowsApps" 过滤，启动器这边曾经漏了，本文件盯住它。
"""
import os
import pathlib
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).parent
LAUNCHER = BASE / "launcher.ps1"
INSTALLER = BASE.parent / "安装环境.bat"
PASS = FAIL = 0

PROBE = r'''
$ErrorActionPreference = "Stop"
$env:PATH = $env:BOM_TEST_PATH
. $env:BOM_LAUNCHER -Action help 6>$null
$py = Resolve-Python
"EXE=[" + $py.Exe + "]"
"WIN=[" + $py.Window + "]"
"STORE=[" + $script:StoreAliasSeen + "]"
'''


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def probe(path_value):
    """把 launcher.ps1 dot-source 进来，在指定 PATH 下跑一次 Resolve-Python。"""
    with tempfile.TemporaryDirectory() as td:
        script = pathlib.Path(td) / "probe.ps1"
        script.write_text(PROBE, encoding="utf-8")
        env = os.environ.copy()
        env["BOM_LAUNCHER"] = str(LAUNCHER)
        env["BOM_TEST_PATH"] = path_value
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or ""), (r.stdout or "")


def pick(out, tag):
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f"{tag}=["):
            return line[len(tag) + 2:-1]
    return None


launcher = LAUNCHER.read_text(encoding="utf-8")
installer = INSTALLER.read_text(encoding="utf-8")

print("== 启动器：静态检查 ==")
check("有应用商店别名判定函数", "function Test-StoreAlias" in launcher)
check("按 \\WindowsApps\\ 识别", r"'\\WindowsApps\\'" in launcher, "正则没写对")
check("收集候选前先过滤", "if (Test-StoreAlias $exe)" in launcher)
idx_filter = launcher.find("if (Test-StoreAlias $exe)")
idx_append = launcher.find("$cands += @{ Exe = $exe")
check("过滤在入列之前", 0 <= idx_filter < idx_append, (idx_filter, idx_append))
check("过滤时置标志", "$script:StoreAliasSeen = $true" in launcher)
check("取满 PATH 上所有命中（-All）", "Get-Command $name -All" in launcher)
check("报错分两种说法", "只有应用商店的占位链接" in launcher)
check("体检也分两种说法", "只有应用商店占位链接，需装官方版 3.10+" in launcher)

print("== 安装环境.bat：静态检查 ==")
check("where python 结果按 WindowsApps 过滤", 'find /i "WindowsApps"' in installer)
check("命中也记下来", 'set "STORE=1"' in installer)
check("提示区分两种情况", "只找到 Windows 应用商店的占位 Python" in installer)
check("仍然是 CRLF（批处理不能是 LF）",
      b"\r\n" in INSTALLER.read_bytes() and
      INSTALLER.read_bytes().count(b"\n") == INSTALLER.read_bytes().count(b"\r\n"))

print("== 启动器：隔离 PATH 实测 ==")
alias = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "python.exe"
sys_dirs = [r"C:\Windows\System32", r"C:\Windows\System32\WindowsPowerShell\v1.0", r"C:\Windows\System32\Wbem"]

if not alias.exists():
    print("  skip 本机没有应用商店占位符，跳过实测")
else:
    # 场景 1：PATH 里只有占位符，等于「这台机器没装过 Python」
    rc, out, _ = probe(";".join(sys_dirs + [str(alias.parent)]))
    check("脚本跑通", rc == 0, out[-500:])
    check("没把占位符当解释器", pick(out, "EXE") == "" and pick(out, "WIN") == "",
          [l for l in out.splitlines() if l.startswith(("EXE=", "WIN="))])
    check("识别出占位符", pick(out, "STORE") == "True",
          [l for l in out.splitlines() if l.startswith("STORE=")])

    # 场景 2：占位符排在真 Python 前面，仍要把真 Python 找出来
    real = pathlib.Path(sys.executable)
    rc, out, _ = probe(";".join([str(alias.parent), str(real.parent)] + sys_dirs))
    check("脚本跑通（占位符遮蔽场景）", rc == 0, out[-500:])
    check("越过占位符找到真 Python",
          pick(out, "EXE") and pathlib.Path(pick(out, "EXE")).samefile(real),
          f"拿到 {pick(out, 'EXE')!r}，期望 {str(real)!r}")
    check("同时标记出占位符", pick(out, "STORE") == "True",
          [l for l in out.splitlines() if l.startswith("STORE=")])

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
