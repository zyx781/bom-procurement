#!/usr/bin/env python3
"""组装「免安装便携版」：把 Python 运行时 + 依赖 + 代码打进一个文件夹。

背景：给别人的电脑用的那份，最大的坑是「环境」——对方没装 Python、装了旧版本、
或者直接把你本机的 .venv 一起拷过去（里面的 python.exe 记着原机器的绝对路径，
换台机器就起不来）。所以便携版把解释器和依赖一起带上，双击就能用，不联网。

原料缓存（.cache/，不进 git）：
    .cache/pyembed/python-3.14.4-embed-amd64.zip   python.org 官方嵌入式包
    .cache/wheelhouse/*.whl                        pip download 下来的依赖

用法：
    python tools/build_portable.py                # 组装 + 自检 + 打 zip
    python tools/build_portable.py --skip-verify  # 只组装
    python tools/build_portable.py --out D:/发布   # 换输出目录

产物：
    dist/BOM采购台-v<版本>-便携版/          解压即用的目录
    dist/BOM采购台-v<版本>-便携版.zip       发给别人的那个
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
PY_EMBED_ZIP = sorted(CACHE.glob("pyembed/python-*-embed-amd64.zip"))
WHEELHOUSE = CACHE / "wheelhouse"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
APP_NAME = f"BOM采购台-v{VERSION}-便携版"

sys.path.insert(0, str(ROOT))
import make_release  # noqa: E402  —— 复用它的「哪些文件该打包 / 不该打包」清单

# 便携版多带的东西：外层入口与自检工具
EXTRA_ROOT_FILES = ["检查电脑环境.bat"]


def log(msg: str) -> None:
    print(msg, flush=True)


def unpack_python(dest: Path) -> Path:
    """解压嵌入式 Python 到 dest，并让它认 Lib\\site-packages。"""
    if not PY_EMBED_ZIP:
        raise SystemExit(
            "找不到 Python 嵌入式包。先跑一次：\n"
            r"  powershell -ExecutionPolicy Bypass -File tools\fetch_runtime.ps1"
        )
    src = PY_EMBED_ZIP[-1]
    py_dir = dest / "runtime" / "python"
    if py_dir.exists():
        shutil.rmtree(py_dir)
    py_dir.mkdir(parents=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(py_dir)

    # 嵌入式包默认只把自身目录和 pythonXY.zip 放进 sys.path，缺 site-packages。
    # 官方文档要求自己往 ._pth 里加一行；这里连 `import site` 一起打开，
    # 否则包里的 .pth 文件（例如 greenlet 的）不会被处理。
    pth = next(py_dir.glob("python*._pth"), None)
    if pth is None:
        raise SystemExit("嵌入式包里没有 ._pth，版本不对？")
    text = pth.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#import site")]
    if "Lib\\site-packages" not in lines:
        lines.append("Lib\\site-packages")
    lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  运行时   {py_dir.relative_to(dest)}（{pth.name} 已加 site-packages）")
    return py_dir


def install_deps(py_dir: Path) -> None:
    """把 wheel 装进运行时，不用 pip 联网。"""
    wheels = sorted(WHEELHOUSE.glob("*.whl"))
    if not wheels:
        raise SystemExit(
            "wheelhouse 是空的。先跑一次：\n"
            r"  powershell -ExecutionPolicy Bypass -File tools\fetch_runtime.ps1"
        )
    target = py_dir / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-index", f"--find-links={WHEELHOUSE}",
        "--target", str(target),
        "--no-compile",
        "--disable-pip-version-check", "-q",
        *[str(w) for w in wheels],
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("依赖安装失败：\n" + (r.stderr or r.stdout)[-2000:])
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    log(f"  依赖     {len(wheels)} 个 wheel → {size / 1048576:.1f} MB")

    # 嵌入式 Python 带 ._pth 时是「隔离模式」：不把脚本所在目录塞进 sys.path，
    # 于是 `python server.py` 里 `import diag` 直接 ModuleNotFoundError。
    # 用 sitecustomize 把包里两个代码目录按「相对运行时自身」算出来，
    # 解压到任何路径（含中文/空格）都成立。
    (target / "sitecustomize.py").write_text(
        '"""便携版的 sys.path 补丁：把包内代码目录挂上去。\n\n'
        "嵌入式 Python 的 ._pth 会关掉「脚本目录自动入 path」，\n"
        "而本工具既跑 bom-site/server.py 也跑 Taobao-Search-Skill/scripts/taobao.py，\n"
        "所以按运行时自身位置推出包根目录，再补两条。\n"
        '"""\n'
        "import os\n"
        "import sys\n\n"
        "_pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))))\n"
        "for _sub in (\"bom-site\", os.path.join(\"Taobao-Search-Skill\", \"scripts\")):\n"
        "    _p = os.path.join(_pkg_root, _sub)\n"
        "    if os.path.isdir(_p) and _p not in sys.path:\n"
        "        sys.path.append(_p)\n",
        encoding="utf-8",
    )
    log("  补丁     sitecustomize.py → 自动挂上 bom-site 与 Taobao-Search-Skill/scripts")


def copy_source(dest: Path) -> int:
    """代码、说明、入口脚本；复用 make_release 的排除清单。"""
    items = make_release.collect()
    count = 0
    for src, rel in items:
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    for name in EXTRA_ROOT_FILES:
        p = ROOT / name
        if p.is_file():
            shutil.copy2(p, dest / name)
            count += 1
    log(f"  代码     {count} 个文件")
    return count


def smoke(py_dir: Path, app_root: Path) -> list[str]:
    """自检：解释器能跑、依赖齐全、服务能起、页面能开。"""
    problems: list[str] = []
    py = py_dir / "python.exe"

    r = subprocess.run(
        [str(py), "-c", "import sys, playwright, openpyxl; print(sys.version.split()[0])"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(app_root),
    )
    if r.returncode != 0:
        problems.append("运行时导入依赖失败：" + (r.stderr or r.stdout).strip()[-500:])
        return problems
    log(f"  自检     Python {r.stdout.strip()} · playwright/openpyxl 可导入")

    # 换到临时目录再起服务：模拟「别人电脑上换个路径解压」，
    # 顺便验证没有代码依赖打包机的绝对路径。
    tmp = Path(tempfile.mkdtemp(prefix="bom-portable-"))
    try:
        work = tmp / "带 空格 的目录" / app_root.name
        shutil.copytree(app_root, work)
        env = dict(os.environ)
        env["TAOBAO_RUNS_DIR"] = str(tmp / "_runs")
        env["PYTHONIOENCODING"] = "utf-8"
        port = make_release.free_port(8870)
        proc = subprocess.Popen(
            [str(work / "runtime" / "python" / "python.exe"), "server.py", "--port", str(port)],
            cwd=str(work / "bom-site"),
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            import time
            ok = False
            deadline = time.time() + 40
            while time.time() < deadline and proc.poll() is None:
                good, _ = make_release.http_ok(f"http://127.0.0.1:{port}/api/health")
                if good:
                    ok = True
                    break
                time.sleep(0.4)
            if not ok:
                tail = ""
                if proc.stdout:
                    try:
                        tail = proc.stdout.read()[-800:]
                    except Exception:  # noqa: BLE001
                        pass
                problems.append("换路径后服务起不来：" + (tail.strip() or "40 秒无响应"))
            else:
                good, body = make_release.http_ok(f"http://127.0.0.1:{port}/")
                if not good or "<html" not in body.lower():
                    problems.append(f"首页异常：{body[:120]}")
                else:
                    log("  自检     复制到带空格/中文的临时路径后，服务与首页正常")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def zip_dir(src: Path, out: Path) -> None:
    tmp = out.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, str(p.relative_to(src.parent)))
    tmp.replace(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个自带 Python 的免安装包")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    dest = out_dir / APP_NAME
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    log(f"打包 {APP_NAME}")
    py_dir = unpack_python(dest)
    install_deps(py_dir)
    copy_source(dest)

    (dest / "runtime" / "README.txt").write_text(
        "这个目录是包内自带的 Python 运行时（python.org 的 embeddable 包）"
        "加上 playwright / openpyxl 依赖。\n"
        "不要删、不要改名，也不用单独安装 Python。\n"
        "想换版本：删掉整个 runtime 目录，重跑 tools\\fetch_runtime.ps1 再打包。\n",
        encoding="utf-8",
    )

    if not args.skip_verify:
        log("自检 ...")
        problems = smoke(py_dir, dest)
        if problems:
            log("自检没通过：")
            for p in problems:
                log("  - " + p)
            return 3

    zip_path = out_dir / f"{APP_NAME}.zip"
    zip_dir(dest, zip_path)
    size = zip_path.stat().st_size / 1048576
    log(f"完成     {zip_path}（{size:.1f} MB）")
    log(f"目录     {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
