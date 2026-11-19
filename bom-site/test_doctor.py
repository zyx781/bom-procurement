"""环境自检与打包清单不能各说各话。

现场：在别人的电脑上「启动淘宝会话」打不开淘宝，但我们拿不到那台机器的信息。
`检查电脑环境.bat` + `bom-site/doctor.ps1` 负责把原因逐项问出来；这个文件守住三件事：

1. 自检真的覆盖了「淘宝打不开」的几个常见原因（运行时、Edge、组策略、调试端口、网络）；
2. 报告落在 runs/ 里，用户找得到、发得出来；
3. 发布脚本与说明文档都记得带上它 —— 否则修好了自检，发出去的包里却没有。
"""
import pathlib
import re

BASE = pathlib.Path(__file__).parent
ROOT = BASE.parent
DOCTOR = BASE / "doctor.ps1"
CHECKER = ROOT / "检查电脑环境.bat"
LAUNCHER = BASE / "launcher.ps1"
DOC = ROOT / "使用说明.md"
MAKE_RELEASE = ROOT / "make_release.py"
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def skip(name):
    print(f"  skip {name}（发布包里没有这项）")


def read(p):
    return p.read_text(encoding="utf-8-sig")


doctor = read(DOCTOR)
checker = read(CHECKER)
launcher = read(LAUNCHER)
doc = read(DOC)
# 发布包里没有 make_release.py（打包脚本只留在开发仓库），这几条按需跳过
release = read(MAKE_RELEASE) if MAKE_RELEASE.is_file() else ""

print("== 自检脚本覆盖的失败原因 ==")
for needle, label in [
    ('"runtime\\python\\python.exe"', "包内运行时"),
    (".venv\\Scripts\\python.exe", "可能坏掉的 .venv"),
    ("RemoteDebuggingAllowed", "组策略是否禁远程调试"),
    ("--remote-debugging-port=", "调试端口实测"),
    ("/json/version", "CDP 端点探测"),
    ("www.taobao.com", "淘宝可达性"),
    ("ProxyEnable", "系统代理"),
    ("doctor-", "报告文件名"),
]:
    check(f"检查了{label}", needle in doctor, f"doctor.ps1 里找不到 {needle!r}")

print("== 只读与安全 ==")
check("试验用临时 profile（不碰登录态）", "bom-doctor-" in doctor and "user-data-dir=$testProfile" in doctor)
check("试验后按 PID 关掉（只关自己开的）", "taskkill /PID $proc.Id" in doctor)
check("报告写进 runs/", "Join-Path $AppDir \"runs\"" in doctor or "$RunsDir" in doctor)
check("结论区分失败/提醒", "script:Fails" in doctor and "script:Warns" in doctor)
check("能免交互运行（NoPause 开关）", "$NoPause" in doctor)

print("== 入口与编码 ==")
check("检查电脑环境.bat 存在", CHECKER.is_file())
check("bat 调 doctor.ps1", "bom-site\\doctor.ps1" in checker)
check("bat 是 CRLF", b"\r\n" in CHECKER.read_bytes())
check("doctor.ps1 带 BOM（PowerShell 5.1 读中文）", DOCTOR.read_bytes().startswith(b"\xef\xbb\xbf"))

print("== 启动器配合 ==")
check("启动器优先用包内运行时", 'Join-Path $RootDir "runtime\\python\\python.exe"' in launcher)
check("体检里认得出内置运行时", "runtime\\python，免安装" in launcher or "内置运行时" in launcher)
check("坏 .venv 有专门说法", "是从别的电脑拷过来的" in launcher)
check("解压到不可写目录会拦下", "这个目录不能写" in launcher)

print("== 文档与发布包 ==")
check("使用说明提到自检入口", "检查电脑环境.bat" in doc)
check("使用说明写了便携版", "-便携版" in doc)
if release:
    check("make_release 把自检打进源码包", "检查电脑环境.bat" in release)
    check("make_release 自检会查 doctor.ps1", "doctor.ps1" in release)
else:
    skip("make_release 清单")
if (ROOT / "tools" / "build_portable.py").is_file():
    check("便携版打包脚本存在", True)
else:
    skip("便携版打包脚本（发布包里没有 tools/）")

# 版本号三处一致：VERSION / README / 使用说明 里出现的 vX.Y.Z 至少匹配 VERSION
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
check("VERSION 是 x.y.z", re.fullmatch(r"\d+\.\d+\.\d+", version) is not None, version)
check("README 写着当前版本", version in read(ROOT / "README.md"), version)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
