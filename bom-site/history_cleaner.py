#!/usr/bin/env python3
"""把淘宝的记录从调试浏览器历史里删掉（只删淘宝，别的一律不动）。

两层机制
--------
1. **浏览器里**（主力）：``browser_ext/history_guard`` 扩展用 ``chrome.history``
   接口，每次访问淘宝系页面后一秒多就把那条记录删掉 —— 历史面板里几乎不留痕。
2. **浏览器外**（这一层，兜底）：Chromium 打开 History 库时是**独占锁**，外部
   进程连只读都打不开（``database is locked``）。所以只有等浏览器退出（点「结束
   会话」、关窗口、或下次启动前）才由这里直接改 SQLite，把漏掉的旧记录清干净。
   扩展被企业策略禁用、或者用户手动关了扩展时，这一层就是最后一道。

为什么不能只靠无痕
------------------
无痕确实不写历史，但 CDP 下 ``new_context()`` 等于**再开一个窗口**，用户看到
两个画面（v0.13.3 的真实反馈）。单窗口优先，所以改成「正常窗口 + 事后删」。

安全边界（很重要）
------------------
只认项目自己的 ``.cdp-profile``：路径必须落在 bom-site 目录下，并且目录名就是
``.cdp-profile``（或带 ``.bom-managed`` 标记）。别的一律拒绝 —— 万一把用户自己
的 Edge 主 profile 传进来，那就是把人家几年的历史删了。

只删「淘宝相关」的行：url 命中 taobao.com / tmall.com / taobao.net，或标题里
带「淘宝 / 天猫」。同一个 profile 里用户顺手打开的 DeepSeek、本机 8765 页面
一条都不碰。

任何异常都吞掉并返回结果字典：清理是增强项，不能让它把启动会话搞失败。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
PROFILE_NAME = ".cdp-profile"
MARKER_NAME = ".bom-managed"

# 命中条件：URL 域名，或标题里的中文关键词。标题也要认，因为淘宝有些跳转页
# 的域名是 ``*.taobao.com`` 之外的中转域名，标题却写着「淘宝搜索」。
TAOBAO_MATCH = (
    "url LIKE '%taobao.com%'"
    " OR url LIKE '%tmall.com%'"
    " OR url LIKE '%taobao.net%'"
    " OR title LIKE '%淘宝%'"
    " OR title LIKE '%天猫%'"
)

# 这些表按 url id 或 visit id 挂着淘宝记录，先删子表再删主表，
# 不留孤儿行（历史面板会把孤儿 visit 渲染成空白条目）。
_URL_KEYED = ("keyword_search_terms", "edge_keyword_search_terms_source", "segments", "visited_links")
_VISIT_KEYED = ("content_annotations", "visit_source", "edge_visits")

_last_lock = threading.Lock()
_last_result: dict[str, Any] = {}


def last_result() -> dict[str, Any]:
    """最近一次清理的结果（给诊断页看）。"""
    with _last_lock:
        return dict(_last_result)


def _remember(result: dict[str, Any]) -> dict[str, Any]:
    with _last_lock:
        _last_result.clear()
        _last_result.update(result)
    return result


def _app_owned(profile: Path) -> bool:
    """只允许操作项目自己的专用 profile。"""
    try:
        profile = profile.resolve()
        base = BASE.resolve()
    except OSError:
        return False
    if profile == base:
        return False
    # 正常情况：项目目录下的 .cdp-profile。
    if profile.name == PROFILE_NAME and base in profile.parents:
        return True
    # 允许显式标记过的目录（测试、或用户把 profile 放到别的盘）：
    # 标记文件由程序自己创建，手工放一个进别人的 Edge 主 profile 属于自找麻烦。
    return (profile / MARKER_NAME).is_file()


def history_dbs(profile: Path) -> list[Path]:
    """profile 下所有 History 库：Default 为主，另兼容 Profile 1/2 …"""
    found: list[Path] = []
    for name in ("Default", *[f"Profile {i}" for i in range(1, 10)]):
        db = profile / name / "History"
        try:
            if db.is_file():
                found.append(db)
        except OSError:
            continue
    return found


def _existing_tables(con: sqlite3.Connection) -> set[str]:
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def purge_db(db: Path, timeout: float = 2.0) -> dict[str, Any]:
    """删掉一个 History 库里的淘宝记录，返回条数统计。

    必须浏览器已经退出：Chromium 开着时库是独占锁，连打开都会 ``database is
    locked``（此时返回 ``skipped='locked'``，交给浏览器里的扩展去删）。
    """
    started = time.monotonic()
    try:
        con = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        msg = str(exc)
        # 浏览器开着时就是这个错：独占锁连只读都进不来。别当故障报，扩展在删。
        if "lock" in msg.lower():
            return {"ok": False, "db": db.name, "skipped": "locked", "error": msg}
        return {"ok": False, "db": db.name, "error": f"open: {msg}"}
    try:
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        tables = _existing_tables(con)
        if "urls" not in tables:
            return {"ok": True, "db": db.name, "urls": 0, "visits": 0, "skipped": "no-history-table"}
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            return {"ok": False, "db": db.name, "skipped": "locked", "error": str(exc)}

        # 临时表放在连接自己的 temp 库里，不动主库 schema，也不受参数个数上限影响。
        con.execute("CREATE TEMP TABLE _bom_url(id INTEGER PRIMARY KEY)")
        con.execute("CREATE TEMP TABLE _bom_visit(id INTEGER PRIMARY KEY)")
        con.execute(f"INSERT OR IGNORE INTO _bom_url SELECT id FROM urls WHERE {TAOBAO_MATCH}")
        con.execute(
            "INSERT OR IGNORE INTO _bom_visit SELECT id FROM visits WHERE url IN (SELECT id FROM _bom_url)"
        )
        n_urls = int(con.execute("SELECT COUNT(*) FROM _bom_url").fetchone()[0] or 0)
        n_visits = int(con.execute("SELECT COUNT(*) FROM _bom_visit").fetchone()[0] or 0)
        if not n_urls:
            con.execute("COMMIT")
            return _remember(
                {
                    "ok": True,
                    "db": db.name,
                    "urls": 0,
                    "visits": 0,
                    "at": _now(),
                    "ms": int((time.monotonic() - started) * 1000),
                }
            )

        errors: list[str] = []

        def run(sql: str) -> None:
            try:
                con.execute(sql)
            except sqlite3.Error as exc:
                # 表不存在 / 结构随 Edge 版本变化：跳过这张，不整批回滚。
                errors.append(f"{sql.split()[2] if len(sql.split()) > 2 else sql}: {exc}")

        # 1) 先删挂在 visit 上的附属行（历史面板会读 content_annotations）
        for table in _VISIT_KEYED:
            if table in tables:
                col = "visit_id" if "visit_id" in _columns(con, table) else "id"
                run(f'DELETE FROM "{table}" WHERE "{col}" IN (SELECT id FROM _bom_visit)')
        # 2) 再删挂 url 的附属行
        if "segment_usage" in tables and "segments" in tables:
            run(
                "DELETE FROM segment_usage WHERE segment_id IN "
                "(SELECT id FROM segments WHERE url_id IN (SELECT id FROM _bom_url))"
            )
        for table in _URL_KEYED:
            if table not in tables:
                continue
            cols = _columns(con, table)
            key = "link_url_id" if "link_url_id" in cols else "url_id" if "url_id" in cols else ""
            if key:
                run(f'DELETE FROM "{table}" WHERE "{key}" IN (SELECT id FROM _bom_url)')
        if "edge_urls" in tables and "url_id" in _columns(con, "edge_urls"):
            run("DELETE FROM edge_urls WHERE url_id IN (SELECT id FROM _bom_url)")
        # 3) 最后删主表：visits 先走，urls 后走
        run("DELETE FROM visits WHERE id IN (SELECT id FROM _bom_visit)")
        run("DELETE FROM urls WHERE id IN (SELECT id FROM _bom_url)")
        con.execute("COMMIT")

        result = {
            "ok": True,
            "db": db.name,
            "urls": n_urls,
            "visits": n_visits,
            "at": _now(),
            "ms": int((time.monotonic() - started) * 1000),
        }
        if errors:
            result["warnings"] = errors[:4]
        return _remember(result)
    except sqlite3.Error as exc:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return {"ok": False, "db": db.name, "error": str(exc), "at": _now()}
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def purge(profile_dir: str | Path | None = None, timeout: float = 2.0) -> dict[str, Any]:
    """清理专用 profile 里的淘宝历史；profile 不对就拒绝。"""
    profile = Path(profile_dir) if profile_dir else BASE / PROFILE_NAME
    if not _app_owned(profile):
        return {
            "ok": False,
            "skipped": "not-app-profile",
            "profile": str(profile),
        }
    dbs = history_dbs(profile)
    if not dbs:
        return {"ok": True, "urls": 0, "visits": 0, "skipped": "no-db", "at": _now()}
    total_urls = total_visits = 0
    results: list[dict[str, Any]] = []
    for db in dbs:
        r = purge_db(db, timeout=timeout)
        results.append(r)
        total_urls += int(r.get("urls") or 0)
        total_visits += int(r.get("visits") or 0)
    failed = [r for r in results if not r.get("ok")]
    out: dict[str, Any] = {
        "ok": not failed,
        "urls": total_urls,
        "visits": total_visits,
        "at": _now(),
        "dbs": len(dbs),
    }
    if failed:
        out["skipped"] = failed[0].get("skipped") or "error"
        out["error"] = failed[0].get("error")
    return _remember(out)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class HistoryWatcher:
    """会话期间的兜底：等浏览器**关掉**的那一刻把库里剩下的淘宝记录删掉。

    运行中的删除交给浏览器里的扩展（``browser_ext/history_guard``）——
    Chromium 把 History 库锁成独占，外部进程连只读都打不开，只有它自己删得掉。
    所以这里的 ``probe`` 用来判断「现在能不能安全写库」：浏览器还在跑就跳过，
    等它退出（用户点「结束会话」或直接关窗口）再收尾。

    为什么还要这一层：扩展可能被企业策略禁用、或用户手动关掉了扩展；那时
    浏览器不会再删，至少关掉之后要把记录清干净。
    """

    def __init__(
        self,
        profile_dir: str | Path,
        interval: float = 10.0,
        log=None,
        probe=None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.interval = max(3.0, float(interval))
        self._log = log
        self._probe = probe  # 返回 True 才动库（浏览器没在跑）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> HistoryWatcher:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name="history-cleaner", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready = self._probe is None or bool(self._probe())
                if ready:
                    r = purge(self.profile_dir)
                    if r.get("urls") and self._log:
                        self._log(
                            f"[site] 已清掉浏览器历史里的淘宝记录 {r['urls']} 条"
                            f"（访问 {r.get('visits', 0)} 次）"
                        )
            except Exception as exc:  # noqa: BLE001 —— 清理失败不能影响跑批
                if self._log:
                    self._log(f"[site] 清理淘宝历史失败（不影响跑批）：{exc}")
            self._stop.wait(self.interval)
