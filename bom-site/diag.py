#!/usr/bin/env python3
"""运行日志与诊断包：出问题时，用户点一次就能把「能定位的东西」交出来。

为什么需要它：软件发到别人电脑上之后，「卡住了」通常只剩一句口头描述。
真正的线索分散在四个地方，用户既不知道该找哪个，也不该被要求去找：

    runs/server.out.log       服务启动、路由、异常堆栈
    runs/server.err.log       未捕获异常
    runs/session-*.log        淘宝会话全过程（连浏览器、判登录、跑批）
    runs/diagnostics-*.jsonl  每行「搜到几条、选中哪个、卡在哪」

设计约束：

  - **只读**。不改 runs/、不碰 profile、不重启服务；用户来这页时别处
    很可能已经坏了，不能再添乱。
  - **白名单**。只打包上面几类日志 + 环境摘要。cookie 快照、订单、收货
    地址、真实 BOM 原文件一律不进包；BOM 明细要用户显式勾选，且只取
    白名单字段（行号、型号、状态、失败原因）。
  - **脱敏**。所有文本先过 redact()：cookie 值、token、手机号、邮箱、
    长随机串一律打码。宁可多打，不能漏。
  - 任何异常都吞掉并降级 —— 这页本身不能成为新的故障点。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import platform
import re
import shutil
import socket
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
VERSION_FILE = ROOT / "VERSION"

# 单个文件最多取这么多行 / 字节：日志可能被死循环刷到几百 MB，
# 全塞进包会把用户邮箱撑爆，也没人看得完。
MAX_LOG_LINES = 4000
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_SESSION_FILES = 3
MAX_DIAG_FILES = 3
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_CLIENT_LINES = 500
MAX_NOTE_CHARS = 1200


def runs_dir() -> Path:
    """runs 跟着环境变量走：测试用 TAOBAO_RUNS_DIR 隔离时自动落到临时目录。"""
    path = Path(os.environ.get("TAOBAO_RUNS_DIR") or (BASE / "runs")).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def app_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


# ============================================================
# 脱敏
# ============================================================
# 顺序有讲究：先整行砍头（Cookie: a=b; c=d），再逐键打码，最后才处理
# 手机号/邮箱/长串。反过来的话，长串规则会先把 cookie 值换成占位符，
# 整行砍头就认不出这是 Cookie 头了 —— 结果反而更难判断问题。
_COOKIE_HEADER = re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n]+")
_PAIR = re.compile(
    r"(?i)\b(unb|cookie2|cookie1|_tb_token_|sgcookie|_cc_|cna|token|access_token|"
    r"refresh_token|password|passwd|pwd|secret|authorization|sessionid|session_id|"
    r"sid|ticket)\s*=\s*([^\s,;&\"'<>]+)"
)
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{6}(\d{2})(?!\d)")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_IDCARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_LONG_RANDOM = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-])")


def _mask_random(match: re.Match[str]) -> str:
    """长串打码，但放过分隔线。

    异常堆栈里到处是 `----------------------------------------`。
    它符合「40 位以上 ASCII」的特征，可它不是凭据；打码之后人就看不出
    哪里是新的异常段了，日志也就没法读了。
    """
    text = match.group(0)
    if len(set(text)) <= 2 or not any(ch.isalnum() for ch in text):
        return text
    return f"<{len(text)}位随机串已省略>"


def redact(text: str) -> str:
    """把疑似凭据与个人信息打码；打码后的文本仍能看懂报错在说什么。"""
    if not text:
        return ""
    out = _COOKIE_HEADER.sub(r"\1: ***", text)
    out = _PAIR.sub(r"\1=***", out)
    out = _PHONE.sub(r"\1******\2", out)
    out = _EMAIL.sub(r"\1***@\2", out)
    out = _IDCARD.sub(r"\1********\2", out)
    out = _LONG_RANDOM.sub(_mask_random, out)
    return out


# ============================================================
# 读日志
# ============================================================
def tail_text(path: Path, max_lines: int = MAX_LOG_LINES, max_bytes: int = MAX_LOG_BYTES) -> str:
    """从文件尾部读一段。绝不整文件读入 —— 日志可能比内存大。"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                raw = fh.read()
                cut = raw.find(b"\n")
                raw = raw[cut + 1 :] if cut >= 0 else b""
            else:
                raw = fh.read()
    except OSError:
        return ""
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return "?"


# ============================================================
# 环境摘要
# ============================================================
def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001  没装、元数据坏了都只是「查不到版本」
        return None


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001  某些包的 find_spec 会抛（父包不存在等）
        return False


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _edge_candidates() -> list[Path]:
    out: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        out.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        out.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return out


def _browser_info() -> dict[str, Any]:
    found = [str(p) for p in _edge_candidates() if p.is_file()]
    cdp_port = 9222
    try:
        cdp_port = int(os.environ.get("TAOBAO_CDP_PORT") or 9222)
    except ValueError:
        pass
    return {
        "found": found,
        "cdp_port": cdp_port,
        "cdp_listening": _port_open(cdp_port),
    }


def _deps_info() -> dict[str, Any]:
    """只探测「装没装」，不 import：import playwright 要几百毫秒，且可能拉子进程。"""
    # 同一个模块可能由不同发行包提供（opencv 三选一），版本名要挨个试
    items: dict[str, tuple[str, tuple[str, ...]]] = {
        "playwright": ("playwright", ("playwright",)),
        "openpyxl": ("openpyxl", ("openpyxl",)),
        "ddddocr": ("ddddocr", ("ddddocr",)),
        "opencv": ("cv2", ("opencv-python-headless", "opencv-python", "opencv-contrib-python")),
    }
    out: dict[str, Any] = {}
    for label, (module, dists) in items.items():
        present = _has_module(module)
        version = None
        if present:
            for dist in dists:
                version = _pkg_version(dist)
                if version:
                    break
        out[label] = {"installed": present, "version": version}
    return out


def _session_info() -> dict[str, Any]:
    """会话状态摘要。延迟 import：诊断页不该把服务启动流程拖慢。"""
    try:
        import taobao_runner as tr

        st = tr.RUNNER.status() or {}
    except Exception as exc:  # noqa: BLE001  服务刚起来/模块坏了都当查不到
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "running": bool(st.get("running")),
        "ready": bool(st.get("ready")),
        "waiting_login": bool(st.get("waiting_login")),
        "login_state": st.get("login_state"),
        "verification": bool(st.get("verification")),
        "stalled": bool(st.get("stalled")),
        "browser_gone": bool(st.get("browser_gone")),
        "desynced": bool(st.get("desynced")),
        "pid": st.get("pid"),
        "started_at": st.get("started_at"),
        "last_error": redact(str(st.get("last_error") or ""))[:400],
    }


def _history_info() -> dict[str, Any]:
    """淘宝历史清理状态。用户问「历史里怎么还有」时，这行就是答案。"""
    try:
        import history_cleaner

        last = history_cleaner.last_result()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        import taobao_runner as tr

        enabled: bool | None = bool(tr.history_cleanup_enabled())
    except Exception:  # noqa: BLE001
        enabled = None
    return {"enabled": enabled, "last": last}


def _runs_info() -> dict[str, Any]:
    rd = runs_dir()
    files = [p for p in rd.glob("*") if p.is_file()]
    logs = [p for p in files if p.suffix.lower() in (".log",)]
    return {
        "path": str(rd),
        "files": len(files),
        "size_mb": round(sum(_size(p) for p in files) / 1048576, 2),
        "log_files": len(logs),
        "log_size_mb": round(sum(_size(p) for p in logs) / 1048576, 2),
    }


def _disk_info() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(ROOT))
        return {
            "free_mb": round(usage.free / 1048576),
            "total_mb": round(usage.total / 1048576),
        }
    except OSError:
        return {}


def log_files() -> list[dict[str, Any]]:
    """可预览/可打包的日志清单。名字是白名单键，不接受任意路径。"""
    rd = runs_dir()
    out: list[dict[str, Any]] = []
    for key, path in (
        ("server.out", rd / "server.out.log"),
        ("server.err", rd / "server.err.log"),
        ("client", rd / "client.log"),
    ):
        if path.is_file():
            out.append(
                {
                    "key": key,
                    "name": path.name,
                    "size": _size(path),
                    "mtime": _fmt_time(_mtime(path)),
                }
            )
    sessions = sorted(rd.glob("session-*.log"), key=_mtime, reverse=True)[:MAX_SESSION_FILES]
    for path in sessions:
        out.append(
            {
                "key": path.stem,
                "name": path.name,
                "size": _size(path),
                "mtime": _fmt_time(_mtime(path)),
            }
        )
    return out


def resolve_log(name: str) -> Path | None:
    """把前端传来的 key 映射到 runs 下的真实文件；只认清单里的。"""
    if not name:
        return None
    rd = runs_dir()
    for item in log_files():
        if name in (item["key"], item["name"]):
            return rd / str(item["name"])
    return None


def summary(include_logs: bool = True) -> dict[str, Any]:
    """给页面用的环境摘要：全部可公开给用户自己看，不含任何凭据。"""
    data: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": app_version(),
        "engine_version": _engine_version(),
        "python": f"{platform.python_version()} ({platform.architecture()[0]})",
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "os": platform.platform(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "deps": _deps_info(),
        "browser": _browser_info(),
        "session": _session_info(),
        "history": _history_info(),
        "runs": _runs_info(),
        "disk": _disk_info(),
        "logs": log_files(),
    }
    if include_logs:
        data["client_errors"] = _client_error_count()
    return data


def _engine_version() -> str:
    try:
        import engine

        return str(getattr(engine, "ENGINE_VERSION", "?"))
    except Exception:  # noqa: BLE001
        return "?"


def _client_error_count() -> int:
    path = runs_dir() / "client.log"
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


# ============================================================
# 前端错误落盘
# ============================================================
_CLIENT_LOCK = threading.Lock()


def append_client_log(entry: dict[str, Any]) -> None:
    """记一条浏览器端错误。限行数，写失败一律吞掉。

    这条链路的价值：页面 JS 崩了只在用户屏幕上闪一下，服务端一无所知。
    用户下次说「点了没反应」时，client.log 里可能已经有答案。
    """
    try:
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kind": str(entry.get("kind") or "error")[:40],
            "message": redact(str(entry.get("message") or ""))[:800],
            "page": str(entry.get("page") or "")[:200],
            "extra": redact(str(entry.get("extra") or ""))[:400],
            "ua": str(entry.get("ua") or "")[:300],
        }
        line = json.dumps(record, ensure_ascii=False)
        path = runs_dir() / "client.log"
        with _CLIENT_LOCK:
            if _client_error_count() > MAX_CLIENT_LINES:
                kept = tail_text(path, max_lines=MAX_CLIENT_LINES // 2)
                path.write_text(kept + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:  # noqa: BLE001  诊断不能反过来影响主流程
        pass


# ============================================================
# BOM 明细（需用户勾选）
# ============================================================
_BOM_LINE_FIELDS = (
    "line_no",
    "mpn",
    "desc",
    "package",
    "qty",
    "refs",
    "category",
    "category_label",
    "design_value",
    "value_conflict",
    "status",
    "missing",
    "blocking",
    "hard_gates",
    "risks",
)


def bom_lines() -> dict[str, Any]:
    """只取排错必需的字段：行号、型号、状态、失败原因。

    刻意不取 row（原始表格）、query_plan（搜索词）、decision（含链接与价格）：
    那些对定位规则问题没帮助，却会带出用户的采购内容。
    """
    path = runs_dir() / "last-analysis.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "lines": []}
    result = (data or {}).get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return {"available": False, "lines": []}
    raw_lines = result.get("lines")
    if not isinstance(raw_lines, list):
        return {"available": False, "lines": []}
    lines: list[dict[str, Any]] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key in _BOM_LINE_FIELDS:
            if key in item:
                value = item[key]
                row[key] = redact(value) if isinstance(value, str) else value
        lines.append(row)
    return {
        "available": True,
        "saved_at": (data or {}).get("saved_at") if isinstance(data, dict) else None,
        "engine_version": result.get("engine_version"),
        "summary": result.get("summary"),
        "count": len(lines),
        "lines": lines,
    }


# ============================================================
# 打包
# ============================================================
_README = """BOM 采购台 · 运行日志包
========================================

生成时间：{generated_at}
应用版本：{app_version}
引擎版本：{engine_version}
Python：{python}
操作系统：{os}

用户描述
----------------------------------------
{note}

包里有什么
----------------------------------------
README.txt                 本文件
environment.txt            环境摘要（人看的）
environment.json           环境摘要（程序读的）
logs/server.out.log        服务启动与运行输出
logs/server.err.log        未捕获异常与堆栈
logs/session-*.log         淘宝会话全过程（连浏览器、判登录、跑批）
logs/client.log            浏览器端错误（页面脚本崩了才有）
diagnostics/*.jsonl        每行执行的结构化信号：搜到几条、卡在哪
state/session.json         导出时的会话状态
state/bom-lines.json       出问题的行（仅当用户勾选「附带 BOM 明细」）

怎么用
----------------------------------------
解压后先看 environment.txt 末尾的「会话状态」，再看 logs/server.err.log。
session 日志按时间顺序记录：有没有连上浏览器、登录判定结果、
每一行搜到几条、选中哪个 SKU。多数问题看这三处就能定位。

隐私
----------------------------------------
导出时已自动打码：cookie 值、token、密码、手机号、邮箱、身份证号、
40 位以上的随机串。包里不含：浏览器 profile、登录 cookie 快照、
收货地址、订单号、支付信息。
"""


def _add_text(zf: zipfile.ZipFile, name: str, text: str) -> int:
    """写入并返回字节数；超限就截断，保证包不会失控。"""
    data = text.encode("utf-8", "replace")
    if len(data) > MAX_BUNDLE_BYTES:
        data = data[:MAX_BUNDLE_BYTES] + "\n…（过大已截断）\n".encode()
    zf.writestr(name, data)
    return len(data)


def _render_env_text(env: dict[str, Any]) -> str:
    lines: list[str] = ["BOM 采购台 · 环境摘要", "=" * 40, ""]
    lines.append(f"生成时间   {env.get('generated_at')}")
    lines.append(f"应用版本   {env.get('app_version')}")
    lines.append(f"引擎版本   {env.get('engine_version')}")
    lines.append(f"Python     {env.get('python')}")
    lines.append(f"解释器     {env.get('executable')}")
    lines.append(f"操作系统   {env.get('os')}")
    lines.append(f"进程 PID   {env.get('pid')}")
    lines.append(f"工作目录   {env.get('cwd')}")
    lines.append("")
    lines.append("依赖")
    lines.append("-" * 40)
    for name, info in (env.get("deps") or {}).items():
        state = "已安装" if info.get("installed") else "未安装"
        ver = f" {info.get('version')}" if info.get("version") else ""
        lines.append(f"  {name:<12} {state}{ver}")
    lines.append("")
    lines.append("浏览器")
    lines.append("-" * 40)
    browser = env.get("browser") or {}
    found = browser.get("found") or []
    lines.append(f"  可执行文件   {found[0] if found else '未找到（Edge / Chrome）'}")
    for extra in found[1:]:
        lines.append(f"               {extra}")
    lines.append(f"  CDP 端口     {browser.get('cdp_port')} "
                 f"{'可连接' if browser.get('cdp_listening') else '未监听'}")
    lines.append("")
    lines.append("目录")
    lines.append("-" * 40)
    runs = env.get("runs") or {}
    lines.append(f"  runs 路径    {runs.get('path')}")
    lines.append(f"  runs 体积    {runs.get('size_mb')} MB（{runs.get('files')} 个文件）")
    disk = env.get("disk") or {}
    if disk:
        lines.append(f"  磁盘剩余     {disk.get('free_mb')} MB / {disk.get('total_mb')} MB")
    lines.append("")
    lines.append("会话状态")
    lines.append("-" * 40)
    sess = env.get("session") or {}
    if sess.get("error"):
        lines.append(f"  读取失败：{sess['error']}")
    else:
        for key, label in (
            ("running", "进程运行中"),
            ("ready", "已就绪"),
            ("waiting_login", "等待登录"),
            ("stalled", "启动卡住"),
            ("verification", "等待验证"),
            ("browser_gone", "浏览器已关闭"),
            ("desynced", "状态失步"),
        ):
            lines.append(f"  {label:<10} {'是' if sess.get(key) else '否'}")
        lines.append(f"  登录状态   {sess.get('login_state') or '未知'}")
        lines.append(f"  启动时间   {sess.get('started_at') or '-'}")
        if sess.get("last_error"):
            lines.append(f"  最近错误   {sess['last_error']}")
    lines.append("")
    lines.append("日志文件")
    lines.append("-" * 40)
    logs = env.get("logs") or []
    if not logs:
        lines.append("  （runs 下还没有日志）")
    for item in logs:
        size_kb = (item.get("size") or 0) / 1024
        lines.append(f"  {item.get('name'):<34} {size_kb:8.1f} KB  {item.get('mtime')}")
    lines.append("")
    return "\n".join(lines)


def build_bundle(note: str = "", include_bom: bool = True) -> tuple[str, bytes]:
    """生成诊断包，返回 (文件名, 字节)。"""
    rd = runs_dir()
    env = summary()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"BOM采购台-日志-{stamp}.zip"

    clean_note = redact(str(note or "").strip())[:MAX_NOTE_CHARS] or "（用户没有填写）"

    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        total += _add_text(
            zf,
            "README.txt",
            _README.format(
                generated_at=env.get("generated_at"),
                app_version=env.get("app_version"),
                engine_version=env.get("engine_version"),
                python=env.get("python"),
                os=env.get("os"),
                note=clean_note,
            ),
        )
        total += _add_text(zf, "environment.txt", _render_env_text(env))
        total += _add_text(zf, "environment.json", json.dumps(env, ensure_ascii=False, indent=1))

        # 服务日志
        for key, target in (("server.out", "logs/server.out.log"), ("server.err", "logs/server.err.log")):
            path = resolve_log(key)
            if path:
                total += _add_text(zf, target, redact(tail_text(path)))

        # 会话日志（最近 3 个）
        for item in log_files():
            if not item["key"].startswith("session-"):
                continue
            path = rd / item["name"]
            total += _add_text(zf, f"logs/{item['name']}", redact(tail_text(path, max_lines=3000)))

        # 浏览器端错误
        client = resolve_log("client")
        if client:
            total += _add_text(zf, "logs/client.log", redact(tail_text(client, max_lines=800)))

        # 诊断流水：保留原始 JSON 结构，只对字符串值脱敏
        diag_files = sorted(rd.glob("diagnostics-*.jsonl"), key=_mtime, reverse=True)[:MAX_DIAG_FILES]
        for path in diag_files:
            raw = tail_text(path, max_lines=2500)
            cleaned = "\n".join(_redact_json_line(line) for line in raw.splitlines() if line.strip())
            total += _add_text(zf, f"diagnostics/{path.name}", cleaned + ("\n" if cleaned else ""))

        # 会话状态（去掉冗长 logs，只留尾部）
        total += _add_text(zf, "state/session.json", json.dumps(_session_export(), ensure_ascii=False, indent=1))

        if include_bom:
            bom = bom_lines()
            total += _add_text(zf, "state/bom-lines.json", json.dumps(bom, ensure_ascii=False, indent=1))

        total += _add_text(
            zf,
            "打包说明.txt",
            f"共 {len(zf.namelist()) + 1} 个文件，未压缩约 {total / 1024:.0f} KB。\n"
            f"已自动脱敏；如仍发现敏感内容，请先告知维护者，不要外传。\n",
        )

    return filename, buf.getvalue()


def _redact_json_line(line: str) -> str:
    """诊断流水是 JSONL：逐行解析，只脱敏字符串值，结构原样保留。"""
    try:
        obj = json.loads(line)
    except ValueError:
        return redact(line)
    return json.dumps(_redact_obj(obj), ensure_ascii=False)


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


def _session_export() -> dict[str, Any]:
    try:
        import taobao_runner as tr

        st = dict(tr.RUNNER.status() or {})
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    logs = st.get("logs")
    if isinstance(logs, list):
        st["logs"] = [redact(str(x)) for x in logs][-80:]
    for key in ("last_error",):
        if isinstance(st.get(key), str):
            st[key] = redact(st[key])
    return st


def bundle_size_hint() -> int:
    """粗估包大小，页面用来提示（不真的打包）。"""
    total = 256 * 1024
    for item in log_files():
        total += min(int(item.get("size") or 0), MAX_LOG_BYTES)
    rd = runs_dir()
    for path in sorted(rd.glob("diagnostics-*.jsonl"), key=_mtime, reverse=True)[:MAX_DIAG_FILES]:
        total += min(_size(path), MAX_LOG_BYTES)
    return total


def now_stamp() -> str:
    """给测试与页面共用的时间戳。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")
