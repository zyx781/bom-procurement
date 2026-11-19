"""没装 Python 的机器上，启动器不能说错话；便携版则必须用自带运行时。

现场：Windows 10/11 自带一个 0 字节的假 python.exe
（%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe，ReparsePoint）。
Get-Command 能找到它，Test-Path 也返回真，但执行它只会打开 Microsoft Store。

踩过的坑一：启动器把它当成真 Python 收进候选，于是
  · 版本探测失败 → 显示「已找到，版本未知」
  · 依赖探测失败 → 报「Python 缺依赖（playwright / openpyxl）」
  · 用户照着提示去装依赖，装完还是起不来，中途还莫名弹出应用商店
踩过的坑二：Get-Command 默认只返回第一个命中。用户级 PATH 里 WindowsApps
常常排在真 Python 前面，于是「明明装了 Python，启动器说没装」。
踩过的坑三：从别的电脑连 .venv 一起拷过来。venv 里记着原机器的 Python 路径，
换台机器 python.exe 一执行就退出；启动器还优先用它，于是「装好依赖也起不来」。

正确行为：
  · 认出占位符并跳过，但继续往后找真 Python；
  · .venv 先验证能不能跑，跑不起来就跳过并记下来（报错时点名）；
  · 便携版（root/runtime/python）永远优先，不受机器上装了什么影响。

测试用 launcher.ps1 的临时副本跑，这样「有没有 runtime 目录」由本文件说了算，
不会因为开发机上刚打过便携包就变出一套不同的行为。
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
"BROKEN=[" + $script:BrokenVenv + "]"
'''


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def probe(path_value, with_runtime=False, with_broken_venv=False):
    """在临时目录里放一份 launcher.ps1，在指定 PATH 下跑一次 Resolve-Python。"""
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "bom-site").mkdir()
        launcher = root / "bom-site" / "launcher.ps1"
        launcher.write_bytes(LAUNCHER.read_bytes())
        if with_runtime:
            py_dir = root / "runtime" / "python"
            py_dir.mkdir(parents=True)
            (py_dir / "python.exe").write_bytes(b"")
            (py_dir / "pythonw.exe").write_bytes(b"")
        if with_broken_venv:
            scripts = root / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            # 一个必然起不来的「解释器」：内容不是 PE，一执行就报错
            (scripts / "python.exe").write_text("not a real python", encoding="utf-8")

        script = root / "probe.ps1"
        script.write_text(PROBE, encoding="utf-8")
        env = os.environ.copy()
        env["BOM_LAUNCHER"] = str(launcher)
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


launcher = LAUNCHER.read_text(encoding="utf-8-sig")
installer = INSTALLER.read_text(encoding="utf-8-sig")

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
check("内置运行时优先", 'Join-Path $RootDir "runtime\\python\\python.exe"' in launcher)
check("venv 先验证能不能跑", "if (Test-PythonRuns $venvExe)" in launcher)
check("坏 venv 记下来", "$script:BrokenVenv = $venvExe" in launcher)
check("坏 venv 有自己的报错", "是从别的电脑拷过来的" in launcher)

print("== 安装环境.bat：静态检查 ==")
check("where python 结果按 WindowsApps 过滤", 'find /i "WindowsApps"' in installer)
check("命中也记下来", 'set "STORE=1"' in installer)
check("提示区分两种情况", "只找到 Windows 应用商店的占位 Python" in installer)
check("内置运行时时直接跳过安装", '%~dp0runtime\\python\\python.exe' in installer)
check("拷来的 .venv 会重建", 'rd /s /q "%VENV%"' in installer)
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

    # 场景 3：便携版自带运行时，即使 PATH 里有真 Python 也必须用它
    rc, out, _ = probe(";".join([str(real.parent)] + sys_dirs), with_runtime=True)
    check("便携版：脚本跑通", rc == 0, out[-500:])
    exe = pick(out, "EXE") or ""
    check("便携版：用包内运行时，不用系统 Python",
          exe.replace("/", "\\").endswith(r"runtime\python\python.exe"),
          f"拿到 {exe!r}")

    # 场景 4：拷来的 .venv 起不来 → 跳过它继续找系统 Python，并记下来
    rc, out, _ = probe(";".join([str(real.parent)] + sys_dirs), with_broken_venv=True)
    check("坏 venv：脚本跑通", rc == 0, out[-500:])
    check("坏 venv：没有把它当解释器",
          (pick(out, "EXE") or "") and pathlib.Path(pick(out, "EXE")).samefile(real),
          f"拿到 {pick(out, 'EXE')!r}，期望 {str(real)!r}")
    check("坏 venv：记下来供报错使用", (pick(out, "BROKEN") or "").endswith("python.exe"),
          [l for l in out.splitlines() if l.startswith("BROKEN=")])

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
