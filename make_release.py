#!/usr/bin/env python3
"""把仓库打成可以直接交给别人用的发布包。

用法：
    python make_release.py                # 打包 + 自检
    python make_release.py --no-verify    # 只打包，不验
    python make_release.py --out D:/发布   # 换输出目录

包内结构（顶层带版本号，解压不会散一地）：

    BOM采购台-v0.13.5/
      VERSION  README.md  使用说明.md  LICENSE
      requirements*.txt
      安装环境.bat  启动BOM选型台.bat  打开调试版Edge.bat  检查电脑环境.bat
      bom-site/...
      Taobao-Search-Skill/...

这是**源码版**（约 1.7 MB，对方要自己装 Python）。要「解压就能跑」的包用
tools/build_portable.py，它会把 Python 运行时和依赖一起带上（约 52 MB）。

**只打代码**。登录 profile、跑批记录、会话日志、截图、本机配置、真实 BOM
和测试报告全在排除清单里；打完还会扫一遍 zip 内的文件名，命中就报错。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
APP_NAME = f"BOM采购台-v{VERSION}"

# ---------------------------------------------------------------- 打包范围
# 根目录只放这些：入口、说明、依赖清单。其余（BOM xlsx、复测报告、改进建议、
# figures、_archive、.git）都属于本机工作产物，不进包。
ROOT_FILES = [
    "VERSION",
    "README.md",
    "使用说明.md",
    "LICENSE",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-optional.txt",
    "安装环境.bat",
    "启动BOM选型台.bat",
    "打开调试版Edge.bat",
    "检查电脑环境.bat",
]
ROOT_DIRS = ["bom-site", "Taobao-Search-Skill"]

# 目录名命中就整棵剪掉
SKIP_DIR_NAMES = {
    "__pycache__",
    ".cache",          # Taobao-Search-Skill/scripts/.cache：profile + 缓存，含 cookie
    "runs",            # 分析结果、跑批进度、日志、截图、真实订单
    ".cdp-profile",    # 调试版 Edge 的登录态
    ".browser-profile",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".git",
    "node_modules",
}
# 相对路径前缀（用 / 分隔）命中就跳过
SKIP_REL_PREFIX = {
    "bom-site/tests/artifacts",
}
SKIP_FILE_NAMES = {
    "settings.json",      # 本机调参，只发 settings.example.json
    "taobao-session.json",
    "Thumbs.db",
    ".DS_Store",
}
SKIP_FILE_PATTERNS = [
    "*.pyc", "*.pyo", "*.pyd",
    "*.log",
    "*.bak", "*.bak-*",
    "*.cookie", "*.cookies",
    "*.tmp", "*.swp",
    "_tmp*",
    "_b.json", "_batch.json", "_cart.json",
    "_probe*",
]
# 包里出现这些文件名/路径就判失败（登录态与真实数据）
FORBIDDEN = re.compile(
    r"(^|/)(\.cdp-profile|\.browser-profile|\.cache|runs)(/|$)"
    r"|(^|/)settings\.json$"
    r"|(^|/)taobao-session\.json$"
    r"|cookie",
    re.IGNORECASE,
)


def _skip_file(name: str) -> bool:
    if name in SKIP_FILE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILE_PATTERNS)


def collect() -> list[tuple[Path, str]]:
    """返回 [(源文件, 包内相对路径)]，路径用 / 分隔。"""
    items: list[tuple[Path, str]] = []

    def walk(base: Path, rel: str) -> None:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            sub = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                if sub in SKIP_REL_PREFIX or any(sub.startswith(p + "/") for p in SKIP_REL_PREFIX):
                    continue
                walk(entry, sub)
            elif entry.is_file():
                if _skip_file(entry.name):
                    continue
                if sub in SKIP_REL_PREFIX:
                    continue
                items.append((entry, sub))

    missing = []
    for name in ROOT_FILES:
        p = ROOT / name
        if not p.is_file():
            missing.append(name)
            continue
        items.append((p, name))
    for name in ROOT_DIRS:
        p = ROOT / name
        if not p.is_dir():
            missing.append(name + "/")
            continue
        walk(p, name)
    if missing:
        raise SystemExit("缺少必要文件，先补齐再打包：" + "、".join(missing))
    return items


def build(out_dir: Path, items: list[tuple[Path, str]]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{APP_NAME}.zip"
    tmp = out.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, rel in items:
            z.write(src, f"{APP_NAME}/{rel}")
    tmp.replace(out)
    return out


def scan(zip_path: Path) -> list[str]:
    """扫包内文件名：登录态、真实数据、本机配置一个都不许有。"""
    bad = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            inner = name.split("/", 1)[1] if "/" in name else name
            if FORBIDDEN.search(inner):
                bad.append(name)
            if inner.lower().endswith((".docx", ".xlsx", ".xls")):
                # 测试夹具里的示例 BOM 是代码的一部分（公开开发板清单）；
                # 根目录那些才是本机真实清单和复测报告，一个都不许进包。
                if not inner.startswith("bom-site/tests/fixtures/"):
                    bad.append(name + "（文档/真实 BOM 不该进包）")
    return bad


def free_port(start: int = 8795) -> int:
    for port in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def http_ok(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(4096)
            return resp.status == 200, body.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def verify(zip_path: Path) -> list[str]:
    """解压到临时目录，编译全部 .py、起一次服务、请求首页与 health。"""
    problems: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bom-release-"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        base = tmp / APP_NAME
        app = base / "bom-site"
        for need in ("server.py", "engine.py", "static/index.html", "launcher.ps1",
                     "doctor.ps1", "settings.example.json"):
            if not (app / need).exists():
                problems.append(f"解压后缺文件：bom-site/{need}")
        if not (base / ".venv").exists():
            pass  # 正常：.venv 由安装脚本现建
        for need in ("安装环境.bat", "检查电脑环境.bat"):
            if not (base / need).exists():
                problems.append(f"解压后缺 {need}")

        # 1) 语法：包里所有 .py 都要能编译
        pys = sorted(base.rglob("*.py"))
        for p in pys:
            r = subprocess.run(
                [sys.executable, "-m", "py_compile", str(p)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode != 0:
                problems.append(f"语法错误 {p.relative_to(base)}: {(r.stderr or '').strip()[:200]}")
                break

        # 2) 启动器语法（PowerShell 解析器）
        ps = base / "bom-site" / "launcher.ps1"
        if ps.exists() and shutil.which("powershell"):
            script = (
                "$e=$null;"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps}',[ref]$null,[ref]$e);"
                "if($e.Count){$e|%{$_.Message};exit 1}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode != 0:
                problems.append("launcher.ps1 解析失败：" + (r.stdout or r.stderr).strip()[:300])

        # 3) 起服务（独立 runs 目录，绝不碰真实数据）
        port = free_port()
        env = dict(os.environ)
        env["TAOBAO_RUNS_DIR"] = str(tmp / "_verify_runs")
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(port)],
            cwd=str(app),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.time() + 40
            healthy = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                ok, _ = http_ok(f"http://127.0.0.1:{port}/api/health")
                if ok:
                    healthy = True
                    break
                time.sleep(0.4)
            if not healthy:
                tail = ""
                if proc.stdout:
                    try:
                        tail = proc.stdout.read()[-800:]
                    except Exception:  # noqa: BLE001
                        pass
                problems.append("解压后服务起不来：" + (tail.strip() or "40 秒内没有健康响应"))

            ok, body = http_ok(f"http://127.0.0.1:{port}/")
            if not ok:
                problems.append(f"首页打不开：{body[:160]}")
            elif "app.js" not in body and "<!DOCTYPE" not in body.upper():
                problems.append("首页内容异常（没有 app.js / DOCTYPE）")

            ok, body = http_ok(f"http://127.0.0.1:{port}/api/last-analysis")
            if not ok:
                problems.append(f"/api/last-analysis 异常：{body[:160]}")
            elif '"result"' in body and '"lines"' in body:
                problems.append("新包里居然带了上次分析结果")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个可以直接交给别人用的发布包")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 ./dist）")
    ap.add_argument("--no-verify", action="store_true", help="跳过解压自检")
    args = ap.parse_args()

    items = collect()
    out = build(Path(args.out), items)

    src_bytes = sum(p.stat().st_size for p, _ in items)
    zip_bytes = out.stat().st_size
    print(f"版本      {VERSION}")
    print(f"文件      {len(items)} 个")
    print(f"原始      {src_bytes / 1048576:.2f} MB")
    print(f"压缩包    {zip_bytes / 1048576:.2f} MB  →  {out}")

    bad = scan(out)
    if bad:
        print("\n包里混进了不该带的东西：")
        for name in bad[:20]:
            print("  -", name)
        out.unlink(missing_ok=True)
        return 2

    if args.no_verify:
        print("已跳过自检（--no-verify）")
        return 0

    print("\n自检：解压 → 编译 → 起服务 → 请求页面 ...")
    problems = verify(out)
    if problems:
        print("自检没通过：")
        for p in problems:
            print("  -", p)
        return 3
    print("自检通过：解压即可运行，首页与 /api/health 正常，包内无登录态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
