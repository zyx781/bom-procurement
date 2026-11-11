#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""淘宝执行层：管理 Taobao-Search-Skill v4 的常驻 session 子进程。

协议：stdin 逐行 JSON，stdout 返回 JSON 信封（缩进格式，因此按 JSON 流解析）。
不做登录、不碰验证码、不结算付款；只执行 search / open / select-sku / cart-add / cart-view。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

import engine

try:  # 窗口/焦点控制只对本地 Edge 有意义，缺依赖时降级为空实现
    import focus_guard as _focus_guard
    import window_pos as _window_pos
except Exception:  # noqa: BLE001  pragma: no cover
    _focus_guard = None
    _window_pos = None

BASE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("TAOBAO_SKILL_REPO") or (BASE.parent / "Taobao-Search-Skill")).resolve()
ARTIFACTS = (REPO / "scripts" / ".cache" / "taobao-search-skill" / "artifacts").resolve()
# 测试/演练可通过 TAOBAO_RUNS_DIR 把批量进度隔离到临时目录，
# 否则集成测试会把用户真实的 batch-progress.json 覆盖成 1 行假数据。
RUNS = Path(os.environ.get("TAOBAO_RUNS_DIR") or (BASE / "runs")).resolve()
RUNS.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = BASE / "settings.json"


PROGRESS_FILE = RUNS / "batch-progress.json"


def _log(msg: str) -> None:
    """统一日志出口。批量跑是父进程读子进程输出，必须 flush，否则日志会攒在缓冲里。"""
    print(msg, flush=True)


def verification_next_step(added: bool, auto_retries: int, human_retries: int) -> str:
    """撞验证之后干什么（2026-11-08 规则）—— 纯函数，便于单测。

    "auto"  : 执行层会先刷新自恢复，这里立刻重试同一行，不惊动人；
    "human" : 自动重试用完（= 刷新后仍然出不来商品），等人工验证；
    "stop"  : 人工也没解开 / 已经点过加购（不能重跑，否则重复下单），停下。

    `added=True` 一律 stop：购物车可能已经进了，重跑会重复下单。
    """
    if added:
        return "stop"
    if auto_retries < VERIFY_AUTO_RETRY:
        return "auto"
    if human_retries < VERIFY_HUMAN_RETRY:
        return "human"
    return "stop"


def load_batch_progress() -> dict:
    """上一次批量的进度，按 line_no 记 —— 用于「暂停后下次接着跑」。"""
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_batch_progress(data: dict) -> None:
    """原子写：先写 .tmp 再 replace，避免断电/重启留下半个 JSON。"""
    try:
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROGRESS_FILE)
    except Exception:
        pass


def clear_batch_progress() -> None:
    try:
        PROGRESS_FILE.unlink(missing_ok=True)
    except Exception:
        pass


BATCH_LINES_FILE = RUNS / "batch-lines.json"


def save_batch_lines(lines: list) -> None:
    """把这一批要跑的行存盘。

    只存进度是不够的：服务重启后行数据（query_plan / params / qty）都在前端内存里，
    没有它就没法「接着跑」，只能重新上传 BOM。落一份到磁盘，重启后可以直接续跑。
    """
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        tmp = BATCH_LINES_FILE.with_suffix(".json.tmp")
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "fingerprint": batch_fingerprint(lines),
            "lines": lines,
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(BATCH_LINES_FILE)
    except Exception:
        pass


def load_batch_lines() -> list:
    """读回上一次存盘的行；没有或损坏时返回空列表。"""
    try:
        data = json.loads(BATCH_LINES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    return lines if isinstance(lines, list) else []


def batch_fingerprint(lines: list) -> str:
    """这一批 BOM 的身份指纹（16 位十六进制）。

    续跑只能复用「同一份 BOM」的进度。踩过的坑：换了一份同样是 19 行的新表，
    done_map 只按 line_no 匹配，把上一批 19 条结果整套套过来 —— 一行都不跑，
    收尾对账又拿旧候选商品去购物车里找，19 行全降级成「待核对」。
    指纹只取 BOM 身份字段，不含候选/价格/搜索词（那些会随引擎版本变化，
    同表重跑却变了指纹会导致重复加购）。
    """
    parts = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        dec = ln.get("decision") or {}
        parts.append(
            "\x1f".join(
                [
                    str(ln.get("line_no") or ""),
                    str(ln.get("mpn") or "").strip(),
                    str(ln.get("desc") or "").strip(),
                    str(ln.get("package") or "").strip(),
                    str(ln.get("params") or "").strip(),
                    str(ln.get("qty") or ""),
                    str(dec.get("headline") or "").strip(),
                ]
            )
        )
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]


def load_batch_lines_meta() -> dict:
    """上一次「开跑」时存下的 BOM 指纹（只读元信息，不返回整份行数据）。"""
    try:
        data = json.loads(BATCH_LINES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "saved_at": data.get("saved_at"),
        "fingerprint": str(data.get("fingerprint") or ""),
    }


def progress_belongs_to(prior: dict, lines: list) -> tuple[bool, str]:
    """这份进度是不是当前 BOM 跑出来的？返回 (是否匹配, 判定依据)。

    老版本进度文件里没有 fingerprint，退化成按 line_no 比对 headline：
    只要有一条对不上就说明换过 BOM（同一份表的 headline 不会变）。
    """
    prior = prior if isinstance(prior, dict) else {}
    rows = prior.get("lines") or {}
    if not rows:
        return True, "无进度"
    fp = batch_fingerprint(lines)
    prior_fp = prior.get("fingerprint") or ""
    if prior_fp:
        return (prior_fp == fp), "fingerprint"
    compared = 0
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        rec = rows.get(str(ln.get("line_no"))) or {}
        old_head = str(rec.get("headline") or "").strip()
        new_head = str((ln.get("decision") or {}).get("headline") or "").strip()
        if old_head and new_head:
            compared += 1
            if old_head != new_head:
                return False, "headline"
    return True, ("legacy-headline" if compared else "legacy-unknown")


def should_clear_stale_progress(batch_state: dict, ran: int) -> bool:
    """整批一行没跑、且所有「续跑跳过」的行都没在购物车里找到 → 清掉错配进度。

    典型场景就是换 BOM 或购物车被清空过：留着这份进度，下次点「继续加购」
    还是整批空跑，用户会反复看到「全识别成功、一件没加」。
    """
    state = batch_state or {}
    rec = state.get("reconcile") or {}
    downgraded = rec.get("downgraded") or []
    resumed_n = sum(1 for r in (state.get("results") or []) if r.get("resumed"))
    return bool(ran == 0 and resumed_n and downgraded and len(downgraded) >= resumed_n)


def batch_progress_summary(active_fingerprint: str = "") -> dict:
    """给前端/接口用的进度摘要。

    传入 active_fingerprint（当前这份 BOM）时，若进度属于另一份 BOM，
    对外表现成「没有进度」：否则前端会弹「上次已完成 N 行，接着跑」，
    用户以为跑过了，实际一行都不会执行。
    """
    data = load_batch_progress()
    rows = data.get("lines") or {}
    ok = [k for k, v in rows.items() if (v or {}).get("status") == "ok"]
    bad = [k for k, v in rows.items() if (v or {}).get("status") in ("failed", "uncertain")]
    fp = str(data.get("fingerprint") or "")
    stale = bool(active_fingerprint and fp and fp != active_fingerprint)
    if stale:
        ok, bad = [], []
    return {
        "has_progress": bool(rows) and not stale,
        "updated_at": data.get("updated_at"),
        "total": data.get("total") or 0,
        "ok_count": len(ok),
        "problem_count": len(bad),
        "ok_line_nos": sorted(int(k) for k in ok if str(k).isdigit()),
        "fingerprint": fp,
        "stale": stale,
    }


# 当前界面上正在看的那份 BOM（由 /api/analyze 写入）。
# 持久化到 runs/，服务重启后进度摘要也不会误报「上次已完成 N 行」。
ACTIVE_BOM_FILE = RUNS / "active-bom.json"
ACTIVE_BOM_FINGERPRINT = ""


def set_active_bom(lines: list) -> str:
    global ACTIVE_BOM_FINGERPRINT
    ACTIVE_BOM_FINGERPRINT = batch_fingerprint(lines)
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        ACTIVE_BOM_FILE.write_text(
            json.dumps(
                {
                    "fingerprint": ACTIVE_BOM_FINGERPRINT,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return ACTIVE_BOM_FINGERPRINT


def active_bom_fingerprint() -> str:
    if ACTIVE_BOM_FINGERPRINT:
        return ACTIVE_BOM_FINGERPRINT
    try:
        return str(json.loads(ACTIVE_BOM_FILE.read_text(encoding="utf-8")).get("fingerprint") or "")
    except Exception:
        return ""


_RE_ITEM_ID = re.compile(r"[?&](?:id|itemId)=(\d+)")


def _item_id_of(url: str) -> str:
    """从商品链接里取商品 id（淘宝 item.htm?id= / 天猫 detail.tmall.com）。"""
    m = _RE_ITEM_ID.search(url or "")
    return m.group(1) if m else ""


def batch_removal_targets(prog: dict | None = None) -> list[dict]:
    """从上次批量记录里挑出「本工具加进去的商品」，供购物车精准删除。

    只取 status=ok 的行：uncertain 连"到底加没加上"都不确定，不能凭它去删东西。
    2026-11 起加购记录里带 `candidate.url`，用商品 id 精确匹配；
    更早的进度没有 url，退回标题匹配（由执行层做数量守卫兜底）。
    """
    data = prog if isinstance(prog, dict) else load_batch_progress()
    rows = data.get("lines") or {}
    keys = sorted(
        rows.keys(),
        key=lambda x: (0, int(x)) if str(x).isdigit() else (1, 0),
    )
    out: list[dict] = []
    for key in keys:
        rec = rows.get(key) or {}
        if (rec.get("status") or "") != "ok":
            continue
        cand = rec.get("candidate") or {}
        title = str(cand.get("title") or "").strip()
        url = str(cand.get("url") or "").strip()
        item_id = _item_id_of(url)
        if not item_id and not title:
            continue
        out.append(
            {
                "item_id": item_id,
                "title": title[:120],
                "qty": int(rec.get("qty") or 0),
                "line_no": key,
            }
        )
    return out


def done_map_from_progress(prior: dict) -> dict:
    """续跑时哪些行可以跳过。

    ok = 确认已加购；uncertain = 点过「加入购物车」但页面没给确认信号。
    两者都不能重跑：uncertain 重跑会在购物车里多一份（淘宝同款同 SKU 会叠加数量），
    只能跳过、交给人工核对。
    """
    out = {}
    for k, v in ((prior or {}).get("lines") or {}).items():
        row = v or {}
        if row.get("status") in ("ok", "uncertain"):
            out[str(k)] = row
    return out


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------- 调试浏览器（CDP）自动就绪 ----------------
# settings.json 里一旦配了 cdp_url，执行层只会“连”那个浏览器，不会自己开。
# 用户点“启动”时如果调试版 Edge 没开着，就会连不上、会话秒退、页面回到“未启动”。
# 所以这里在拉会话之前，先按同一个 profile 把带调试端口的浏览器拉起来。

CDP_PROFILE_DIR = BASE / ".cdp-profile"


def cdp_endpoint_ok(cdp_url: str, timeout: float = 1.2) -> bool:
    if not cdp_url:
        return False
    url = str(cdp_url).rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except Exception:
        return False


def cdp_port(cdp_url: str) -> int:
    m = re.search(r":(\d{2,5})", str(cdp_url or ""))
    return int(m.group(1)) if m else 9222


def find_browser_exe() -> str | None:
    candidates: list[Path] = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def ensure_debug_browser(cdp_url: str, wait_seconds: float = 25.0, log=None) -> None:
    """确保 cdp_url 可连；连不上就按同一 profile 拉起调试版浏览器。"""
    if cdp_endpoint_ok(cdp_url):
        return
    exe = find_browser_exe()
    if not exe:
        raise SessionError(
            "调试浏览器连不上，也没找到 Edge / Chrome。请装 Edge，或删掉 settings.json 里的 cdp_url 改用内置浏览器。",
            "CDP_NO_BROWSER",
        )
    port = cdp_port(cdp_url)
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CDP_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "https://www.taobao.com",
    ]
    kwargs: dict = {"close_fds": True}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不跟着服务进程一起死，也不弹黑框
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"启动调试浏览器失败：{exc}", "CDP_LAUNCH_FAILED") from exc
    if log:
        log(f"[site] 已启动调试浏览器 {exe}（端口 {port}），等待就绪 …")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.4)
        if cdp_endpoint_ok(cdp_url):
            if log:
                log(f"[site] 调试浏览器已就绪 {cdp_url}")
            return
    raise SessionError(
        f"调试浏览器起来了但 {wait_seconds:g} 秒内没监听端口 {port}。"
        f"如果已经有用同一个 profile（{CDP_PROFILE_DIR}）开着的浏览器窗口，"
        "先全部关掉再点一次“启动淘宝服务”。",
        "CDP_NOT_READY",
    )

# 节奏参数：淘宝的验证码是按「短时间内的页面导航次数」触发的，
# 调行间隔只能拖长到某个点，真正管用的是周期性停一段让风控计数回落。
LINE_GAP_S = float(os.environ.get("TAOBAO_LINE_GAP", "1.2"))
COOLDOWN_EVERY = max(1, int(os.environ.get("TAOBAO_COOLDOWN_EVERY", "20")))
COOLDOWN_SEC = int(os.environ.get("TAOBAO_COOLDOWN_SEC", "40"))
# 自适应降温（2026-11-08）：uncertain（点了加购但接口不给回执）是淘宝软限流的
# 早期信号 —— 实测连续出现后 1~2 行内就会升级成硬拦截。所以连续 N 行 uncertain
# 就自动拉长冷却，别等验证弹出来才停。
UNCERTAIN_STREAK_TRIGGER = max(1, int(os.environ.get("TAOBAO_UNC_TRIGGER", "2")))
# 淘宝购物车约 200 行上限，满了之后每行都会失败。开跑前先看一眼件数：
# 到 STOP 直接不跑（省 20 分钟白跑），到 WARN 记日志提醒清车。
CART_LIMIT_WARN = max(1, int(os.environ.get("TAOBAO_CART_WARN", "150")))
CART_LIMIT_STOP = max(1, int(os.environ.get("TAOBAO_CART_STOP", "190")))
UNCERTAIN_COOL_BASE_S = int(os.environ.get("TAOBAO_UNC_COOL", "60"))
UNCERTAIN_COOL_MAX_S = int(os.environ.get("TAOBAO_UNC_COOL_MAX", "300"))

# 撞验证的处置顺序（2026-11-08 规则变更）：
#   ① 先自己扛 —— 执行层在下一次 search 前会刷新页面自恢复，这里立刻重试同一行；
#   ② 自动重试用完还是被拦（= 刷新后仍然出不来商品），才等人工。
VERIFY_AUTO_RETRY = max(1, int(os.environ.get("TAOBAO_VERIFY_AUTO_RETRY", "2")))
VERIFY_RETRY_GAP_S = float(os.environ.get("TAOBAO_VERIFY_RETRY_GAP", "12"))
# 自动恢复失败后，等人工完成验证（滑块/图片点选）的最长时间。
VERIFY_WAIT_S = int(os.environ.get("TAOBAO_VERIFY_WAIT", "1800"))
# 人工验证解除后，这一行最多再重试几次
VERIFY_HUMAN_RETRY = max(1, int(os.environ.get("TAOBAO_VERIFY_HUMAN_RETRY", "3")))

# I3 容差：淘宝的 0.1% 精密电阻常被一个「系列链接」覆盖多档精度，一刀切否决
# 会把本来能买的行判死；但 BOM 本身是精密档时，±1% 是**另一个等级的料**，
# 装上去分压/采样就偏 —— 所以普通档只降权，精密档才否决。
TOLERANCE_DEMOTE_RATIO = float(os.environ.get("TAOBAO_TOL_DEMOTE_RATIO", "3"))
TOLERANCE_PRECISION_PCT = float(os.environ.get("TAOBAO_TOL_PRECISION", "0.5"))
TOLERANCE_PRECISION_VETO = os.environ.get("TAOBAO_TOL_PRECISION_VETO", "1") not in ("0", "false", "False")

DEFAULT_TIMEOUTS = {
    "ping": 20,
    "search": 90,
    "open": 90,
    "select-sku": 90,
    "cart-add": 150,
    "cart-view": 60,
    "cart-remove": 180,
    "screenshot": 60,
    "dom": 60,
}


class SessionError(RuntimeError):
    def __init__(self, message, code="SESSION_ERROR", detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


class SessionRunner:
    def __init__(self, command=None, repo=None, artifacts=None):
        self._command = list(command) if command else None
        self._repo = Path(repo).resolve() if repo else REPO
        self._artifacts = Path(artifacts).resolve() if artifacts else ARTIFACTS
        self._proc: subprocess.Popen | None = None
        self._cmd_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._responses: queue.Queue = queue.Queue()
        self._logs: deque[str] = deque(maxlen=400)
        self._task_id = None
        self._ready = False
        self._waiting_login = False
        self._login_state = "unknown"
        self._verification = False
        self._verify_probe_at = 0.0
        self._desynced = False
        self._started_at = None
        self._log_path = None
        self._last_error = None
        self._guard = None  # 前台守卫：跑批时别让浏览器盖住用户正在用的软件
        self._guard_lock = threading.RLock()
        self._autohide_done = False
        self._browser_gone = False
        self._batch = {
            "running": False,
            "total": 0,
            "done": 0,
            "index": -1,
            "phase": "未开始",
            "results": [],
            "started_at": None,
            "finished_at": None,
            "stopped_reason": None,
            "verification": None,
        }
        # I7：把上次的进度装回状态机。否则重启后前端只看到 0/0，
        # 分不清「没跑过」和「跑了一半」，也没法判断 running 是真在跑还是残留。
        self._restore_batch_from_progress()

    def _restore_batch_from_progress(self) -> None:
        """启动时装载磁盘上的上次进度（不启动线程，running 恒为 False）。

        关键点：**没有活线程就不能说 running=true**。以前站点重启后
        `_batch` 可能是脏的（或干脆空着），前端要么显示「还在跑」点不动按钮，
        要么显示 0/0 看不出能续跑。
        """
        try:
            prog = load_batch_progress()
        except Exception:  # noqa: BLE001
            return
        lines = (prog or {}).get("lines") or {}
        if not lines:
            return
        results = []
        for key in sorted(lines, key=lambda x: int(x) if str(x).isdigit() else 0):
            v = lines[key] or {}
            try:
                line_no = int(v.get("line_no") or key)
            except (TypeError, ValueError):
                continue
            results.append(
                {
                    "line_no": line_no,
                    "headline": v.get("headline") or "",
                    "qty": v.get("qty"),
                    "status": v.get("status") or "pending",
                    "message": v.get("message") or "",
                    "attempts": v.get("attempts") or [],
                    "candidate": v.get("candidate"),
                    "screenshot": v.get("screenshot"),
                    "cost": v.get("cost"),
                    "final_price": v.get("final_price"),
                    "total_ms": v.get("total_ms"),
                }
            )
        settled = ("ok", "failed", "uncertain", "skipped")
        has_pending = any(r["status"] not in settled for r in results)
        # P2-4：上一轮已经跑完（没有 pending 行）就不要说「服务重启」——
        # 那是中断态文案，会误导人以为有一批没跑完。
        if has_pending:
            phase = "已装载上次进度（服务重启，点「继续加购」接着跑）"
            stopped = "server restarted"
        else:
            phase = "已装载上次进度（上一轮已跑完）"
            stopped = (prog or {}).get("stopped_reason") or ""
        with self._state_lock:
            self._batch.update(
                {
                    "running": False,
                    "total": int((prog or {}).get("total") or len(results)),
                    "done": sum(1 for r in results if r["status"] in settled),
                    "index": -1,
                    "phase": phase,
                    "results": results,
                    "started_at": (prog or {}).get("started_at"),
                    "finished_at": (prog or {}).get("finished_at"),
                    "stopped_reason": stopped,
                    "restored": True,
                }
            )

    # ---------------- 生命周期 ----------------
    def start(self) -> dict:
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                # 会话进程还活着，但浏览器被用户手动关了：那是个「僵尸会话」——
                # 命令全都会失败。先收掉再重建，否则点「启动」什么都不会发生。
                gone = self._browser_gone or self._browser_unreachable()
                if not gone:
                    return self.status()
                self._append_log("[site] 浏览器已关闭，重建会话 …")
                self.stop(timeout=5)
            self._reset_state()
            self._task_id = "bom-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            self._log_path = RUNS / f"session-{self._task_id}.log"
            self._log_path.write_text("", encoding="utf-8")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            env["TAOBAO_PROFILE_DIR"] = str(BASE / ".browser-profile")
            settings = load_settings()
            cdp_url = os.environ.get("TAOBAO_CDP_URL") or settings.get("cdp_url")
            if cdp_url:
                cdp_url = str(cdp_url).strip()
                # 页面点“启动”时调试版 Edge 常常没开着：先自动拉起，否则必然秒退
                if not cdp_endpoint_ok(cdp_url):
                    self._append_log(f"[site] {cdp_url} 未就绪，正在自动启动调试浏览器 …")
                    ensure_debug_browser(cdp_url, log=self._append_log)
                env["TAOBAO_CDP_URL"] = cdp_url

            # Pace profile: "fast" trims the simulated human pauses, the
            # networkidle caps and the per-step screenshots. Only timings
            # change — every SKU / cart check stays exactly the same.
            pace = str(settings.get("pace") or "fast").lower()
            if pace == "fast":
                env.setdefault("TAOBAO_SPEED", str(settings.get("speed") or 0.35))
                env.setdefault("TAOBAO_SHOTS", "fail")
                env.setdefault("TAOBAO_IDLE_TIMEOUT", str(settings.get("idle_timeout") or 3))
                env.setdefault("TAOBAO_BROWSE_SIM", "0")

            cmd = list(self._command) if self._command else [
                sys.executable,
                str(self._repo / "scripts" / "taobao.py"),
                "session",
                "--task-id",
                self._task_id,
            ]
            try:
                sub_kwargs: dict = {}
                if os.name == "nt":
                    # 不建控制台：站点是 cmd 拉起来的，会话进程再开一个黑框纯属添乱
                    sub_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(self._repo),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    # 子进程偶尔会按控制台编码（GBK）输出中文，严格 utf-8 解码会
                    # 在读取线程里抛 UnicodeDecodeError（日志直接断掉）。替换掉就好。
                    errors="replace",
                    bufsize=1,
                    env=env,
                    **sub_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                raise SessionError(f"无法启动会话进程：{exc}", "SESSION_START_FAILED")

            self._started_at = datetime.now().isoformat(timespec="seconds")
            threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()
            self._append_log(f"[site] session process started pid={self._proc.pid}")
            self._start_guard()
            return self.status()

    # ---------------- 窗口 / 焦点（别盖住用户正在用的软件） ----------------
    def _start_guard(self) -> None:
        """起前台守卫：自动化执行期间，浏览器一抢前台就还给用户。

        只在**发命令的时候**生效（begin/end），命令结束留 2s 尾巴；用户自己点
        这个浏览器窗口时守卫不动手，不会跟用户抢。
        """
        if _focus_guard is None:
            return
        with self._guard_lock:
            guard = self._guard
            if guard is not None and guard.is_alive():
                return
            try:
                port = cdp_port(
                    os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url")
                )
                self._guard = _focus_guard.FocusGuard(port=port, log=self._append_log).start()
                self._append_log("[site] 前台守卫已启动：执行命令时浏览器抢前台会立刻还给用户")
            except Exception as exc:  # noqa: BLE001
                self._guard = None
                self._append_log(f"[site] 前台守卫启动失败（不影响跑批）：{exc}")

    def _stop_guard(self) -> None:
        with self._guard_lock:
            guard, self._guard = self._guard, None
        if guard is not None:
            try:
                guard.stop_guard()
            except Exception:  # noqa: BLE001
                pass

    def _guard_begin(self) -> None:
        guard = self._guard
        if guard is not None:
            try:
                guard.begin()
            except Exception:  # noqa: BLE001
                pass

    def _guard_end(self) -> None:
        guard = self._guard
        if guard is not None:
            try:
                guard.end()
            except Exception:  # noqa: BLE001
                pass

    def _guard_pids(self) -> set[int]:
        """调试版 Edge 的 pid 集合（守卫里缓存过就直接用，省一次 powershell）。"""
        guard = self._guard
        if guard is not None:
            try:
                pids = guard.known_pids()
                if pids:
                    return pids
            except Exception:  # noqa: BLE001
                pass
        if _window_pos is None:
            return set()
        try:
            port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
            # 浏览器已经关了就别去查进程：那要起 powershell，每起一次就多一个
            # 控制台窗口（用户看到的就是「命令窗口一直弹出又关闭」）。
            if not _window_pos.debug_browser_alive(port):
                return set()
            return (
                _window_pos.debug_edge_pids()
                if port == 9222
                else _window_pos.debug_edge_pids_for(port)
            )
        except Exception:  # noqa: BLE001
            return set()

    def _browser_unreachable(self) -> bool:
        """调试浏览器还连得上吗（关掉网页/进程后 CDP 端口就不通了）。"""
        if _window_pos is None:
            return False
        try:
            port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
            return not _window_pos.debug_browser_alive(port, timeout=0.5)
        except Exception:  # noqa: BLE001
            return False

    def hide_browser(self) -> bool:
        """把调试浏览器藏到屏幕外（不最小化，渲染不停，截图照常）。"""
        if _window_pos is None:
            return False
        try:
            ok = _window_pos.hide(self._guard_pids())
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] 隐藏浏览器窗口失败：{exc}")
            return False
        if ok:
            self._append_log("[site] 浏览器窗口已藏到屏幕外（需要看时点「显示淘宝窗口」）")
        return ok

    def show_browser(self, pos: tuple[int, int] | None = None) -> bool:
        """把调试浏览器移回屏幕内（人工过验证 / 用户主动看），不抢前台。"""
        if _window_pos is None:
            return False
        try:
            ok = _window_pos.show(self._guard_pids(), pos or _window_pos.SCREEN_POS)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] 显示浏览器窗口失败：{exc}")
            return False
        return ok

    def stop(self, timeout: float = 8.0) -> dict:
        # UI 承诺「结束会话会关闭淘宝浏览器窗口」——只 quit 会话进程的话
        # Edge 还留在后台（2026-11-09 用户实测点关闭没反应）。这里补杀
        # 调试版 Edge 的进程树；只杀带 remote-debugging-port 的，
        # 用户自己开的普通 Edge 不受影响。会话进程不在（proc=None）时
        # 也要关 —— 那正是「浏览器还挂着、会话早就停了」的常见残留态。
        if _window_pos is not None and _window_pos.debug_browser_alive():
            _closed = _window_pos.close()
            self._append_log(
                "[site] debug Edge closed" if _closed else "[site] debug Edge close failed"
            )
        with self._cmd_lock:
            proc = self._proc
            if proc is None:
                return self.status()
            if proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self._append_log("[site] session stopped")
            # 先落盘再清内存：顺序反了会把进度覆盖成空（见 _save_progress 注释）
            self._save_progress()
            self._reset_state()
            # 会话都收工了，截图也没人要了
            self.cleanup_screenshots()
            return self.status()

    def _reset_state(self):
        self._stop_guard()  # 会话都停了，守卫也别再动人家窗口
        self._autohide_done = False
        self._browser_gone = False
        self._proc = None
        self._ready = False
        self._waiting_login = False
        self._login_state = "unknown"
        self._verification = False
        self._verify_probe_at = 0.0
        self._desynced = False
        self._task_id = None
        self._started_at = None
        self._last_error = None
        with self._state_lock:
            was_running = bool(self._batch.get("running"))
            self._batch = {
                "running": False,
                "total": 0,
                "done": 0,
                "index": -1,
                "phase": "未开始",
                "results": [],
                "started_at": None,
                "finished_at": None,
                "stopped_reason": "会话已结束，批量任务中止" if was_running else None,
                "verification": None,
            }
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    def _should_probe_verification(self) -> bool:
        """要不要在这一轮状态轮询里顺手探验证。"""
        if not self._verification:
            return False
        with self._state_lock:
            if self._batch.get("running"):
                return False  # 批量自己在发命令，成功即复位，别插队
        return (time.monotonic() - self._verify_probe_at) >= 2.5

    def recheck_verification(self, timeout: float = 20.0) -> bool:
        """探一次页面：验证是否已经人工处理完。返回 True = 已无验证，可以继续。

        为什么需要它：`_verification` 原先只在「下一条命令成功」时复位。
        批量一停就再没有命令发出，标志会一直挂着 true，前端据此把所有按钮
        锁死（继续加购点不动），必须重启会话才能解开 —— 就是那个死锁。

        抢锁用非阻塞：批量命令正在跑时直接跳过，绝不排队等，
        否则一旦等待超时会话被判成 desynced，反而更糟。
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        if not self._cmd_lock.acquire(blocking=False):
            return False
        try:
            try:
                resp = self.send({"cmd": "ping"}, timeout=timeout)
            except SessionError:
                return False
            ok = str(resp.get("status")) == "success"
            with self._state_lock:
                self._verification = not ok
                self._verify_probe_at = time.monotonic()
            if ok:
                self._append_log("[session] 验证已解除，按钮恢复可用")
            return ok
        finally:
            self._cmd_lock.release()

    def status(self, auto_probe: bool = True) -> dict:
        proc = self._proc
        running = proc is not None and proc.poll() is None
        # 标志卡住时顺手探一下：人工过完滑块，2~3 秒内界面自己就恢复了，
        # 不用重启会话，也不用点任何按钮。
        if auto_probe and running and self._should_probe_verification():
            self.recheck_verification()
        # 会话就绪（登录都好了）就把浏览器藏到屏幕外：调试版 Edge 是用户自己的
        # 窗口，开着不动也会盖住别人正在用的软件。等登录/验证期间不藏，那时候
        # 用户本来就要看这个窗口。每个会话只自动藏一次，之后用户点「显示」就不再抢。
        if (
            running
            and self._ready
            and not self._waiting_login
            and not self._verification
            and not self._autohide_done
        ):
            self._autohide_done = True
            threading.Thread(target=self.hide_browser, daemon=True).start()
        # 浏览器被手动关掉时，会话进程还活着但已经没法干活了：这里探一下端口，
        # 明确告诉界面「浏览器没了」，别等用户点了加购才报错。
        if running and _window_pos is not None and not self._verification:
            try:
                port = cdp_port(os.environ.get("TAOBAO_CDP_URL") or load_settings().get("cdp_url"))
                gone = not _window_pos.debug_browser_alive(port, timeout=0.5)
            except Exception:  # noqa: BLE001
                gone = self._browser_gone
            if gone and not self._browser_gone:
                self._browser_gone = True
                self._append_log("[site] 调试浏览器已关闭（端口不通）；请重新启动会话")
            elif not gone and self._browser_gone:
                self._browser_gone = False
                self._append_log("[site] 调试浏览器已恢复")
        with self._state_lock:
            return {
                "running": running,
                "ready": self._ready and running,
                "waiting_login": self._waiting_login and running,
                "login_state": self._login_state,
                "verification": self._verification,
                "browser_gone": self._browser_gone,
                "desynced": self._desynced,
                "task_id": self._task_id,
                "pid": proc.pid if running else None,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "repo": str(self._repo),
                "logs": list(self._logs)[-60:],
            }

    # ---------------- 输出读取 ----------------
    def _read_stdout(self, proc):
        buf = ""
        decoder = json.JSONDecoder()
        try:
            for line in proc.stdout:
                buf += line
                while True:
                    s = buf.lstrip()
                    if not s:
                        buf = ""
                        break
                    try:
                        obj, end = decoder.raw_decode(s)
                    except json.JSONDecodeError:
                        break
                    buf = s[end:]
                    if isinstance(obj, dict):
                        self._responses.put(obj)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[site] stdout reader stopped: {exc}")

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                self._append_log(line)
                low = line.lower()
                with self._state_lock:
                    if "session ready, waiting for commands" in low:
                        self._ready = True
                        self._waiting_login = False
                        self._last_error = None
                    if "cdp 端点连不上" in line or line.startswith("RuntimeError:"):
                        self._last_error = line.strip()
                    if "waiting for human takeover" in low or "手动完成淘宝登录" in line:
                        self._waiting_login = True
                        if self._login_state != "ok":
                            self._login_state = "waiting"
                    if "login successful" in low or "登录成功" in line:
                        self._login_state = "ok"
                    if "login failed or timed out" in low or "登录失败" in line:
                        self._login_state = "failed"
                    if "verification required" in low:
                        self._verification = True
        except Exception:
            pass

    def _append_log(self, line: str):
        entry = f"{datetime.now():%H:%M:%S} {line}"
        with self._state_lock:
            self._logs.append(entry)
        if self._log_path is not None:
            try:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    # ---------------- 命令 ----------------
    def send(self, cmd: dict, timeout: float | None = None) -> dict:
        with self._cmd_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise SessionError("淘宝会话未启动或已退出", "SESSION_NOT_STARTED")
            if self._desynced:
                raise SessionError("上一个命令超时，会话状态不确定；请结束会话后重新启动", "SESSION_DESYNCED")
            timeout = timeout or DEFAULT_TIMEOUTS.get(str(cmd.get("cmd")), 90)
            self._guard_begin()  # 这段区间里浏览器敢抢前台，守卫就把它压回去
            try:
                try:
                    proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                except Exception as exc:  # noqa: BLE001
                    raise SessionError(f"写入会话失败：{exc}", "SESSION_WRITE_FAILED")

                try:
                    resp = self._responses.get(timeout=timeout)
                except queue.Empty:
                    self._desynced = True
                    raise SessionError(
                        f"命令 {cmd.get('cmd')} 超时（{timeout:g}s）。页面可能卡在登录/验证/加载。",
                        "COMMAND_TIMEOUT",
                    )
            finally:
                self._guard_end()

            if resp.get("status") == "error":
                code = (resp.get("error") or {}).get("code")
                if code == "VERIFICATION_REQUIRED":
                    self._verification = True
            else:
                self._verification = False
            return resp

    # ---------------- 截图 ----------------
    def wait_verification(self, max_s: int | None = None, poll_s: float = 5.0) -> bool:
        """刷新自恢复失败后（页面刷完仍不出商品）才走这里：等人工完成验证。

        用 ping 当探针：会话进程在验证未解除时会一直回 VERIFICATION_REQUIRED，
        解除后第一条命令自动复位闸门 —— 所以这里只要反复 ping，成功即代表恢复。
        """
        deadline = time.monotonic() + (max_s if max_s is not None else VERIFY_WAIT_S)
        # 守卫先退场：用户自己点开窗口操作时，不会被「防抢焦点」压回去。
        guard = self._guard
        if guard is not None:
            guard.pause("等人工过验证")
        try:
            return self._wait_verification_loop(deadline, poll_s)
        finally:
            self.hide_browser()  # 用户手动打开过的话，结束（解除/超时/人停）后藏回屏幕外
            if guard is not None:
                guard.resume()

    @staticmethod
    def _move_debug_window(x: int | None = None, y: int | None = None) -> None:
        """老接口：调 window_pos.py 子进程挪窗口（保留给脚本/兼容用）。"""
        try:
            import subprocess
            args = [sys.executable, str(Path(__file__).parent / "window_pos.py")]
            if x is not None:
                args += [str(x), str(y)]
            subprocess.run(args, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            print(f"[batch] window_pos skipped: {exc}", flush=True)

    def _wait_verification_loop(self, deadline: float, poll_s: float) -> bool:
        last_log = 0.0
        total = max(0, int(deadline - time.monotonic()))
        # 不再自动弹窗（太突兀）：挂一个状态出去，前端弹站内提示，
        # 用户点「打开淘宝窗口」才真的把窗口移进来。
        with self._state_lock:
            self._batch["verification"] = {
                "waiting": True,
                "reason": "刷新后仍无商品，需要人工完成淘宝验证",
                "total_s": total,
                "remaining_s": total,
                "since": datetime.now().isoformat(timespec="seconds"),
            }
        self._append_log(
            f"[batch] 刷新后仍无商品 → 需要你手动完成验证（最多等 {total // 60} 分钟）"
        )
        try:
            while time.monotonic() < deadline:
                with self._state_lock:
                    if self._batch.get("stopped_reason"):
                        return False  # 人主动停了，别再等
                time.sleep(poll_s)
                try:
                    resp = self.send({"cmd": "ping"}, timeout=25)
                except SessionError as exc:
                    if exc.code in ("SESSION_NOT_STARTED", "SESSION_DESYNCED"):
                        return False
                    continue
                if resp.get("status") != "error":
                    self._append_log("[batch] 验证已解除，从断行继续")
                    return True
                now = time.monotonic()
                left = max(0, int(deadline - now))
                with self._state_lock:
                    v = self._batch.get("verification") or {}
                    v["remaining_s"] = left
                    self._batch["verification"] = v
                if now - last_log > 30:
                    last_log = now
                    with self._state_lock:
                        self._batch["phase"] = f"刷新后仍无商品，等人工验证（剩余 {left}s）"
                    print(f"[batch] 仍在等验证… 剩余 {left}s", flush=True)
            return False
        finally:
            with self._state_lock:
                self._batch["verification"] = None

    def persist_screenshot(self, path: str | None) -> str | None:
        if not path:
            return None
        src = Path(path)
        try:
            src = src.resolve()
            src.relative_to(self._artifacts)
        except (ValueError, OSError):
            return None
        if not src.is_file():
            return None
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{src.name}"
        dst = RUNS / name
        try:
            shutil.copy2(src, dst)
        except OSError:
            return None
        return "/runs/" + name

    def cleanup_screenshots(self, keep: int | None = None) -> dict:
        """清掉采集过程留下的截图。

        一行淘宝会截 1~3 张首屏图，119 行就是三四百 MB，而且只有当下核对
        用得上 —— 跑完就是纯垃圾。默认全清（settings.screenshot_keep=0），
        想留几张排查就把 screenshot_keep 调大。

        会话还在跑的时候不删：正在写的文件删了会让界面上的图裂掉。
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with self._state_lock:
                if self._batch.get("running"):
                    return {"deleted": 0, "freed_bytes": 0, "skipped": "批量运行中"}
        if keep is None:
            try:
                keep = int(load_settings().get("screenshot_keep") or 0)
            except (TypeError, ValueError):
                keep = 0
        keep = max(0, keep)

        deleted = 0
        freed = 0
        for folder in {self._artifacts, RUNS}:
            try:
                shots = [p for p in Path(folder).glob("*.png") if p.is_file()]
                shots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                continue
            for p in shots[keep:]:
                try:
                    size = p.stat().st_size
                    p.unlink()
                    deleted += 1
                    freed += size
                except OSError:
                    continue

        if deleted:
            mb = freed / 1024 / 1024
            self._append_log(f"[site] 已清理 {deleted} 张截图，释放 {mb:.1f} MB")
        return {"deleted": deleted, "freed_bytes": freed, "kept": keep}

    # ---------------- 业务封装 ----------------
    def search(self, line: dict, query: str | None = None, require_scored: bool = False) -> dict:
        """搜索并打分。

        require_scored=True（批量用）：一条搜索词虽然有结果，但候选全被硬门槛拒掉时，
        不要就此判整行失败 —— 换下一条搜索词再试。典型场景是「型号 + 封装」这种组合词
        在淘宝会召回到一堆近似型号，而只搜料号反而能命中（DMP4015SK3-13 DPAK → 0 命中，
        DMP4015SK3-13 → 有货）。
        """
        queries = [query] if query else list(line.get("query_plan") or [])
        if not queries:
            raise SessionError("该行没有可用搜索词", "NO_QUERY")
        attempts = []
        used = None
        items = []
        scored = None
        resp = None
        tried = queries[:4]
        for qi, q in enumerate(tried):
            try:
                # light：不截图、不取整页文本 —— 每行省 0.4–0.6s，119 行就是 1 分钟
                resp = self.send({"cmd": "search", "keyword": q, "light": True})
            except SessionError as exc:
                # 搜索页偶发超时也一样：换下一条词，而不是直接判这行没救
                if exc.code == "VERIFICATION_REQUIRED" or not _is_transient_error(exc):
                    raise
                attempts.append({"query": q, "error": {"code": exc.code, "message": str(exc)[:120]}})
                continue
            if resp.get("status") == "error":
                err = resp.get("error") or {}
                if err.get("code") == "VERIFICATION_REQUIRED":
                    raise SessionError(err.get("message") or "需要人工完成验证", "VERIFICATION_REQUIRED", err)
                attempts.append({"query": q, "error": err})
                continue
            got = (resp.get("data") or {}).get("items") or []
            if not got:
                attempts.append({"query": q, "items_count": 0})
                continue
            cands_try = [
                {
                    "title": _title_with_spec(line, it),
                    "spec_text": it.get("spec_text") or "",
                    "price": it.get("price_value"),
                    "sales": it.get("sales_count"),
                    "rating": it.get("rating"),
                    "shop_type": "",
                    "is_tmall": bool(it.get("is_tmall")),
                    "real_photo": False,
                    "index": it.get("index"),
                    "url": it.get("url"),
                }
                for it in got
            ]
            scored_try = engine.score_candidates(line, candidates=cands_try, execution=True)
            usable = any((c.get("score") or 0) > 0 for c in scored_try["candidates"])
            items, used, scored = got, q, scored_try
            if usable or not require_scored or qi == len(tried) - 1:
                break
            attempts.append(
                {"query": q, "items_count": len(got), "reason": "候选都不匹配，换下一条搜索词"}
            )

        if resp is None:
            raise SessionError("搜索没有返回结果", "SEARCH_NO_RESPONSE")
        if not items or scored is None:
            return {
                "ok": False,
                "code": "A01",
                "message": "搜索词都没有可用结果；不要放宽硬门槛，换词或人工处理。",
                "attempts": attempts,
                # 搜索走 light 模式不带图，失败时补拍一张，方便人看当时页面到底怎么了
                "screenshot": self.persist_screenshot(self._grab_shot()),
                "page_text": resp.get("page_text_summary"),
                "queries": queries,
            }

        return {
            "ok": True,
            "query": used,
            "items_count": len(items),
            "candidates": scored["candidates"],
            "stats": scored["stats"],
            "verdict": scored["verdict"],
            "attempts": attempts,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
            "queries": queries,
        }

    def _grab_shot(self) -> str | None:
        """按需补拍一张当前视口截图（轻量模式下失败路径用）。"""
        try:
            resp = self.send({"cmd": "screenshot"})
            return (resp.get("screenshot") if isinstance(resp, dict) else None) or None
        except Exception:  # noqa: BLE001
            return None

    def open_item(self, line: dict, index: int) -> dict:
        # shot=False：开页截图对批量没价值（证据图在加购那一步），省 0.3–0.5s/行
        resp = self.send({"cmd": "open", "index": int(index), "shot": False})
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            raise SessionError(err.get("message") or "打开商品失败", err.get("code") or "OPEN_FAILED", err)
        data = resp.get("data") or {}
        item = data.get("item") or {}
        groups = data.get("sku_groups") or []
        suggestions, unresolved = suggest_sku(line, groups)
        sku_texts = [s.get("value", "") for s in suggestions]
        pack, pack_src = _pack_from_texts(sku_texts, explicit_only=False, allow_generic=True)
        if not pack:
            pack, pack_src = _title_pack(item.get("title") or "")
        if not pack:
            pack, pack_src = _pack_from_texts([resp.get("page_text_summary") or ""], explicit_only=True)
        bom_qty = line.get("qty")
        suggested_qty, pack, actual_pcs = _plan_qty(bom_qty, pack)
        price = data.get("detail_price")
        unit_price = round(price / pack, 6) if (price and pack and pack > 1) else price
        return {
            "ok": True,
            "item": item,
            "detail_price": price,
            "sku_groups": groups,
            "no_sku": not groups,
            "suggestions": suggestions,
            "unresolved": unresolved,
            "pack_size": pack if pack and pack > 1 else None,
            "pack_source": pack_src,
            "bom_qty": bom_qty,
            "suggested_qty": suggested_qty,
            "actual_pcs": actual_pcs,
            "unit_price": unit_price,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    def add_to_cart(self, line: dict, index: int, selections: list, qty: int,
                    allow_empty: bool = False, auto_qty: bool = False,
                    pack_hint: int | None = None, price_hint: float | None = None) -> dict:
        skip_sku = not isinstance(selections, list) or not selections
        if skip_sku and not allow_empty:
            return {"ok": False, "code": "NO_SELECTIONS", "message": "没有 SKU 选择，拒绝加购（避免默认 SKU）"}

        _sku_ms = 0
        groups = []
        resp = {}
        final_price = None
        pack, pack_src = None, ""
        if skip_sku:
            # 单规格商品：页面根本没有 SKU 选择器，直接按数量加购（没有「默认 SKU」风险）
            pass
        else:
            _t_sku = time.monotonic()
            resp = self.send({"cmd": "select-sku", "selections": selections, "light": True})
            _sku_ms = int((time.monotonic() - _t_sku) * 1000)
            if resp.get("status") == "error":
                err = resp.get("error") or {}
                return {
                    "ok": False,
                    "code": err.get("code") or "SELECT_SKU_FAILED",
                    "message": err.get("message") or "选择 SKU 失败",
                    "detail": err,
                    "screenshot": self.persist_screenshot(resp.get("screenshot")),
                }

            data = resp.get("data") or {}
            groups = data.get("sku_groups") or []
            final_price = data.get("final_price")
            missing = groups_without_selection(groups)
            if missing:
                return {
                    "ok": False,
                    "code": "SKU_INCOMPLETE",
                    "message": "还有 SKU 组未选中，已停止加购。请在页面里补齐或手动选择。",
                    "missing": missing,
                    "groups": groups,
                    "screenshot": self.persist_screenshot(resp.get("screenshot")),
                }
            # 选定 SKU 之后才知道这份到底多少只：以页面已选值为准，重算加购份数
            sel_vals = [v.get("text", "") for g in groups for v in (g.get("values") or []) if v.get("selected")]
            pack, pack_src = _pack_from_texts(sel_vals, explicit_only=False, allow_generic=True)
            if not pack:
                pack, pack_src = _pack_from_texts([s.get("value", "") for s in selections], explicit_only=False, allow_generic=True)

        eff_pack = pack or pack_hint
        if auto_qty:
            qty, eff_pack, actual_pcs = _plan_qty(line.get("qty"), eff_pack)
        else:
            actual_pcs = int(qty) * int(eff_pack or 1)
            eff_pack = int(eff_pack or 1)
        qty = max(1, int(qty))

        # 选完 SKU 的价是「每份价」。和标题价差 5 倍以上，说明标题那 ¥1 是引流价
        # （常见于「多个规格一个链接」，贵的规格才是真实价），记下来给界面/报告看。
        price_shift = None
        if final_price is not None and price_hint:
            try:
                if float(final_price) >= float(price_hint) * 5:
                    price_shift = round(float(final_price) / float(price_hint), 1)
            except (TypeError, ValueError):
                price_shift = None

        # 金额闸门：贵得离谱就不点加购（真正的钱是在 cart-add 那一下花出去的）
        est_cost = round(float(final_price) * int(qty), 2) if final_price is not None else None
        cap = _line_cost_cap(line.get("category"))
        if est_cost is not None and cap and est_cost > cap:
            return {
                "ok": False,
                "code": "COST_TOO_HIGH",
                "message": (
                    f"这一行要花 ¥{est_cost:g}（{qty} 份 × ¥{final_price:g}／份），"
                    f"超过单行上限 ¥{cap:g}，已放弃自动加购"
                ),
                "est_cost": est_cost,
                "cap": cap,
                "qty": qty,
                "final_price": final_price,
                "price_shift": price_shift,
                "pack_size": eff_pack if eff_pack and eff_pack > 1 else None,
                "actual_pcs": actual_pcs,
                "unit_price": (
                    round(final_price / eff_pack, 6)
                    if (final_price and eff_pack and eff_pack > 1)
                    else final_price
                ),
                "screenshot": self.persist_screenshot(resp.get("screenshot")),
            }

        # 超量太狠 + 不是小钱：整盘/整卷报价，BOM 只要十来个却买到一大盘，
        # 这种不自动下单 —— 交给下一家卖小包装的，或留给人工判断。
        if (
            auto_qty
            and est_cost is not None
            and actual_pcs
            and line.get("qty")
            and est_cost >= 50
            and float(actual_pcs) / max(1, int(line.get("qty"))) >= 50
        ):
            _over = float(actual_pcs) / max(1, int(line.get("qty")))
            return {
                "ok": False,
                "code": "OVERBUY_TOO_MUCH",
                "message": (
                    f"要 {int(line.get('qty'))} 只却到手 {int(actual_pcs)} 只（超量 {_over:.0f} 倍），"
                    f"合计 ¥{est_cost:g}，已放弃自动加购"
                ),
                "over_ratio": round(_over, 1),
                "est_cost": est_cost,
                "qty": qty,
                "final_price": final_price,
                "pack_size": eff_pack if eff_pack and eff_pack > 1 else None,
                "actual_pcs": actual_pcs,
                "screenshot": self.persist_screenshot(resp.get("screenshot")),
            }

        _t_add = time.monotonic()
        # light：证据图用 jpeg + 不拉整页文本，单行省 2–3s
        resp = self.send({"cmd": "cart-add", "qty": int(qty), "light": True})
        _add_ms = int((time.monotonic() - _t_add) * 1000)
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_ADD_FAILED",
                "message": err.get("message") or "加购失败",
                "detail": err,
                "screenshot": self.persist_screenshot(resp.get("screenshot") or err.get("screenshot")),
            }

        data = resp.get("data") or {}
        if not data.get("cart_added"):
            return {
                "ok": False,
                "code": "CART_UNCONFIRMED",
                "message": "已点击加入购物车，但未在页面/角标/购物车里确认到商品。请打开购物车人工核对，不要重复点击。",
                "screenshot": self.persist_screenshot(resp.get("screenshot")),
                "groups": groups,
            }
        # 单规格商品没有 SKU 可选，select-sku 拿不到价 —— 用详情页的价兜底，否则金额永远是空的
        if final_price is None and price_hint is not None:
            final_price = price_hint

        return {
            "ok": True,
            "cart_added": data.get("cart_added"),
            "confirmed": data.get("confirmed"),
            "cart_verified_via": data.get("cart_verified_via"),
            "qty": data.get("qty") or qty,
            "qty_sent": qty,
            "qty_confirmed": data.get("qty_confirmed"),
            "pack_size": eff_pack if eff_pack and eff_pack > 1 else None,
            "pack_source": pack_src,
            "actual_pcs": actual_pcs,
            "bom_qty": line.get("qty"),
            "final_price": final_price,
            "price_shift": price_shift,
            # 实付金额 = 页面每份单价 × 买的份数（不是单只价，单只价另算）
            "cost": round(float(final_price) * int(qty), 2) if final_price is not None else None,
            "unit_price": round(final_price / eff_pack, 6) if (final_price and eff_pack > 1) else final_price,
            "sku_verified": data.get("sku_verified"),
            "sku_selection_count": data.get("sku_selection_count"),
            "select_ms": _sku_ms,
            "add_ms": _add_ms,
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
            "groups": groups,
        }

    def cart_view(self) -> dict:
        resp = self.send({"cmd": "cart-view"})
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_VIEW_FAILED",
                "message": err.get("message") or "读取购物车失败",
                "screenshot": self.persist_screenshot(err.get("screenshot") or resp.get("screenshot")),
            }
        data = resp.get("data") or {}
        return {
            "ok": True,
            "cart_item_count": data.get("cart_item_count"),
            "cart_empty": bool(data.get("cart_empty")),
            # I4：逐行明细一起回，结算前核对/定位要删的行全靠这个
            "rows": data.get("rows") or [],
            "rows_page": data.get("rows_page"),
            "rows_total": data.get("rows_total"),
            "pages_read": data.get("pages_read"),
            "rows_degraded": data.get("rows_degraded"),
            "rows_selector": data.get("rows_selector"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    # ---------------- 删除购物车 ----------------
    def cart_remove(self, keyword: str | None = None, remove_all: bool = False) -> dict:
        """Delete matching cart rows (or everything) — the only destructive command."""
        req: dict = {"cmd": "cart-remove"}
        if keyword:
            req["keyword"] = keyword
        if remove_all:
            req["all"] = True
        resp = self.send(req)
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_REMOVE_FAILED",
                "message": err.get("message") or "删除购物车条目失败",
                "matched": err.get("matched"),
                "screenshot": self.persist_screenshot(err.get("screenshot") or resp.get("screenshot")),
            }
        data = resp.get("data") or {}
        return {
            "ok": True,
            "removed": data.get("removed"),
            "before": data.get("before"),
            "after": data.get("after"),
            "matched": data.get("matched") or [],
            "confirm_clicked": data.get("confirm_clicked"),
            "verified": data.get("verified"),
            "pages_touched": data.get("pages_touched"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
            "page_text": resp.get("page_text_summary"),
        }

    def cart_remove_last_batch(self, dry: bool = False) -> dict:
        """只删「上一批加购进去」的那些商品，用户自己加的保持不动。

        dry=True 时执行层只扫描并返回计划（会删哪几件、跳过哪几件），
        一个按钮都不点 —— 前端先用它给用户看清，再决定要不要真删。
        """
        targets = batch_removal_targets()
        if not targets:
            return {
                "ok": False,
                "code": "NO_BATCH_RECORD",
                "message": "没有可用的批量加购记录：上次批量没有成功加购的行",
                "delete": [],
                "skip": [],
                "missing": [],
                "merged": [],
            }
        resp = self.send(
            {"cmd": "cart-remove", "items": targets, "dry": dry}, timeout=240
        )
        if resp.get("status") == "error":
            err = resp.get("error") or {}
            return {
                "ok": False,
                "code": err.get("code") or "CART_REMOVE_FAILED",
                "message": err.get("message") or "删除购物车条目失败",
                "screenshot": self.persist_screenshot(
                    err.get("screenshot") or resp.get("screenshot")
                ),
            }
        data = resp.get("data") or {}
        return {
            "ok": True,
            "dry": bool(data.get("dry")),
            "targets": data.get("targets"),
            "delete": data.get("delete") or [],
            "skip": data.get("skip") or [],
            "missing": data.get("missing") or [],
            "merged": data.get("merged") or [],
            "removed": data.get("removed"),
            "removed_titles": data.get("removed_titles") or [],
            "before": data.get("before"),
            "after": data.get("after"),
            "verified": data.get("verified"),
            "cart_rows": data.get("cart_rows"),
            "cart_degraded": data.get("cart_degraded"),
            "code": data.get("code"),
            "message": data.get("message"),
            "screenshot": self.persist_screenshot(resp.get("screenshot")),
        }

    # ---------------- 批量加购 ----------------
    def batch_status(self) -> dict:
        with self._state_lock:
            data = json.loads(json.dumps(self._batch, ensure_ascii=False, default=str))
        # 金额汇总：只有拿到页面单价的行才计入，避免把 None 当 0 摊平均价
        rows = data.get("results") or []
        cost, n_cost, okn = 0.0, 0, 0
        for r in rows:
            if r.get("status") != "ok":
                continue
            okn += 1
            c = r.get("cost")
            if isinstance(c, (int, float)):
                cost += float(c)
                n_cost += 1
        data["cost_total"] = round(cost, 2)
        data["cost_lines"] = n_cost
        data["ok_count"] = okn
        data["cost_avg"] = round(cost / n_cost, 2) if n_cost else None
        # 服务重启后内存里的 batch 是空的，但进度文件还在 —— 从进度里把累计金额捞出来，
        # 免得重启一次「合计花费」就变 0。按文件 mtime 缓存，别每秒轮询都读盘。
        if not rows:
            try:
                mt = PROGRESS_FILE.stat().st_mtime
            except OSError:
                mt = None
            cache = getattr(self, "_cost_cache", None)
            if mt is not None and (cache is None or cache[0] != mt):
                prior = load_batch_progress().get("lines") or {}
                c, n, o = 0.0, 0, 0
                for v in prior.values():
                    if (v or {}).get("status") != "ok":
                        continue
                    o += 1
                    cv = (v or {}).get("cost")
                    if isinstance(cv, (int, float)):
                        c += float(cv)
                        n += 1
                cache = (mt, round(c, 2), n, o)
                self._cost_cache = cache
            if cache:
                data["cost_total"] = cache[1]
                data["cost_lines"] = cache[2]
            if cache and not okn:
                data["ok_count"] = cache[3]
            data["cost_from_progress"] = True
        # I7：明确告诉前端「有没有可续跑的进度」「是不是真的有线程在跑」
        data["has_progress"] = bool(data.get("results"))
        data["resumable"] = (not data.get("running")) and bool(data.get("results"))
        data["restored_from_disk"] = bool(data.get("restored"))
        return data

    def _batch_patch(self, **patch):
        with self._state_lock:
            self._batch.update(patch)

    def _batch_line_patch(self, index: int, **patch):
        with self._state_lock:
            if 0 <= index < len(self._batch["results"]):
                self._batch["results"][index].update(patch)

    def start_batch(
        self,
        lines: list,
        prefer_cheap: bool = True,
        max_candidates: int = 2,
        resume: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """启动批量加购。

        resume=True（默认）时读 runs/batch-progress.json，把上次已经「已加入购物车」的行
        直接标成 ok 并跳过 —— 暂停/停止/重启后接着跑，不会从头重复加购。
        想真正重头来，传 resume=False（或先调 /api/taobao/batch-reset）。
        """
        if not isinstance(lines, list) or not lines:
            raise SessionError("没有可执行的行", "BATCH_EMPTY")
        # 浏览器被关掉时跑批只会得到一堆「搜索词没有可用结果」的假失败，
        # 直接拦住并说清楚怎么办。
        if self._browser_gone or self._browser_unreachable():
            raise SessionError(
                "淘宝浏览器已关闭，先点「启动淘宝会话」重建会话（会自动重开浏览器）",
                "BROWSER_GONE",
            )
        fp = batch_fingerprint(lines)
        # 必须在覆盖 batch-lines.json 之前读：里面存的是「上一次开跑」的 BOM 指纹。
        # 只看进度文件不够 —— 用户可能已经手动点过「重置进度」，但界面内存里
        # 还留着上一批的 ok_count，照样会被骗进一次整批空跑。
        prev_lines_meta = load_batch_lines_meta()
        prior = load_batch_progress() if resume else {}
        stale_progress = False
        if resume and (prior.get("lines") or {}):
            belongs, why = progress_belongs_to(prior, lines)
            prev_fp = prev_lines_meta.get("fingerprint") or ""
            # 新进度文件自带明确指纹时以它为准；只有老进度（无 fingerprint）
            # 才需要拿 batch-lines 里「上次开跑的 BOM」当佐证。
            if not prior.get("fingerprint") and prev_fp and prev_fp != fp:
                belongs, why = False, "上次开跑的 BOM 指纹"
            if not belongs:
                stale_progress = True
                prior = {}
                clear_batch_progress()
                _log(f"[batch] 检测到 BOM 已更换（{why} 不一致），旧进度作废，本批从头跑")
        save_batch_lines(lines)
        done_map = done_map_from_progress(prior)
        results, resumed = [], 0
        for ln in lines:
            key = str(ln.get("line_no"))
            head = (ln.get("decision") or {}).get("headline") or ln.get("mpn") or ""
            prev = done_map.get(key)
            if prev:
                resumed += 1
                _uncertain = (prev.get("status") or "") == "uncertain"
                results.append(
                    {
                        "line_no": ln.get("line_no"),
                        "headline": head or prev.get("headline") or "",
                        "qty": ln.get("qty"),
                        "status": "uncertain" if _uncertain else "ok",
                        "message": (
                            "上次已点加购但页面没确认，跳过以免重复加购，请到购物车核对"
                            if _uncertain
                            else "上次已完成（续跑跳过）"
                        ),
                        "attempts": [],
                        "resumed": True,
                        "candidate": prev.get("candidate"),
                        "price": prev.get("price"),
                        "final_price": prev.get("final_price") if prev.get("final_price") is not None else prev.get("price"),
                        "cost": prev.get("cost"),
                        "unit_price": prev.get("unit_price"),
                        "pack_size": prev.get("pack_size"),
                        "actual_pcs": prev.get("actual_pcs"),
                        "screenshot": prev.get("screenshot"),
                        # 续跑行也要把「选了哪个 SKU / 靠什么确认加购」带回来，
                        # 否则界面上这条记录只剩金额，导出 CSV 的 SKU、确认方式两列是空的。
                        "sku": prev.get("sku") or [],
                        "cart_verified_via": prev.get("cart_verified_via"),
                        "query": prev.get("query"),
                    }
                )
            else:
                results.append(
                    {
                        "line_no": ln.get("line_no"),
                        "headline": head,
                        "qty": ln.get("qty"),
                        "status": "pending",
                        "message": "",
                        "attempts": [],
                    }
                )
        # 开跑前先清掉上一轮 / 手动操作留下的截图（默认全清，见 settings.screenshot_keep）
        with self._state_lock:
            already = bool(self._batch.get("running"))
        if not already:
            self.cleanup_screenshots()
        with self._state_lock:
            if self._batch.get("running"):
                raise SessionError("批量加购任务已在运行，等它结束或先停止", "BATCH_RUNNING")
            self._batch = {
                "running": True,
                "total": len(lines),
                "done": resumed,
                "index": -1,
                "phase": (
                    "演练准备中（不点加购）"
                    if dry_run
                    else (
                        "准备中（新 BOM，旧进度已作废）"
                        if stale_progress
                        else (f"准备中（续跑，跳过已完成 {resumed} 行）" if resumed else "准备中")
                    )
                ),
                "dry_run": bool(dry_run),
                "prefer_cheap": bool(prefer_cheap),
                "results": results,
                "resumed": resumed,
                "fingerprint": fp,
                "stale_progress_dropped": stale_progress,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "stopped_reason": None,
            }
        threading.Thread(
            target=self._run_batch,
            args=(list(lines), bool(prefer_cheap), max(1, int(max_candidates)), bool(dry_run)),
            daemon=True,
        ).start()
        return self.batch_status()

    def stop_batch(self) -> dict:
        with self._state_lock:
            if self._batch.get("running"):
                self._batch["stopped_reason"] = "用户请求停止（当前行完成后生效）"
        self._save_progress()
        return self.batch_status()

    def _save_progress(self):
        """把已定局（ok/failed/uncertain）的行按 line_no 存盘，供下次续跑跳过。"""
        prior_data = load_batch_progress() or {}
        prior_lines = prior_data.get("lines") or {}
        with self._state_lock:
            rows = {}
            for r in self._batch.get("results") or []:
                if r.get("status") not in ("ok", "failed", "uncertain"):
                    continue
                ln = r.get("line_no")
                if ln is None:
                    continue
                payload = {
                    "status": r["status"],
                    "headline": r.get("headline"),
                    "message": r.get("message"),
                    "qty": r.get("qty"),
                    "price": r.get("final_price") if r.get("final_price") is not None else r.get("price"),
                    "final_price": r.get("final_price"),
                    "cost": r.get("cost"),
                    "unit_price": r.get("unit_price"),
                    "pack_size": r.get("pack_size"),
                    "actual_pcs": r.get("actual_pcs"),
                    # 选了哪个 SKU、靠什么信号确认加购：续跑/导出都要用，缺了就查不出买了什么规格
                    "sku": r.get("sku") or [],
                    "cart_verified_via": r.get("cart_verified_via"),
                    "query": r.get("query"),
                    # uncertain 行要留着候选和截图，续跑跳过时还能让人核对
                    "candidate": r.get("candidate"),
                    "screenshot": r.get("screenshot"),
                    # 耗时埋点：只用于复盘速度，不影响续跑判定
                    "total_ms": r.get("total_ms"),
                    "search_ms": r.get("search_ms"),
                    "open_ms": r.get("open_ms"),
                    "select_ms": r.get("select_ms"),
                    "add_ms": r.get("add_ms"),
                    "attempts_n": len(r.get("attempts") or []),
                }
                if r.get("resumed"):
                    # 续跑跳过的行：结论是从进度文件里读出来的，别拿它覆盖上一轮的
                    # 真实记录（message 会变成「续跑跳过」、耗时全变 null）。
                    # 只有结论真的变了（收尾对账把 ok 降成 uncertain）才写回，
                    # 而且明细沿用上一轮，免得把 SKU/金额/截图抹掉。
                    prev = prior_lines.get(str(ln)) or {}
                    if (prev.get("status") or "") == payload["status"]:
                        continue
                    for key, val in prev.items():
                        if key in ("status", "message"):
                            continue
                        if val not in (None, "", [], {}):
                            payload[key] = val
                rows[str(ln)] = payload
            data = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "total": self._batch.get("total") or 0,
                "started_at": self._batch.get("started_at"),
                "finished_at": self._batch.get("finished_at"),
                "lines": rows,
            }
        # 合并写，不是覆盖写。
        # 曾经踩过：会话停止时 _reset_state 先把内存里的 batch 清空，
        # _run_batch 的 finally 随后把「空 rows」落盘，把辛苦跑出来的
        # 几十行进度全抹了 —— 下次「继续加购」就当成新任务从第 1 行重来，
        # 购物车直接翻倍。所以这里先把旧文件读出来合并。
        if not rows:
            return
        merged = dict(prior_lines)
        merged.update(rows)  # 本轮的结论覆盖旧结论（失败转成功要生效）
        save_batch_progress(
            {
                "updated_at": data["updated_at"],
                "total": data["total"] or prior_data.get("total") or 0,
                "lines": merged,
                # 绑定 BOM 身份：换表后这份进度必须整体作废，不能按行号套用
                "fingerprint": self._batch.get("fingerprint") or prior_data.get("fingerprint") or "",
                # I7：时间戳也落盘，重启后状态机能还原「这轮什么时候开始/结束」
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
            }
        )

    def _precheck_cart_limit(self, dry_run: bool = False) -> None:
        """开跑前看购物车件数：接近淘宝上限就别白跑了。

        实测上限约 200 行，满了之后每一行都会失败；与其跑 20 分钟全红，
        不如开跑 5 秒就看一眼（cart-view 一次）。
        """
        if dry_run:
            return
        try:
            cv = self.send({"cmd": "cart-view"}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            _log(f"[batch] cart precheck skipped: {exc}")
            return
        data = cv.get("data") or {}
        count = data.get("cart_item_count")
        with self._state_lock:
            self._batch["cart_count_before"] = count
        if not isinstance(count, int):
            return
        if count >= CART_LIMIT_STOP:
            with self._state_lock:
                self._batch["stopped_reason"] = (
                    f"购物车已有 {count} 件，接近淘宝上限（约 200 行），先清理再跑"
                )
            self._append_log(
                f"[batch] 购物车 {count} 件 ≥ 阈值 {CART_LIMIT_STOP}，不启动（先清理购物车）"
            )
            return
        if count >= CART_LIMIT_WARN:
            self._append_log(
                f"[batch] 购物车已有 {count} 件，接近上限（约 200 行），建议先清理"
            )

    def _run_batch(self, lines: list, prefer_cheap: bool, max_candidates: int, dry_run: bool = False):
        ran = 0
        _unc_streak = 0  # 连续 uncertain 计数：软限流的早期信号
        _slow_streak = 0  # 连续「加购超过 5s」计数：限流前兆
        # 开跑先把调试浏览器藏到屏幕外：逐行搜索/点加购时 Chromium 会抢前台，
        # 用户正在用的软件被盖住（实测反馈）。藏起来后连"盖住"的机会都没有，
        # 加上 send() 期间的前台守卫，抢了也会在 0.2s 内还回去。
        try:
            self.hide_browser()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[batch] 隐藏浏览器窗口失败（不影响跑批）：{exc}")
        try:
            # 开跑前预检购物车件数：淘宝上限约 200 行，满了一行都加不进去。
            # 这里直接看一次，比跑 20 分钟全失败再回来查划算。
            self._precheck_cart_limit(dry_run=dry_run)
            if self._batch.get("stopped_reason"):
                return
            for i, line in enumerate(lines):
                _skip_why = ""
                with self._state_lock:
                    if self._batch.get("stopped_reason"):
                        break
                    if self._batch["results"][i].get("resumed"):
                        # 上次已经加过的行：直接跳过，不重复下单
                        continue
                    if not (
                        line.get("query_plan")
                        or (line.get("decision") or {}).get("search_queries")
                    ):
                        # 残缺行（型号/名称/参数都空，只剩一个封装）：没有搜索词可搜，
                        # 跑下去只会在 search() 里抛 NO_QUERY，白等一轮再记一条「失败」。
                        # 直接标「跳过」并写清原因，让人回去补 BOM。
                        _skip_why = (
                            "；".join(line.get("blocking") or [])
                            or "该行没有可用搜索词（型号/名称/参数都为空）"
                        )
                        self._batch["results"][i].update(
                            status="skipped", message=f"跳过：{_skip_why}"
                        )
                    else:
                        self._batch["index"] = i
                        self._batch["phase"] = f"第 {i + 1}/{len(lines)} 行"
                        self._batch["results"][i]["status"] = "running"
                if _skip_why:
                    self._append_log(
                        f"[batch] line #{line.get('line_no')} 跳过（无搜索词）：{_skip_why}"
                    )
                    continue
                if ran:
                    # 行间留一点间隔：连续狂翻商品页最容易触发滑块验证。
                    # 淘宝的限速看着是「短窗口内导航次数」——实测 1.0s/行 跑十行左右
                    # 就会弹验证。所以光调行间隔不够，还得每 N 行整段冷却一次。
                    time.sleep(LINE_GAP_S)
                    if ran % COOLDOWN_EVERY == 0:
                        self._append_log(
                            f"[batch] 风控冷却 {COOLDOWN_SEC}s（已跑 {ran} 行）"
                        )
                        self._batch["phase"] = f"风控冷却 {COOLDOWN_SEC}s（已跑 {ran} 行）"
                        _cd = COOLDOWN_SEC
                        while _cd > 0 and not self._batch.get("stopped_reason"):
                            time.sleep(min(5, _cd))
                            _cd -= 5
                ran += 1
                _verify_retries = 0
                _verify_human_retries = 0
                _stop_after = False
                while True:
                    try:
                        _t0 = time.monotonic()
                        res = self._add_line_to_cart(line, prefer_cheap, max_candidates, dry_run=dry_run)
                        res["total_ms"] = int((time.monotonic() - _t0) * 1000)
                        self._append_log(
                            f"[batch] line #{line.get('line_no')} {res.get('status')} "
                            f"total={res['total_ms'] / 1000:.1f}s "
                            f"search={(res.get('search_ms') or 0) / 1000:.1f}s "
                            f"open={(res.get('open_ms') or 0) / 1000:.1f}s "
                            f"sku={(res.get('select_ms') or 0) / 1000:.1f}s "
                            f"add={(res.get('add_ms') or 0) / 1000:.1f}s"
                        )
                        break
                    except SessionError as exc:
                        res = {
                            "line_no": line.get("line_no"),
                            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
                            "qty": line.get("qty"),
                            "attempts": [],
                            "status": "failed",
                            "message": str(exc),
                            "code": exc.code,
                        }
                        if exc.code == "VERIFICATION_REQUIRED":
                            # 新规则（2026-11-08）：验证先自己扛。
                            # 执行层在下一次 search 前会刷新页面自恢复，所以这里**立刻重试**
                            # 同一行，既不等 30 分钟，也不惊动人；
                            # 只有自动重试用完、刷新后仍然出不来商品，才升级为人工验证。
                            # 已经点过「加入购物车」的行绝不能重跑，否则重复下单。
                            _added = bool((exc.detail or {}).get("cart_added"))
                            _step = verification_next_step(_added, _verify_retries, _verify_human_retries)
                            if _step == "auto":
                                _verify_retries += 1
                                self._append_log(
                                    f"[batch] 第 {line.get('line_no')} 行撞验证 → 刷新自恢复后重试"
                                    f"（{_verify_retries}/{VERIFY_AUTO_RETRY}）"
                                )
                                time.sleep(VERIFY_RETRY_GAP_S)
                                continue
                            if _step == "human" and self.wait_verification():
                                _verify_human_retries += 1
                                _verify_retries += 1
                                self._append_log(
                                    f"[batch] 第 {line.get('line_no')} 行刷新后仍无商品，人工验证已解除，重试"
                                    f"（人工第 {_verify_human_retries} 次）"
                                )
                                continue
                            with self._state_lock:
                                self._batch["stopped_reason"] = "刷新后仍无商品，需要人工完成淘宝验证，批量任务已暂停"
                            self._batch_line_patch(i, **res)
                            _stop_after = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        res = {
                            "line_no": line.get("line_no"),
                            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
                            "qty": line.get("qty"),
                            "attempts": [],
                            "status": "failed",
                            "message": f"未预期错误：{exc}",
                        }
                        break
                if _stop_after:
                    break
                self._batch_line_patch(i, **res)
                # 自适应降温：连续 ≥2 行「点了但没回执」→ 疑似被限流，立刻拉长
                # 冷却（60s×连续数，封顶 300s）。ok 正常则清零。跑批实测：从第 4
                # 行开始 uncertain 爬升，第 21 行就是 tmd 硬拦截 —— 早降温能保住
                # 整轮不被掐断。
                if res.get("status") == "uncertain":
                    _unc_streak += 1
                    if _unc_streak >= UNCERTAIN_STREAK_TRIGGER:
                        _cool = min(UNCERTAIN_COOL_MAX_S, UNCERTAIN_COOL_BASE_S * _unc_streak)
                        self._append_log(
                            f"[batch] 连续 {_unc_streak} 行无加购回执 → 疑似限流，降温 {_cool}s"
                        )
                        with self._state_lock:
                            self._batch["phase"] = (
                                f"疑似限流降温 {_cool}s（连续 {_unc_streak} 行无回执）"
                            )
                        _cd = _cool
                        while _cd > 0 and not self._batch.get("stopped_reason"):
                            time.sleep(min(5, _cd))
                            _cd -= 5
                else:
                    _unc_streak = 0
                # 加购接口连续变慢（>5 s）也是限流前兆：实测 1 s → 8.5 s 之后
                # 就开始「点了没回执」。提前降温，别等失败了才反应。
                _slow_streak = _slow_streak + 1 if (res.get("add_ms") or 0) > 5000 else 0
                if _slow_streak >= 2:
                    _cool = min(UNCERTAIN_COOL_MAX_S, UNCERTAIN_COOL_BASE_S * 2)
                    self._append_log(
                        f"[batch] 加购连续 {_slow_streak} 行超过 5s → 疑似限流前兆，降温 {_cool}s"
                    )
                    with self._state_lock:
                        self._batch["phase"] = f"加购变慢，预热降温 {_cool}s"
                    _cd = _cool
                    while _cd > 0 and not self._batch.get("stopped_reason"):
                        time.sleep(min(5, _cd))
                        _cd -= 5
                    _slow_streak = 0
                with self._state_lock:
                    self._batch["done"] = sum(
                        1
                        for r in self._batch["results"]
                        # skipped 也算「处理过了」：无搜索词的残缺行和用户中途
                        # 停止后剩下的行都是 skipped，不算进去的话进度条永远差几行。
                        if r["status"] in ("ok", "failed", "uncertain", "skipped")
                    )
                # 每行都落盘：随时暂停/关机都不丢进度，下次接着跑
                # 演练模式不写进度：否则假的「ok 无金额」会覆盖真实进度
                if not dry_run:
                    self._save_progress()
        finally:
            with self._state_lock:
                self._batch["running"] = False
                self._batch["index"] = -1
                self._batch["phase"] = "已结束"
                self._batch["finished_at"] = datetime.now().isoformat(timespec="seconds")
                if self._batch.get("stopped_reason"):
                    for r in self._batch["results"]:
                        if r["status"] == "pending":
                            r["status"] = "skipped"
                            r["message"] = self._batch["stopped_reason"]
            if not dry_run:
                self._save_progress()
            # 收尾对账（P0-1）：把 ok 行逐条跟购物车实际行比对。ok 判定再严，
            # 也可能有「网络侧 200 但淘宝侧没入车」的漏网行 —— 只有购物车里
            # 真找得到这个商品才算数。对不上就降级 uncertain，报告里看得见。
            # 演练模式不做对账：dry-run 本来就不点加购，购物车当然找不到，
            # 一比对就把演练的 ok 全降级成 uncertain（2026-11-09 实测踩到）。
            try:
                if not dry_run:
                    cv = self.send({"cmd": "cart-view"}, timeout=90)
                    cdata = cv.get("data") or {}
                    with self._state_lock:
                        self._batch["cart_count"] = cdata.get("cart_item_count")
                    _log(f"[batch] cart count after run: {cdata.get('cart_item_count')}")
                    self._reconcile_cart(cdata)
                    if not dry_run:
                        self._save_progress()  # 降级结果也要落盘，续跑时才不会误当 ok 跳过
                    # 兜底：一行没跑、所有「续跑跳过」的行又都没在购物车里找到，
                    # 说明进度与购物车严重错配（换 BOM / 购物车被清空过）。
                    # 留着这份进度，下次点「继续加购」还是整批空跑，直接清掉。
                    if should_clear_stale_progress(self._batch, ran):
                        clear_batch_progress()
                        msg = (
                            "本批没有实际执行任何行：上一批进度里的商品在购物车里都找不到，"
                            "已清空旧进度，请重新点「一键全部加购」从头跑。"
                        )
                        with self._state_lock:
                            self._batch["warning"] = msg
                        _log("[batch] 整批未执行且对账全降级 → 已清空错配进度")
            except Exception as exc:  # noqa: BLE001
                _log(f"[batch] cart count skipped: {exc}")
            if dry_run:
                _log("[batch] dry-run 结束：未加购，进度文件未改动")
            # 本轮速度复盘：单独存一份，跑 10 遍才有得比。
            try:
                self._save_run_metrics()
            except Exception as exc:  # noqa: BLE001
                _log(f"[batch] metrics skipped: {exc}")
            # 跑完（正常结束 / 被停止 / 撞验证暂停）统一清截图，别把磁盘堆满
            self.cleanup_screenshots()

    @staticmethod
    def _norm_title(t: str) -> str:
        """对账用标题归一：去空白/全半角差异，取前 16 位做「在不在车里」的模糊匹配。"""
        t = re.sub(r"\s+", "", str(t or "")).lower()
        return t[:16]

    def _cart_contains(self, title: str) -> tuple[bool | None, str]:
        """购物车里有没有这个商品。

        返回 (True/False/None, 原因)：None 表示明细读不到（降级/会话错），
        这时候**不能**下「没加上」的结论，只能保持 uncertain。
        """
        t = self._norm_title(title)
        if not t:
            return None, "标题为空"
        try:
            cv = self.send({"cmd": "cart-view"}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            return None, f"查购物车失败：{str(exc)[:40]}"
        data = cv.get("data") or {}
        if data.get("rows_degraded"):
            return None, "购物车明细降级，无法确认"
        for r in data.get("rows") or []:
            rt = self._norm_title(r.get("title"))
            if rt and (rt in t or t in rt):
                return True, ""
        return False, ""

    def _reconcile_cart(self, cart_data: dict) -> None:
        """批量收尾对账：ok 行的候选标题必须在购物车行里找得到，否则降级 uncertain。

        淘宝同款同 SKU 会合并数量，所以「在不在车」看行不看件数；行抽不出来
        （degraded）就不动结果，只记 warning —— 宁可虚高也不冤枉真加进去的行。
        """
        rows = cart_data.get("rows") or []
        degraded = bool(cart_data.get("rows_degraded"))
        cart_titles = [self._norm_title(r.get("title")) for r in rows]

        def _in_cart(title: str) -> bool:
            return any(t and (t in title or title in t) for t in cart_titles)

        checked, downgraded, uncertain_found = 0, [], []
        with self._state_lock:
            for r in self._batch.get("results") or []:
                # uncertain 行也查一遍：这类行是「点了加购但没抓到回执」，
                # 实际很可能已经进车（2026-11-09 实测 1/1 命中）。把证据补上，
                # 但**不改状态**——状态仍是 uncertain，只是不再让人白核对。
                if r.get("status") == "uncertain" and not degraded:
                    title = self._norm_title((r.get("candidate") or {}).get("title"))
                    if title and _in_cart(title):
                        r["cart_found"] = True
                        note = "收尾对账：购物车里找到该商品（加购实际已生效）"
                        if note not in (r.get("message") or ""):
                            r["message"] = f"{r.get('message') or ''}；{note}".strip("；")
                        uncertain_found.append(r.get("line_no"))
                    continue
                if r.get("status") != "ok":
                    continue
                title = self._norm_title((r.get("candidate") or {}).get("title"))
                if not title:
                    continue
                checked += 1
                if degraded:
                    continue  # 明细抽不出来，没法核对，不动结论
                if not _in_cart(title):
                    r["status"] = "uncertain"
                    r["message"] = "收尾对账：购物车里没找到该商品（可能加购未生效），请人工核对"
                    downgraded.append(r.get("line_no"))
            self._batch["reconcile"] = {
                "checked": checked,
                "downgraded": downgraded,
                "uncertain_found": uncertain_found,
                "cart_rows": len(rows),
                "cart_degraded": degraded,
                "cart_count": cart_data.get("cart_item_count"),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        if degraded:
            _log(f"[batch] reconcile skipped: cart rows degraded（{len(rows)} 行）")
        elif downgraded:
            _log(f"[batch] reconcile: {checked} ok 行里 {len(downgraded)} 行在购物车找不到，降级 uncertain: {downgraded}")
        else:
            _log(f"[batch] reconcile: {checked} ok 行全部在购物车核对到")
        if uncertain_found:
            _log(f"[batch] reconcile: {len(uncertain_found)} 个 uncertain 行确认已在购物车: {uncertain_found}")

    def _save_run_metrics(self):
        """把本轮每行耗时落盘到 runs/metrics-<时间>.json，供多轮对比。"""
        with self._state_lock:
            rows = []
            for r in self._batch.get("results") or []:
                if r.get("status") not in ("ok", "failed", "uncertain"):
                    continue
                rows.append(
                    {
                        "line_no": r.get("line_no"),
                        "status": r.get("status"),
                        "total_ms": r.get("total_ms"),
                        "search_ms": r.get("search_ms"),
                        "open_ms": r.get("open_ms"),
                        "select_ms": r.get("select_ms"),
                        "add_ms": r.get("add_ms"),
                        "attempts_n": len(r.get("attempts") or []),
                    }
                )
            meta = {
                "started_at": self._batch.get("started_at"),
                "finished_at": self._batch.get("finished_at"),
                "cart_count": self._batch.get("cart_count"),
                "max_candidates": self._batch.get("max_candidates"),
            }
        totals = [x["total_ms"] for x in rows if x.get("total_ms")]
        payload = dict(meta)
        payload["count"] = len(rows)
        payload["sum_total_ms"] = sum(totals)
        payload["mean_total_ms"] = int(sum(totals) / len(totals)) if totals else None
        payload["lines"] = rows
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = RUNS / f"metrics-{ts}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(
            f"[batch] metrics -> {path.name} "
            f"lines={len(rows)} mean={ (payload['mean_total_ms'] or 0) / 1000:.1f}s "
            f"total={(payload['sum_total_ms'] or 0) / 1000:.0f}s"
        )

    def _add_line_to_cart(self, line: dict, prefer_cheap: bool, max_candidates: int, dry_run: bool = False) -> dict:
        base = {
            "line_no": line.get("line_no"),
            "headline": (line.get("decision") or {}).get("headline") or line.get("mpn") or "",
            "qty": line.get("qty"),
            "attempts": [],
        }
        _t_search = time.monotonic()
        # require_scored：这条词有结果但候选全被门槛拒掉时，换下一条词再试
        search = self.search(line, require_scored=True)
        base["search_ms"] = int((time.monotonic() - _t_search) * 1000)
        base["search"] = {
            "ok": search.get("ok"),
            "query": search.get("query"),
            "items_count": search.get("items_count"),
        }
        if not search.get("ok"):
            base.update(status="failed", message=search.get("message") or "搜索没有结果", screenshot=search.get("screenshot"))
            return base

        cands, dropped = [], []
        for c in (search.get("candidates") or []):
            if (c.get("score") or 0) <= 0:
                continue
            why = _candidate_veto(line, c.get("title"))
            if why:
                # 标题层面的"料不对"：保险丝行拿连接器、针座行拿带线母头。
                # 这类候选打开也是白花 15 s，还可能被页面默认值蒙混过关。
                dropped.append({"index": c.get("index"), "title": c.get("title"), "reason": why})
                continue
            c["mpn_hit"] = _title_has_mpn(line, c.get("title"))
            cands.append(c)
        if dropped:
            base["rejected"] = dropped[:10]
        # 按「买够这一行要花多少钱」比，而不是单只价：
        # 只要 1 个电阻时，100 只/包 ¥3.5 的单只价最低，但要多花 3.4 还多拿 99 个。
        need = max(1, int(line.get("qty") or 1))
        for c in cands:
            pack, _src = _title_pack(c.get("title") or "")
            pack = pack if pack and pack > 1 else None
            c["pack_size"] = pack
            price = c.get("price")
            c["unit_price"] = round(price / pack, 6) if (pack and price is not None) else price
            if c.get("cost") is None and price is not None:
                if pack:
                    buy, _per, actual = engine.plan_qty(need, pack)
                    c["buy_qty"], c["actual_pcs"] = buy, actual
                    c["over_ratio"] = round(actual / need, 2)
                    c["cost"] = round(price * buy, 4)
                else:
                    c["buy_qty"] = c["actual_pcs"] = need
                    c["over_ratio"] = 1.0
                    c["cost"] = round(price * need, 4)
        if prefer_cheap:
            cands.sort(
                key=lambda c: (
                    not c.get("mpn_hit", True),
                    c.get("cost") is None,
                    c.get("cost") or 0.0,
                    c.get("over_ratio") or 1.0,
                )
            )
        else:
            cands.sort(key=lambda c: (not c.get("mpn_hit", True), -(c.get("score") or 0)))
        base["price_from"] = cands[0].get("price") if cands else None
        if not cands:
            base.update(status="failed", message="没有通过硬门槛的候选")
            return base

        last_message = ""
        # 被动件（电阻/电容）常遇到「一个链接只挂了部分阻值」，SKU 里根本没有 BOM 要的那个值，
        # 这时只能换店。阻值档位多的品类多试两家，别因为前 3 家都没这一档就整行失败。
        _limit = max(int(max_candidates), 5) if (line.get("category") == "passive") else max_candidates
        _try_list = cands[:_limit]
        for _ti, cand in enumerate(_try_list):
            attempt = {
                "index": cand.get("index"),
                "title": cand.get("title"),
                "price": cand.get("price"),
                "unit_price": cand.get("unit_price"),
                "pack_size": cand.get("pack_size"),
                "score": cand.get("score"),
                "ok": False,
                "message": "",
            }
            base["attempts"].append(attempt)
            _t_open = time.monotonic()
            opened = None
            # 打开商品时常有偶发故障（店铺页慢、图片卡住、导航超时）。
            # 以前一次超时就把整行判死，白等一轮还得人工补 —— 先原地重试一次，
            # 还不行再换下一个候选，最后才整行失败。
            for _retry in range(2):
                try:
                    opened = self.open_item(line, cand["index"])
                    break
                except SessionError as exc:
                    if exc.code == "VERIFICATION_REQUIRED":
                        raise
                    if _retry == 0 and _is_transient_error(exc):
                        base.setdefault("retries", []).append(
                            {"index": cand.get("index"), "reason": str(exc)[:100]}
                        )
                        time.sleep(1.5)
                        continue
                    attempt["message"] = f"打开商品失败：{str(exc)[:140]}"
                    last_message = attempt["message"]
                    break
                except Exception as exc:  # noqa: BLE001
                    attempt["message"] = f"打开商品异常：{str(exc)[:140]}"
                    last_message = attempt["message"]
                    break
            base["open_ms"] = (base.get("open_ms") or 0) + int((time.monotonic() - _t_open) * 1000)
            attempt["open_ms"] = int((time.monotonic() - _t_open) * 1000)
            if opened is None:
                continue
            unresolved = opened.get("unresolved") or []
            sels = opened.get("suggestions") or []
            no_sku = bool(opened.get("no_sku"))
            manual = [s for s in sels if not s.get("auto")]

            # 包规经常藏在 SKU 里（选了「100只」才知道），标题看不出来。
            # 打开后发现超量太狠（只要 1 个却要买 100 个），只要后面还有候选
            # 就先跳过这家 —— 这就是「换一家店买」。最后一个候选无论多超量都认，
            # 否则整行会买不到。
            _pack_now = opened.get("pack_size") or 0
            _need_now = max(1, int(line.get("qty") or 1))
            _actual_now = opened.get("actual_pcs") or (opened.get("suggested_qty") or 0) * _pack_now
            if _pack_now > 1 and _actual_now:
                _over_now = _actual_now / _need_now
                # 散料本来就按 50/100 只一包卖，BOM 要 1–4 只时超量必然 20–100 倍，
                # 以前 `over>=8` 恒真 → 每行连开 4 家、每家 15–20 s，最后第 5 家
                # 收的还是同样的 100 只/包（实测 #74 拒了 20/100/100/50 四家）。
                # 现在只有当后面**确实有更小包装、且不更贵**的候选时才换店。
                if _over_now >= 8 and _ti < len(_try_list) - 1:
                    _nxt = _try_list[_ti + 1]
                    _nxt_pack = _nxt.get("pack_size") or 0
                    _cur_cost = (opened.get("suggested_qty") or 1) * (opened.get("detail_price") or 0)
                    _nxt_cost = _nxt.get("cost")
                    _worth = bool(_nxt_pack and _nxt_pack < _pack_now)
                    if _worth and _nxt_cost is not None and _cur_cost:
                        # 小包装贵一点点可以接受（少浪费 80 只），贵太多就不值当
                        _worth = _nxt_cost <= max(_cur_cost * 1.3, _cur_cost + 1.0)
                    if _worth:
                        attempt["message"] = f"整包 {_pack_now} 只/份（超量 {_over_now:.0f} 倍），下一家包装更小，换"
                        last_message = attempt["message"]
                        continue

            if not no_sku and (unresolved or not sels or manual):
                why = "；".join(f"{u.get('label', '')}:{u.get('reason', '')}" for u in unresolved)
                if manual:
                    why = "；".join(f"{s.get('label')}={s.get('value')} 未确认" for s in manual) + ("；" + why if why else "")
                attempt["message"] = "SKU 无法确认 —— " + (why or "没有可用的 SKU 建议")
                last_message = attempt["message"]
                continue

            qty = opened.get("suggested_qty") or line.get("qty") or 1
            selections = [{"label": s["label"], "value": s["value"]} for s in sels]
            if dry_run:
                # 演练：选型全流程照跑（搜索/开页/SKU 判定/金额估算），只是不点「加入购物车」。
                # 用于多轮回归测速度，不给购物车堆重复商品、不污染真实进度。
                _p = opened.get("detail_price")
                _ps = opened.get("pack_size") or 1
                _pcs = opened.get("actual_pcs") or (opened.get("suggested_qty") or 1) * _ps
                attempt["ok"] = True
                attempt["dry"] = True
                base.update(
                    status="ok",
                    message="演练：未点加购（仅校验选型）",
                    candidate={
                        "index": cand.get("index"),
                        "title": cand.get("title"),
                        "price": cand.get("price"),
                        "url": cand.get("url"),
                        "score": cand.get("score"),
                    },
                    sku=selections,
                    qty=qty,
                    final_price=_p,
                    cost=(_p * (opened.get("suggested_qty") or 1)) if _p else None,
                    pack_size=opened.get("pack_size"),
                    actual_pcs=_pcs,
                    dry_run=True,
                )
                return base
            # 加购最多尝试两次：第一次「点了没回执」时，先查购物车——
            #   · 车里有 → 其实成功了（记 ok，证据=购物车）
            #   · 车里没有 → 再点一次是安全的（不会重复下单）
            # 这是把「偶尔加不上」从 uncertain 变成「自动补一次」的关键，
            # 2026-11-09 加入。购物车明细读不到（降级）时保持原样，不乱试。
            added = None
            for _try_add in range(2):
                added = self.add_to_cart(
                    line, cand["index"], selections, qty, allow_empty=no_sku,
                    auto_qty=True, pack_hint=opened.get("pack_size"),
                    price_hint=opened.get("detail_price"),
                )
                if added.get("ok"):
                    break
                if added.get("code") != "CART_UNCONFIRMED":
                    break
                _found, _why = self._cart_contains(cand.get("title"))
                if _found is True:
                    added = dict(added)
                    added.update(
                        ok=True,
                        cart_verified_via="cart_check",
                        message="加购无回执，但购物车里确认到该商品（已生效）",
                    )
                    break
                if _found is False and _try_add == 0:
                    attempt["message"] = "加购无回执；购物车确认没有该商品 → 重试一次"
                    _log(f"[batch] line #{line.get('line_no')} 加购无回执且车里没有，重试一次")
                    continue
                if _why:
                    attempt["message"] = f"{attempt.get('message') or ''}（{_why}）"
                break
            qty = added.get("qty") or qty
            attempt["message"] = added.get("message") or ("已加入购物车" if added.get("ok") else "加购失败")
            if not added.get("ok"):
                last_message = attempt["message"]
                if added.get("code") == "CART_UNCONFIRMED":
                    # 已经点过「加入购物车」，再换候选会重复加购 —— 这行到此为止。
                    # 但不要中断整批：淘宝购物车页常被风控挡成 Oops，确认不到不算失败。
                    base.update(
                        status="uncertain",
                        message="已点击加购但页面没给确认信号（淘宝购物车页可能被风控挡），请到购物车核对",
                        candidate={
                            "index": cand.get("index"),
                            "title": cand.get("title"),
                            "price": cand.get("price"),
                            "url": cand.get("url"),
                            "score": cand.get("score"),
                        },
                        sku=sels,
                        qty=qty,
                        screenshot=added.get("screenshot"),
                    )
                    break
                if added.get("code") in ("COST_TOO_HIGH", "OVERBUY_TOO_MUCH"):
                    # 这家买够这一行太贵：还有候选就换下一家，最后一个候选交人工判断
                    attempt["cost"] = added.get("est_cost")
                    attempt["pack_size"] = added.get("pack_size")
                    if _ti < len(_try_list) - 1:
                        attempt["message"] = (added.get("message") or "") + "，换下一家"
                        last_message = attempt["message"]
                        continue
                    base.update(
                        status="uncertain",
                        message=(added.get("message") or "金额超过单行上限") + "，可到页面人工确认是否购买",
                        candidate={
                            "index": cand.get("index"),
                            "title": cand.get("title"),
                            "price": cand.get("price"),
                            "url": cand.get("url"),
                            "score": cand.get("score"),
                        },
                        sku=sels,
                        qty=qty,
                        final_price=added.get("final_price"),
                        cost=added.get("est_cost"),
                        pack_size=added.get("pack_size"),
                        actual_pcs=added.get("actual_pcs"),
                        screenshot=added.get("screenshot"),
                    )
                    return base
                continue

            attempt["ok"] = True
            # 金额：final_price 是「选完 SKU 后页面显示的每份单价」，cost 由 add_to_cart 算好
            _final = added.get("final_price")
            _sent = added.get("qty_sent") or qty
            _cost = added.get("cost")
            base.update(
                status="ok",
                message="已加入购物车",
                candidate={
                    "index": cand.get("index"),
                    "title": cand.get("title"),
                    "price": cand.get("price"),
                    "url": cand.get("url"),
                    "score": cand.get("score"),
                },
                detail_price=opened.get("detail_price"),
                qty=qty,
                qty_sent=_sent,
                final_price=_final,
                cost=_cost,
                pack_size=added.get("pack_size") or opened.get("pack_size"),
                pack_source=added.get("pack_source") or opened.get("pack_source"),
                actual_pcs=added.get("actual_pcs") or opened.get("actual_pcs"),
                unit_price=added.get("unit_price") or opened.get("unit_price"),
                bom_qty=opened.get("bom_qty"),
                sku=sels,
                screenshot=added.get("screenshot"),
                cart_verified_via=added.get("cart_verified_via"),
                select_ms=added.get("select_ms"),
                add_ms=added.get("add_ms"),
            )
            return base

        if base.get("status") != "uncertain":
            base.update(status="failed", message=last_message or "候选都无法加购")
        return base


RUNNER = SessionRunner()


# --------------------------------------------------------------------------
# SKU 匹配
# --------------------------------------------------------------------------

_PKG_PREFIX = re.compile(r"^\s*(0201|0402|0603|0805|1206|1210|1812|2512)\s*")
_RISK_WORDS = ("散新", "拆机", "翻新", "打磨", "改字", "白字", "洗板", "二手", "返修")
# 这些 SKU 值根本不能直接下单（要询价/改价），命中就直接判定该候选不可买
_UNBUYABLE = ("询价", "联系客服", "拍下改价", "议价")


# 整包报价的包装数解析统一放在 engine（比价也要用同一套规则），这里只做别名
_detect_pack = engine.detect_pack
_pack_from_texts = engine.pack_from_texts
_title_pack = engine.title_pack
_pack_size = engine.pack_size
_plan_qty = engine.plan_qty

# 单行金额上限：只有选完 SKU 才知道「一份」到底多少钱。
# 阻容感一行正常几毛到几十元，200 元已经明显不对（典型是整盘/整卷报价）；
# 芯片/功率件单价本来高，默认给到 5000，等于不拦。
_DEFAULT_LINE_CAP = {"passive": 200.0, "connector": 300.0, "other": 200.0, "default": 5000.0}


def _line_cost_cap(category) -> float:
    cat = str(category or "other")
    cfg = load_settings().get("max_line_cost") or {}
    val = cfg.get(cat, cfg.get("default"))
    try:
        return float(val)
    except (TypeError, ValueError):
        return _DEFAULT_LINE_CAP.get(cat, _DEFAULT_LINE_CAP["default"])




def _title_with_spec(line: dict, item: dict) -> str:
    """标题第一行里没有型号、但商品卡的规格行里有 —— 补进标题再评分。

    淘宝搜索结果把型号印在标题下面的规格行（「4.2E104-BT40UART」），
    title 只取第一行会丢掉型号，H02「标题未出现完整型号」直接把整行判死
    （#107 E104-BT40 就是这么连续 4 条搜索词全灭的）。
    """
    title = (item.get("title") or "").strip()
    mpn = (line.get("mpn") or "").strip()
    if not mpn or not title:
        return title
    if _alnum(mpn) and _alnum(mpn) in _alnum(title):
        return title
    spec = item.get("spec_text") or ""
    if _alnum(mpn) and _alnum(mpn) in _alnum(spec):
        return f"{title} {mpn}"
    return title


def _is_transient_error(exc) -> bool:
    """打开商品失败是不是「偶发」的（页面慢/网络抖/导航超时）。

    偶发错误值得原地重试一次；验证拦截、商品下架这类重试也没用，直接换候选。
    """
    code = str(getattr(exc, "code", "") or "")
    if code in ("VERIFICATION_REQUIRED", "NO_QUERY", "BATCH_RUNNING", "BATCH_EMPTY"):
        return False
    msg = str(exc or "")
    if code in ("OPEN_FAILED", "OPEN_TIMEOUT", "SEARCH_NO_RESPONSE"):
        return True
    return any(
        k in msg
        for k in ("Timeout", "timeout", "超时", "net::ERR", "Target closed", "Navigation",
                  "ECONNRESET", "断开的连接", "ERR_CONNECTION")
    )


EXPORT_COLUMNS = (
    "行号",
    "状态",
    "需求",
    "买到的商品",
    "SKU",
    "数量",
    "实付金额",
    "确认方式",
    "耗时(s)",
    "备注",
)


def batch_export_rows(results: list[dict]) -> list[list[str]]:
    """把批量逐行结果整理成可直接落 CSV 的二维表（首行是表头）。

    跑完一轮要人工核对 100+ 行时，屏幕上滚太痛苦 —— 导出后可以用 Excel
    跟 BOM 并排 diff。金额/耗时都转成字符串，避免 csv 写入出现类型差异。
    """
    rows: list[list[str]] = [list(EXPORT_COLUMNS)]
    for r in results or []:
        if r.get("status") not in ("ok", "failed", "uncertain", "skipped"):
            continue
        sku = " / ".join(
            f"{s.get('label')}={s.get('value')}" for s in (r.get("sku") or []) if s.get("value")
        )
        cost = r.get("cost")
        rows.append(
            [
                str(r.get("line_no") or ""),
                str(r.get("status") or ""),
                str(r.get("headline") or ""),
                str((r.get("candidate") or {}).get("title") or ""),
                sku,
                str(r.get("qty") or ""),
                "" if cost is None else f"{float(cost):.2f}",
                str(r.get("cart_verified_via") or ""),
                f"{(r.get('total_ms') or 0) / 1000:.1f}",
                " ".join(str(r.get("message") or "").split()),
            ]
        )
    return rows


def _strip_pkg(text: str) -> str:
    return _PKG_PREFIX.sub("", str(text or ""))


def _sku_resistance(text: str, allow_bare: bool = False):
    t = _strip_pkg(text).replace(" ", "")
    if re.search(r"[pnuµμ]f", t, re.I):
        return None
    # 4K7 / 1K2：夹点写法，代表 4.7k / 1.2k
    m = re.match(r"^(\d+)([kKmM])(\d)(?!\d)", t)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * {"k": 1e3, "K": 1e3, "M": 1e6}[m.group(2)]
    # R24 = 0.24Ω
    m = re.match(r"^R(\d{1,2})(?!\d)", t)
    if m:
        return float("0." + m.group(1))
    # 1R2 = 1.2Ω
    m = re.match(r"^(\d+)R(\d)(?!\d)", t)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # 0R / 2.2R / 100R / 24.9R：R 当 Ω 用，这是卖家最常见的写法，
    # 以前只认 Ω，导致「0603 2.2R 0.1% (20个)」这种值解析成 None ——
    # 阻值没参与打分，2.2R 靠封装 +15 就混过了 0Ω/33Ω/100Ω 这些行。
    m = re.match(r"^(\d+(?:\.\d+)?)R(?![a-zA-Z])", t)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmM]?)(Ω|欧)?", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    has_ohm = bool(m.group(3))
    rest = t[m.end():]
    if unit:
        mult = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3}[unit]
        return val * mult
    if not has_ohm:
        if not allow_bare:
            return None
        if rest and not re.match(r"^(±?\d+(?:\.\d+)?%)", rest):
            return None
    return val


def _sku_capacitance(text: str):
    t = _strip_pkg(text).replace(" ", "")
    # 容值不一定在开头：「25V220UF 6*8」「0402 100NF」都要能取到
    m = re.search(r"(?<![0-9.])(\d+(?:\.\d+)?)(p|n|u|µ|μ|m)[fF](?![a-z])", t, re.I)
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}[m.group(2).lower()]


def _close(a, b, rel=0.005):
    if b == 0:
        return abs(a) < 1e-12
    return abs(a - b) / abs(b) <= rel


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _sku_inductance(text: str):
    t = _strip_pkg(text).replace(" ", "")
    m = re.search(r"(?<![0-9.])(\d+(?:\.\d+)?)(n|u|µ|μ|m)[hH](?![a-z])", t, re.I)
    if not m:
        return None
    return float(m.group(1)) * {"n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}[m.group(2).lower()]


_PACK_ONLY = re.compile(r"^[^\dA-Za-z]{0,6}\s*\d+\s*(?:个|只|片|支|条|颗|件|pcs)\s*(?:推荐|热卖|包邮)?$", re.I)


def _is_pack_only(text: str) -> bool:
    """True for SKU values that only differ by package quantity: 「10个推荐」「100个」."""
    return bool(_PACK_ONLY.match(str(text or "").strip()))


def _pack_in_text(text: str) -> int | None:
    """'15UH(印字150) 10个' -> 10 (pieces per package)."""
    n, _ = _detect_pack(text, explicit_only=False, allow_generic=True)
    return n


def _sku_tolerance(text: str):
    m = re.search(r"(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*%", str(text or ""))
    return float(m.group(1)) if m else None


def _sku_has_spec(line: dict, text: str) -> bool:
    """这段 SKU 文本里有没有能和 BOM 参数对上号的东西。

    用来认出「非规格组」：像「50个 / 100个 / 500个」这种只在卖包装数量的组，
    以前会被拿去做电压/容值匹配（页面默认「50个」→ 判成电压不符 → 整行卡死）。
    """
    p = line.get("params") or {}
    t = str(text or "")
    if not t:
        return False
    if any(w in t for w in _UNBUYABLE):
        return True
    if "resistance_ohm" in p and _sku_resistance(t) is not None:
        return True
    if "capacitance_f" in p and _sku_capacitance(t) is not None:
        return True
    if "inductance_h" in p and _sku_inductance(t) is not None:
        return True
    if "tolerance_pct" in p and _sku_tolerance(t) is not None:
        return True
    if "voltage_v" in p and re.search(r"\d+(?:\.\d+)?\s*[vV]\b", t):
        return True
    if "power_w" in p and re.search(r"\d+(?:\.\d+)?\s*[wW]\b", t):
        return True
    if "pins" in p and re.search(r"(?<![\d.])\d{1,2}\s*(?:p|pin|针)(?![0-9a-zA-Z])", t, re.I):
        return True
    if "current_a" in p and engine.title_amps(t):
        return True
    if "pitch_mm" in p and (
        re.search(r"\d+(?:\.\d+)?\s*mm", t, re.I)
        or re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)\d\.\d{1,2}", t, re.I)
    ):
        return True
    mpn_key = _alnum(line.get("mpn"))
    if len(mpn_key) >= 5 and mpn_key in _alnum(t):
        return True
    full_pkg = line.get("package") or ""
    if full_pkg:
        if engine.connector_series(full_pkg, line.get("desc"), line.get("mpn")):
            variants = engine.connector_pkg_variants(full_pkg)
        else:
            variants = engine.pkg_variants(full_pkg.split(" ")[0])
        nt = t.lower()
        if any(v.lower() in nt for v in variants):
            return True
    return False


def _tol_verdict(want: float, got: float) -> tuple[float, str]:
    """容差判定：返回 (分数增减, 给人工看的备注)。

    以前只有「≤要求 +15 / ≤20% +10 / 否则 −40」三档，于是 BOM 要 0.1%、
    实买 ±1% 也照样 +10 分被加购（#70）。这里改成按**倍数**分档：
    宽容差只降权不否决，备注里写清宽了多少倍，报告里能直接看出来。
    """
    if got <= want + 1e-9:
        return 15.0, ""
    ratio = got / max(want, 1e-9)
    if ratio <= 1.5:
        return 8.0, ""
    if ratio <= TOLERANCE_DEMOTE_RATIO + 1e-9:
        return 0.0, f"容差 {got:g}% 略宽于要求 {want:g}%"
    if ratio <= 10:
        return -40.0, f"容差 {got:g}% 宽于要求 {want:g}%（{ratio:.0f}×）"
    return -70.0, f"容差 {got:g}% 远宽于要求 {want:g}%（{ratio:.0f}×）"


def _score_sku_value(line: dict, label: str, text: str) -> float:
    p = line.get("params") or {}
    lab = label or ""
    score = 0.0
    if any(w in str(text) for w in _RISK_WORDS):
        score -= 80
    if any(w in str(text) for w in _UNBUYABLE):
        score -= 150

    # 型号出现在 SKU 文本里是最强信号（连接器/芯片/功率管几乎只按型号区分）
    mpn_key = _alnum(line.get("mpn"))
    if len(mpn_key) >= 5 and mpn_key in _alnum(text):
        score += 60

    if "resistance_ohm" in p:
        r = _sku_resistance(text, allow_bare=("阻" in lab))
        if r is not None:
            score += 50 if _close(r, p["resistance_ohm"]) else -100
    if "capacitance_f" in p:
        c = _sku_capacitance(text)
        if c is not None:
            score += 50 if _close(c, p["capacitance_f"], 0.02) else -100
    if "inductance_h" in p:
        ind = _sku_inductance(text)
        if ind is not None:
            score += 50 if _close(ind, p["inductance_h"], 0.05) else -100
    if "tolerance_pct" in p:
        tol = _sku_tolerance(text)
        if tol is not None:
            delta, _note = _tol_verdict(float(p["tolerance_pct"]), tol)
            score += delta
        else:
            # 没写容差 ≠ 容差合格。不否决（详情页常把精度放在标题里），
            # 但排在同一档里写清楚了精度的那几个后面。
            score -= 5
    if "voltage_v" in p:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[vV]\b", str(text))
        if m:
            score += 15 if float(m.group(1)) >= p["voltage_v"] - 1e-9 else -30
    if "power_w" in p:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[wW]\b", str(text))
        if m:
            score += 10 if float(m.group(1)) >= p["power_w"] * 0.999 else -20
    if "current_a" in p:
        # 保险丝：额定电流必须同一个值。页面默认常常是 0.25A，8A 的位子买错就是整批返工。
        amps = engine.title_amps(text)
        if amps:
            ok = any(abs(a - p["current_a"]) <= max(0.005, p["current_a"] * 0.01) for a in amps)
            score += 50 if ok else -100
    full_pkg = line.get("package") or ""
    # 连接器系列：XH / 排针 / 端子台。库里叫 XH2.50mm-4P，标题写 XH2.54-4P，
    # 直接比字符串必然不中，要换成系列写法再比。
    conn_series = engine.connector_series(full_pkg, line.get("desc"), line.get("mpn"))
    pkg = full_pkg.split(" ")[0]
    if conn_series:
        variants = engine.connector_pkg_variants(full_pkg)
    else:
        variants = engine.pkg_variants(pkg) if pkg else []
    if variants:
        nt = str(text).lower()
        if any(v.lower() in nt for v in variants):
            score += 15
        else:
            others = [v for v in ("0201", "0402", "0603", "0805", "1206", "1210", "2512") if v not in variants and v in nt]
            if others:
                score -= 40
    if "channel" in p:
        if re.search(rf"{p['channel']}\s*(?:沟道|channel|mos)", str(text), re.I):
            score += 15
        elif re.search(r"[NP]\s*(?:沟道|channel|mos)", str(text), re.I):
            score -= 40
    if "pins" in p:
        txt = str(text)
        m = re.search(r"(\d+)\s*(?:p\b|pin|针)", txt, re.I)
        if not m:
            # JST/排针类 SKU 几乎都写成 XH2.54-3PWT / PH2.0-4AW / DC3-10P贴片，
            # 第一个正则要求 "3P" 后面是词边界，"3PWT" 就整组漏掉 → 必须补一条宽松的。
            m = re.search(r"(?<![\d.])(\d{1,2})\s*[pP](?=[A-Z]|[-_(\s]|$|[\u4e00-\u9fff])", txt)
        if not m:
            # XH 系列的 4AW / 3A（A=带扣，W=弯针），针数就在 A 前面
            m = re.search(r"(?<![\d.])(\d{1,2})\s*[aA][wW]?(?=[-_)\s]|$|[\u4e00-\u9fff])", txt)
        if m:
            n = int(m.group(1))
            want = int(p["pins"])
            if n == want:
                score += 15
            elif conn_series in ("排针", "排母") and n > want:
                # 排针整条卖（40P 一条自己剪），针数多于需求可用
                score += 5
            else:
                score -= 40
    # 单排 / 双排 是不同料：SKU 里常见 “1*2p / 2*2p” 写法
    raw_params = str((line.get("row") or {}).get("params") or "")
    params_text = raw_params + " " + str(line.get("params_text") or "") + " " + str(line.get("desc") or "")
    rows = re.search(r"([12])\s*[*xX×]\s*(\d+)", str(text))
    if rows:
        if "单排" in params_text and rows.group(1) == "2":
            score -= 45
        if "双排" in params_text and rows.group(1) == "1":
            score -= 45
        if "单排" in params_text and rows.group(1) == "1":
            score += 15
    if "pitch_mm" in p:
        txt = str(text)
        m = re.search(r"(\d+(?:\.\d+)?)\s*mm", txt, re.I)
        if not m:
            # XH2.54 / PH2.0 这种把间距写在系列名后面的写法
            m = re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)(\d\.\d{1,2})", txt, re.I)
        # 「单排排针2.54 2P」这种不带 mm 也不带系列前缀的写法，再兜一层
        pitch_val = float(m.group(1)) if m else engine._parse_pitch_loose(txt)
        if pitch_val is not None:
            # 2.50 与 2.54 是同一个 XH 系列，必须视为同档
            score += 15 if engine.pitch_match(pitch_val, p["pitch_mm"]) else -40
    return score


_FORM_HARNESS_WORDS = ("带线", "线束", "渡锡线", "母头带线", "端子线", "杜邦线", "连接线")
_FORM_HEADER_WORDS = ("针座", "直针", "弯针", "排针", "插座", "母座", "端子台", "卧贴", "立贴", "编带")


def _rows_of(text: str):
    """SKU/标题里的排数：1*40P / 2*2p / 三排 / 单排。认不出返回 None。"""
    t = str(text or "")
    m = re.search(r"(?<![\d.])([1-4])\s*[*xX×]\s*(\d+)", t)
    if m:
        return int(m.group(1))
    for word, n in (("单排", 1), ("双排", 2), ("三排", 3), ("四排", 4)):
        if word in t:
            return n
    return None


def _line_blob(line: dict) -> str:
    """BOM 这一行的全部文本（含原始行）。

    engine 会把「排针 2.54mm 2P 单排」归一成 package=「排针」、把「SMD 保险丝」
    归一成「SMD」，所以判断形态/品类时必须把原始行列进来，否则「单排」「保险丝」
    这些关键信息在归一化时就丢了。
    """
    row = line.get("row") or {}
    parts = [
        line.get("package"), line.get("desc"), line.get("params_text"), line.get("mpn"), line.get("category"),
        row.get("package"), row.get("desc"), row.get("params"), row.get("mpn"),
    ]
    return " ".join(str(x or "") for x in parts)


def _line_rows(line: dict):
    blob = _line_blob(line)
    for word, n in (("单排", 1), ("双排", 2), ("三排", 3), ("四排", 4)):
        if word in blob:
            return n
    return None


def _conn_kind(line: dict) -> str | None:
    return engine.connector_series(line.get("package") or "", line.get("desc"), line.get("mpn"))


def _sku_veto(line: dict, label: str, text: str) -> str | None:
    """硬属性不一致 → 这个 SKU 值直接出局，不允许靠"总分高"混过。

    以前冲突只扣分（耐压 −30、针数 −40、容值 −100），而一项命中能加 +50，
    于是一个 6.3V 的 47µF 电容（要 16V）能打出 +35 分被自动加购。
    这里把"能不能用"和"值不值"分开：先判能不能用。
    """
    p = line.get("params") or {}
    t = str(text or "")
    if not t:
        return None
    if any(w in t for w in _UNBUYABLE):
        return "要询价/改价，不能直接下单"
    if "resistance_ohm" in p:
        r = _sku_resistance(t, allow_bare=True)
        if r is not None and not _close(r, p["resistance_ohm"]):
            return f"阻值 {_fmt_val(r)}Ω ≠ 要求 {_fmt_val(p['resistance_ohm'])}Ω"
    if "capacitance_f" in p:
        c = _sku_capacitance(t)
        if c is not None and not _close(c, p["capacitance_f"], 0.02):
            return f"容值 {_fmt_farad(c)}F ≠ 要求 {_fmt_farad(p['capacitance_f'])}F"
    if "inductance_h" in p:
        ind = _sku_inductance(t)
        if ind is not None and not _close(ind, p["inductance_h"], 0.05):
            return f"感值 {_fmt_farad(ind)}H ≠ 要求 {_fmt_farad(p['inductance_h'])}H"
    # 精密档容差：BOM 要 0.1%/0.05%，选项是 ±1% —— 这是另一个等级的料，
    # 装上分压/采样就偏，宁可 unresolved 也不买（#70）。普通档（1%/5%）只降权。
    # 「没标注」的情况不能在这里拦：容值和容差经常是两个独立 SKU 组
    # （容值组只写 24.9pF），这里一刀切会把容值组全 veto 掉 —— 由
    # suggest_sku 在所有组扫完后统一判（见 _precision 检查）。
    if "tolerance_pct" in p and TOLERANCE_PRECISION_VETO:
        want_tol = float(p["tolerance_pct"])
        got_tol = _sku_tolerance(t)
        if (
            got_tol is not None
            and want_tol <= TOLERANCE_PRECISION_PCT + 1e-9
            and got_tol > want_tol * TOLERANCE_DEMOTE_RATIO + 1e-9
        ):
            return f"容差 {got_tol:g}% 宽于要求 {want_tol:g}%（精密档不能用）"
    # 介质（P0-4）：要 C0G 买了 X7R 是另一个等级的料 —— SKU 值里明确写了
    # 别的介质就拦（#8 的错买：1nF 50V C0G 5% 买成 1nF_±10%_50V_X7R）。
    # 没写介质不算冲突，由 suggest_sku 挂「未标注」备注让人工留意。
    _dv = _dielectric_verdict(line, t)
    if _dv:
        return _dv
    if "voltage_v" in p:
        volts = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", t)]
        if volts and max(volts) < p["voltage_v"] - 1e-9:
            return f"耐压 {max(volts):g}V < 要求 {p['voltage_v']:g}V"
    if "current_a" in p:
        amps = engine.title_amps(t)
        if amps and not any(abs(a - p["current_a"]) <= max(0.005, p["current_a"] * 0.01) for a in amps):
            return f"电流 {'/'.join(f'{a:g}' for a in amps)}A ≠ 要求 {p['current_a']:g}A"
    if "pins" in p:
        want = int(p["pins"])
        n = _sku_pins(t)
        if n is not None and n != want:
            kind = _conn_kind(line)
            rows = _rows_of(t) or _line_rows(line) or 1
            # 排针整条卖（1*40P 剪成 3P）是可接受的；但 3*40P 三排不能顶 1*3P
            strip_ok = kind in ("排针", "排母") and n > want and rows == 1
            if not strip_ok:
                return f"针数 {n} ≠ 要求 {want}"
    # 单排/双排/三排：2*2p 是双排 4 针，顶不了"2P 单排"
    want_rows = _line_rows(line)
    got_rows = _rows_of(t)
    if want_rows and got_rows and got_rows != want_rows:
        return f"排数 {got_rows} ≠ 要求 {want_rows}"
    if "pitch_mm" in p:
        pv = _sku_pitch(t)
        if pv is not None and not engine.pitch_match(pv, p["pitch_mm"]):
            return f"间距 {pv:g}mm ≠ 要求 {p['pitch_mm']:g}mm"
    if "channel" in p:
        ch = str(p["channel"]).upper()
        if re.search(r"[NP]", ch):
            if ch == "N" and re.search(r"[Pp]\s*(?:沟道|channel|mos)", t):
                return "沟道不符（要 N 沟道）"
            if ch == "P" and re.search(r"[Nn]\s*(?:沟道|channel|mos)", t):
                return "沟道不符（要 P 沟道）"
    # 连接器形态：BOM 要板端针座，SKU 却是带线/母头 —— 焊不上板
    blob = _line_blob(line)
    if _conn_kind(line) or "排针" in blob or "端子台" in blob:
        if not any(w in blob for w in ("带线", "线束", "线材", "端子线")):
            if any(w in t for w in _FORM_HARNESS_WORDS):
                return "形态不符：要板端针座，这是带线/母头"
    # 尺寸/耐压：电解电容、电感的实体尺寸只有 SKU 选项里才写得清
    # （标题「10/16/25/35V」多档，实际买哪档看 SKU 文本）
    _sv = _size_verdict(line, t)
    if _sv:
        return _sv
    # 连接器系列：标题层面看不出来的（标题只写「2.54mm 4P」），SKU 里往往写着型号
    want_fam = _conn_family(blob)
    got_fam = _conn_family(t)
    if want_fam and got_fam and want_fam != got_fam:
        return f"连接器系列不符：BOM 是 {want_fam}，SKU 是 {got_fam}"
    return None


def _sku_pins(text: str):
    txt = str(text or "")
    m = re.search(r"(\d+)\s*(?:p\b|pin|针)", txt, re.I) \
        or re.search(r"(?<![\d.])(\d{1,2})\s*[pP](?=[A-Z]|[-_(\s]|$|[\u4e00-\u9fff])", txt)
    if not m:
        m = re.search(r"(?<![\d.])(\d{1,2})\s*[aA][wW]?(?=[-_)\s]|$|[\u4e00-\u9fff])", txt)
    return int(m.group(1)) if m else None


def _sku_pitch(text: str):
    txt = str(text or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", txt, re.I) \
        or re.search(r"(?:XH|PH|ZH|SH|EH|GH|KF|MX|HY|SM)(\d\.\d{1,2})", txt, re.I)
    if m:
        return float(m.group(1))
    return engine._parse_pitch_loose(txt)


def _fmt_val(v):
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    if v >= 1 or v == 0:
        return f"{v:g}"
    return f"{v:g}"


def _fmt_farad(v):
    for unit, scale in (("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= scale * 0.999:
            return f"{v / scale:g}{unit}"
    return f"{v:g}"


_DIELECTRIC_C1 = ("C0G", "NP0", "COG", "NPO")
_DIELECTRIC_C2 = ("X7R", "X5R", "X6S", "X7S", "X8R", "Y5V", "Y5U", "Z5U", "X7T")


def _dielectric_of(text: str) -> str | None:
    """从标题/SKU 文本里认介质。NP0/COG/NPO 归一成 C0G。

    Class II 的 X 系列允许嵌在 MPN 里认（CC0603**KRX7R**9BB103 的 X7R 就在
    料号中间，是真实介质）；C0G/NP0 必须有边界，防误配。
    """
    t = str(text or "").upper()
    for code in _DIELECTRIC_C1:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", t):
            return "C0G"
    for code in _DIELECTRIC_C2:
        if code in t:
            return code
    return None


def _dielectric_verdict(line: dict, text: str) -> str | None:
    """BOM 要求介质、候选明确写了另一种介质 → 拦。没写不算（很多标题不写），只降权提示。"""
    want = (line.get("params") or {}).get("dielectric")
    if not want:
        return None
    got = _dielectric_of(text)
    if got is None:
        return None
    if (want == "C0G") != (got == "C0G"):
        return f"介质不符：要 {want}，这是 {got}（温漂差一个量级，不能互顶）"
    return None


_CONN_FAMILY_PATTERNS = (
    ("XH", (r"\bxh\b", r"-xh-", r"xh\s*-?\s*2\.54", r"jst[_\- ]?xh", r"b\d+b-xh")),
    ("PH", (r"phc2\.0", r"-ph-", r"jst[_\- ]?ph\b")),
    ("ZH", (r"zh1\.5", r"-zh-", r"jst[_\- ]?zh\b")),
    ("SH", (r"sh1\.0", r"-sh-", r"jst[_\- ]?sh\b")),
    ("EH", (r"eh2\.5", r"-eh-", r"jst[_\- ]?eh\b")),
    ("GH", (r"gh1\.25", r"-gh-", r"jst[_\- ]?gh\b")),
    ("KF2510", (r"kf\s*-?\s*2510",)),
    ("KF128/2EDG", (r"kf\s*-?\s*128", r"\b2edg", r"2edgv[ck]")),
    ("VH", (r"vh3\.96", r"-vh-")),
    ("MX1.25", (r"mx\s*-?\s*1\.25", r"1\.25\s*mm\s*卧贴")),
)
# 同族可直接互换的写法（不同族一律否决：KF2510 插不进 XH 座）
_CONN_FAMILY_ALIAS = {"KF128/2EDG": {"KF128/2EDG"}}


def _conn_family(text: str) -> str:
    """从标题/封装/型号里认出连接器家族（XH / PH / KF2510 / KF128-2EDG …）。"""
    t = str(text or "").lower()
    for name, pats in _CONN_FAMILY_PATTERNS:
        for pat in pats:
            if re.search(pat, t):
                return name
    return ""


def _size_pairs(text: str) -> list[tuple[float, float]]:
    """抠「长×宽」尺寸：13*20mm / 13.5x12.6 / L7.1-W6.6 / BD6.3。"""
    t = str(text or "")
    out: list[tuple[float, float]] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[x*×]\s*(\d+(?:\.\d+)?)", t, re.I):
        out.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"L\s*(\d+(?:\.\d+)?)\s*[-_ ]\s*W\s*(\d+(?:\.\d+)?)", t, re.I):
        out.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"BD\s*(\d+(?:\.\d+)?)", t, re.I):
        d = float(m.group(1))
        out.append((d, d))
    return out


def _volts(text: str) -> float | None:
    """文本里第一个耐压值（25V / 80v），没有就 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", str(text or ""))
    return float(m.group(1)) if m else None


def _size_verdict(line: dict, title: str) -> str | None:
    """电解电容 / 电感的实体尺寸、耐压对不上 → 否决。

    2026-11-10 真机踩到：BOM 要 25V 220µF 6×8mm，买成 80V 220µF 13×20mm；
    15µH 要 7.1×6.6 焊盘，买成 13.5×12.6mm 15A —— 容值/感值全对，但**装不上去**。
    只看电气参数的匹配永远发现不了这种错。
    """
    blob = _line_blob(line)
    is_elec = ("电解" in blob) or ("CAP-TH_BD" in blob.upper())
    is_ind = ("电感" in blob) or re.search(r"IND-|_L\d", blob, re.I) is not None
    if not (is_elec or is_ind):
        return None
    req_sizes = _size_pairs(blob)
    cand_sizes = _size_pairs(title)
    if req_sizes and cand_sizes:
        req_max = max(max(a, b) for a, b in req_sizes)
        cand_max = max(max(a, b) for a, b in cand_sizes)
        if cand_max > req_max * 1.35:
            kind = "电解电容" if is_elec else "电感"
            return (
                f"{kind}体积不符：BOM 最大边 {req_max:g}mm，候选 {cand_max:g}mm"
                "（大概率装不进焊盘）"
            )
    if is_elec:
        req_v = _volts(line.get("mpn") or "") or _volts((line.get("row") or {}).get("mpn") or "")
        # 标题里列了「10/16/25/35V」这种多档电压的，第一个数字不代表实际买到的那档，
        # 不能按它判耐压（否则好行被误杀）。这种交给 SKU 层用实际选中值判。
        cand_vs = {v for v in re.findall(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", str(title or ""))}
        if req_v and len(cand_vs) == 1:
            cand_v = float(next(iter(cand_vs)))
            if cand_v < req_v - 1e-9:
                return f"电解电容耐压不够：BOM 要 {req_v:g}V，候选只有 {cand_v:g}V"
    return None


def _candidate_veto(line: dict, title: str) -> str | None:
    """候选标题层面的"料不对"判断（在打开商品之前就拦掉，省时间也防买错）。"""
    t = str(title or "")
    if not t:
        return "标题为空"
    _dv = _dielectric_verdict(line, t)
    if _dv:
        return _dv
    # 尺寸/耐压（电解、电感）：容值对了也可能装不上，见 _size_verdict 注释
    _sv = _size_verdict(line, t)
    if _sv:
        return _sv
    # 连接器系列：KF2510 与 XH 同为 2.54 但互不兼容，只对针数/间距会被它骗过去
    blob = _line_blob(line)
    want_fam = _conn_family(blob)
    got_fam = _conn_family(t)
    if want_fam and got_fam and want_fam != got_fam:
        return f"连接器系列不符：BOM 是 {want_fam}，候选是 {got_fam}"
    if any(w in blob for w in ("保险丝", "熔断")):
        if not any(w in t for w in ("保险丝", "熔断", "Fuse", "fuse", "FUSE")):
            return "不是保险丝（标题没有保险丝/熔断字样）"
    if _conn_kind(line) or "排针" in blob or "端子台" in blob:
        if not any(w in blob for w in ("带线", "线束", "线材", "端子线")):
            if any(w in t for w in _FORM_HARNESS_WORDS):
                return "形态不符：要板端针座，这是带线/母头"
    return None


def _title_has_mpn(line: dict, title: str) -> bool:
    """标题里有没有这个料号。只用来**排序优先**，不做否决（国产替代很常见）。"""
    mpn = _alnum(line.get("mpn"))
    if len(mpn) < 6:
        return True
    return mpn in _alnum(title)


def _tie_pick(line: dict, options: list):
    """同分选项里挑最贴合 BOM 的：电压正好等于要求 > 大于要求 > 没有电压；再比包装小。"""
    p = line.get("params") or {}

    def key(pair):
        _score, v = pair
        text = str(v.get("text", ""))
        rank = []
        if "voltage_v" in p:
            m = re.search(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])", text)
            if m:
                rank.append(0 if abs(float(m.group(1)) - p["voltage_v"]) < 1e-9 else 1)
            else:
                rank.append(2)
        if "tolerance_pct" in p:
            # 同分时优选「容差更紧或等于要求」；没写容差的排最后
            got = _sku_tolerance(text)
            if got is None:
                rank.append(2)
            elif got <= float(p["tolerance_pct"]) + 1e-9:
                rank.append(0)
            else:
                rank.append(1)
        rank.append(_pack_in_text(text) or 1)
        return tuple(rank)

    return min(options, key=key)[1]


def suggest_sku(line: dict, groups: list) -> tuple[list, list]:
    suggestions, unresolved = [], []
    # P0-3：精密档（要求 ≤0.5%）最终选中的 SKU 组合里必须能读到容差，
    # 否则不自动买。放在组循环外统一判 —— 容值/容差经常是独立 SKU 组，
    # 单组里判「未标注」会把容值组全误杀；「任一选项标注了就放行」也不行，
    # 混合组里 ±1% 被 veto 后，未标注的「24.9K」会成唯一选项被买走（#70 形态）。
    _p = line.get("params") or {}
    _precision = (
        TOLERANCE_PRECISION_VETO
        and "tolerance_pct" in _p
        and float(_p["tolerance_pct"]) <= TOLERANCE_PRECISION_PCT + 1e-9
    )
    for group in groups or []:
        label = (group.get("label") or "").strip()
        values = group.get("values") or []
        enabled = [v for v in values if not v.get("disabled") and str(v.get("text") or "").strip()]
        if not values:
            continue
        if not enabled:
            unresolved.append({"label": label, "reason": "该组没有可选项（可能缺货）", "values": []})
            continue

        # 硬属性冲突的值直接出局：能不能用，和值不值，是两件事。
        keeps, vetoed = [], []
        for v in enabled:
            why = _sku_veto(line, label, v.get("text", ""))
            if why:
                vetoed.append((v, why))
            else:
                keeps.append(v)
        if not keeps:
            reason = "所有选项都与 BOM 冲突"
            if vetoed:
                reason += f"（{vetoed[0][1]}）"
            unresolved.append(
                {
                    "label": label,
                    "reason": reason,
                    "values": [v.get("text") for v in enabled[:20]],
                }
            )
            continue
        enabled_ok = keeps

        scored = sorted(
            [(_score_sku_value(line, label, v.get("text", "")), v) for v in enabled_ok],
            key=lambda x: -x[0],
        )
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else -999
        selected = [v for v in enabled_ok if v.get("selected")]

        pick, auto, note = None, False, ""
        _any_spec = any(_sku_has_spec(line, v.get("text", "")) for v in enabled_ok)
        if selected and _score_sku_value(line, label, selected[0].get("text", "")) >= 10:
            pick, auto = selected[0], True
            note = "保留页面已选值"
        elif not _any_spec:
            # 整组没有一个值能对上 BOM 参数（纯包装数量 / 颜色 / 产地）：
            # 它不是规格维度，保留页面已选或取最小包装，不能因为「认不出来」把整行卡死。
            pick = selected[0] if selected else min(
                enabled_ok, key=lambda v: (_pack_in_text(v.get("text", "")) or 1)
            )
            auto = True
            note = "该组不区分电气规格，保留默认"
        elif best_score >= 10 and (len(scored) == 1 or best_score - second >= 5):
            pick, auto = best, True
            note = "按 BOM 参数匹配"
        elif len(enabled_ok) == 1 and best_score > -50:
            # 唯一选项（已过 veto）也要分数不为负才自动加购。
            pick, auto = enabled_ok[0], True
            note = "该组唯一可选"
        elif best_score >= 10 and len(scored) > 1:
            # 分数打平：以前这里是 min(包装数)，包装数解析不出来就退化成
            # 「取选项列表第一个」—— 电压组 [6.3,10,16,25,50]V 里 10V 以上
            # 全是 +15 平手，结果挑中了排第一的 6.3V（−30 分那个）。
            # 现在同分里按「电压正好等于要求 → 包装最小」挑，绝不按列表顺序。
            ties = [x for x in scored if x[0] >= best_score - 0.5]
            pick = _tie_pick(line, ties)
            auto = True
            note = "同型号多个选项，取最贴合 BOM 的"
        elif len(enabled_ok) > 1 and all(_is_pack_only(v.get("text", "")) for v in enabled_ok):
            # 这一组只是在卖包装数量（10个 / 100个），没有规格差异 —— 取最小包装，
            # 数量按 BOM 折算，不放大采购量。
            pick = min(enabled_ok, key=lambda v: (_pack_in_text(v.get("text", "")) or 1))
            auto = True
            note = "该组只区分包装数量，取最小包装"
        elif best_score <= -50:
            unresolved.append(
                {
                    "label": label,
                    "reason": "所有选项都与 BOM 冲突",
                    "values": [v.get("text") for v in enabled[:20]],
                }
            )
            continue
        else:
            # 页面默认值可能和 BOM 不符，不能「先加了再说」——交给人工选
            hint = f"页面默认「{selected[0].get('text')}」无法确认符合 BOM" if selected else "无法唯一匹配"
            unresolved.append(
                {
                    "label": label,
                    "reason": hint + "，请在页面里手动选择",
                    "values": [v.get("text") for v in enabled[:30]],
                }
            )
            continue

        # 备注里写清容差放宽了多少 —— 报告/日志要能直接看出「这个买宽了」。
        # 只在**选中的值自己写了容差**时加，否则「容值/电压」这类组也会被
        # 挂上「容差未标注」，纯属噪音。
        if "tolerance_pct" in (line.get("params") or {}):
            _got_tol = _sku_tolerance(pick.get("text", ""))
            if _got_tol is not None:
                _d, _n = _tol_verdict(float(line["params"]["tolerance_pct"]), _got_tol)
                if _n:
                    note = f"{note}；{_n}" if note else _n
        # P0-4：BOM 要求介质但选中值没写 —— 不拦（很多商家不写），但必须留痕
        if (line.get("params") or {}).get("dielectric") and not _dielectric_of(pick.get("text", "")):
            note = f"{note}；介质未标注，请人工核对是否 {line['params']['dielectric']}" if note \
                else "介质未标注，请人工核对"

        suggestions.append(
            {
                "label": label,
                "value": pick.get("text", ""),
                "auto": auto,
                "score": round(best_score, 1),
                "note": note,
            }
        )
    # P0-3 收口：精密档但最终选中的组合读不出容差 → 不自动买，交人工/换店。
    if _precision and suggestions:
        _combined = " ".join(s.get("value", "") for s in suggestions)
        if _sku_tolerance(_combined) is None:
            unresolved.append(
                {
                    "label": "容差",
                    "reason": (
                        f"精密档（要求 {_p['tolerance_pct']:g}%）：SKU 组合里读不到容差标注，"
                        "无法确认精度，不予自动购买"
                    ),
                    "values": [],
                }
            )
            return [], unresolved
    return suggestions, unresolved


def groups_without_selection(groups: list) -> list:
    missing = []
    for g in groups or []:
        label = (g.get("label") or "").strip()
        values = g.get("values") or []
        if not label or not values:
            continue
        if not any(v.get("selected") for v in values):
            missing.append({"label": label, "values": [v.get("text") for v in values if not v.get("disabled")][:20]})
    return missing
