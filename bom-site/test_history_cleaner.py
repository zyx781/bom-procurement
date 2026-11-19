#!/usr/bin/env python3
"""淘宝历史清理：浏览器历史里不该留下「买了什么料」。

用户反馈（2026-11-19）：v0.13.4 为了单窗口关掉无痕之后，Edge 历史记录里
还是能看到一串「0603WAF2201T5E 电阻_淘宝搜索」「商品详情」。这套测试锁住
四件事：

1. **只删淘宝**：同一个 profile 里顺手打开的 DeepSeek、本机 8765 页面必须留着；
2. **连子表一起删**：visits / content_annotations / keyword_search_terms /
   edge_urls 留孤儿行，历史面板会渲染成空白条目；
3. **只碰自己的 profile**：路径不是 .cdp-profile、也没有 .bom-managed 标记，
   直接拒绝 —— 绝不误删用户自己 Edge 的历史；
4. **锁住了不添乱**：浏览器正写着库时返回 locked，下一轮再试，不抛异常。
"""

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import history_cleaner as hc

import taobao_runner as tr

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f"  [{extra}]" if extra else ""))


def eq(name, got, want):
    check(name, got == want, f"got={got!r} want={want!r}")


SCHEMA = """
CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INT,
                  typed_count INT, last_visit_time INT, hidden INT);
CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INT, from_visit INT,
                    transition INT, segment_id INT, visit_duration INT,
                    incremented_omnibox_typed_score INT, opener_visit INT);
CREATE TABLE visit_source(id INTEGER PRIMARY KEY, source INT);
CREATE TABLE content_annotations(visit_id INTEGER PRIMARY KEY, visibility_score INT, search_terms TEXT);
CREATE TABLE edge_visits(visit_id INTEGER PRIMARY KEY, has_user_interaction INT,
                         active_visit_duration INT, conversation_id TEXT);
CREATE TABLE edge_urls(url_id INTEGER PRIMARY KEY, topics TEXT, favicon_id INT);
CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT, normalized_term TEXT);
CREATE TABLE edge_keyword_search_terms_source(keyword_id INTEGER, url_id INTEGER, source INT);
CREATE TABLE segments(id INTEGER PRIMARY KEY, name TEXT, url_id INTEGER);
CREATE TABLE segment_usage(id INTEGER PRIMARY KEY, segment_id INT, time_slot INT, visit_count INT);
CREATE TABLE visited_links(id INTEGER PRIMARY KEY, link_url_id INT, top_level_url TEXT,
                           frame_url TEXT, visit_count INT);
"""

URLS = [
    (1, "https://s.taobao.com/search?q=0603WAF2201T5E", "0603WAF2201T5E 电阻_淘宝搜索"),
    (2, "https://item.taobao.com/item.htm?id=1", "0603贴片电阻 100只"),
    (3, "https://detail.tmall.com/item.htm?id=2", "贴片电容"),
    (4, "https://chat.deepseek.com/", "DeepSeek"),
    (5, "http://127.0.0.1:8765/", "BOM 采购台"),
]


def build(tmp: Path, name=".cdp-profile") -> tuple[Path, Path]:
    profile = tmp / name
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    db = profile / "Default" / "History"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO urls(id,url,title,visit_count,typed_count,last_visit_time,hidden) "
        "VALUES(?,?,?,1,0,0,0)",
        URLS,
    )
    con.executemany("INSERT INTO visits(id,url) VALUES(?,?)", [(10, 1), (11, 2), (12, 3), (13, 4), (14, 5)])
    con.executemany("INSERT INTO visit_source(id,source) VALUES(?,0)", [(10,), (11,), (13,)])
    con.executemany(
        "INSERT INTO content_annotations(visit_id,search_terms) VALUES(?,?)",
        [(10, "电阻"), (13, "deepseek")],
    )
    con.executemany("INSERT INTO edge_visits(visit_id) VALUES(?)", [(10,), (13,)])
    con.executemany("INSERT INTO edge_urls(url_id,favicon_id) VALUES(?,1)", [(1,), (2,), (4,)])
    con.executemany(
        "INSERT INTO keyword_search_terms(keyword_id,url_id,term,normalized_term) VALUES(1,?,?,?)",
        [(1, "电阻", "电阻"), (4, "deepseek", "deepseek")],
    )
    con.executemany(
        "INSERT INTO edge_keyword_search_terms_source(keyword_id,url_id,source) VALUES(1,?,0)",
        [(1,), (4,)],
    )
    con.executemany("INSERT INTO segments(id,name,url_id) VALUES(?,?,?)", [(1, "seg", 1), (2, "seg", 4)])
    con.execute("INSERT INTO segment_usage(id,segment_id,time_slot,visit_count) VALUES(1,1,0,1)")
    con.execute("INSERT INTO visited_links(id,link_url_id,top_level_url) VALUES(1,2,'https://s.taobao.com')")
    con.execute("INSERT INTO visited_links(id,link_url_id,top_level_url) VALUES(2,4,'https://chat.deepseek.com')")
    con.commit()
    con.close()
    return profile, db


def counts(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {}
        for t in (
            "urls",
            "visits",
            "visit_source",
            "content_annotations",
            "edge_visits",
            "edge_urls",
            "keyword_search_terms",
            "edge_keyword_search_terms_source",
            "segments",
            "segment_usage",
            "visited_links",
        ):
            out[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] if t in have else 0
        return out
    finally:
        con.close()


print("== 1. 只删淘宝，别的页面留着 ==")
tmp = Path(tempfile.mkdtemp(prefix="bom-hist-"))
try:
    profile, db = build(tmp)
    (profile / hc.MARKER_NAME).write_text("bom", encoding="utf-8")
    r = hc.purge(profile)
    check("清理成功", r.get("ok") is True, repr(r))
    eq("删了 3 条淘宝 URL", r.get("urls"), 3)
    eq("删了 3 次访问", r.get("visits"), 3)
    n = counts(db)
    eq("urls 只剩非淘宝", n["urls"], 2)
    eq("visits 只剩非淘宝", n["visits"], 2)
    con = sqlite3.connect(db)
    remain = [row[0] for row in con.execute("SELECT url FROM urls ORDER BY id")]
    con.close()
    eq("留下的是 DeepSeek 和本机页面", remain, ["https://chat.deepseek.com/", "http://127.0.0.1:8765/"])

    print("== 2. 子表不留孤儿行 ==")
    eq("visit_source 只剩非淘宝", n["visit_source"], 1)
    eq("content_annotations 只剩非淘宝", n["content_annotations"], 1)
    eq("edge_visits 只剩非淘宝", n["edge_visits"], 1)
    eq("edge_urls 只剩非淘宝", n["edge_urls"], 1)
    eq("keyword_search_terms 只剩非淘宝", n["keyword_search_terms"], 1)
    eq("edge_keyword_search_terms_source 只剩非淘宝", n["edge_keyword_search_terms_source"], 1)
    eq("segments 只剩非淘宝", n["segments"], 1)
    eq("segment_usage 跟着 segment 一起删", n["segment_usage"], 0)
    eq("visited_links 只删指向淘宝的", n["visited_links"], 1)

    print("== 3. 再跑一次是空操作（幂等） ==")
    r2 = hc.purge(profile)
    eq("第二次没有可删的", r2.get("urls"), 0)
    eq("库结构没被改坏", counts(db)["urls"], 2)

    print("== 4. 只认自己的 profile ==")
    other = tmp / "someone-else"
    (other / "Default").mkdir(parents=True)
    (other / "Default" / "History").write_bytes(b"")
    r3 = hc.purge(other)
    eq("陌生目录直接拒绝", r3.get("skipped"), "not-app-profile")
    check("拒绝时不动手", r3.get("ok") is False)
    check("真正的 .cdp-profile 才放行", hc._app_owned(profile) is True)
    bare = Path(tempfile.mkdtemp(prefix="bom-hist-bare-")) / ".cdp-profile"
    bare.mkdir(parents=True)
    bare.joinpath("Default").mkdir()
    check("临时目录里的 .cdp-profile 不认（不在项目内、无标记）", hc._app_owned(bare) is False)
    eq("给这类目录打清理请求也会被拒", hc.purge(bare).get("skipped"), "not-app-profile")

    print("== 5. 表结构缺项（Chrome 没有 edge_* 表）也能跑 ==")
    tmp2 = Path(tempfile.mkdtemp(prefix="bom-hist-chrome-"))
    prof2 = tmp2 / ".cdp-profile"
    (prof2 / "Default").mkdir(parents=True)
    (prof2 / hc.MARKER_NAME).write_text("bom", encoding="utf-8")
    db2 = prof2 / "Default" / "History"
    con = sqlite3.connect(db2)
    con.executescript(
        "CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER);"
    )
    con.execute("INSERT INTO urls(id,url,title) VALUES(1,'https://s.taobao.com/search?q=x','x_淘宝搜索')")
    con.execute("INSERT INTO urls(id,url,title) VALUES(2,'https://example.com/','Example')")
    con.execute("INSERT INTO visits(id,url) VALUES(1,1)")
    con.execute("INSERT INTO visits(id,url) VALUES(2,2)")
    con.commit()
    con.close()
    r4 = hc.purge(prof2)
    check("只有基础表也能清", r4.get("ok") is True and r4.get("urls") == 1, repr(r4))
    eq("基础表清完只剩 1 条", counts(db2)["urls"], 1)

    print("== 6. 浏览器正占着写锁：返回 locked，不抛异常 ==")
    profile3, db3 = build(Path(tempfile.mkdtemp(prefix="bom-hist-lock-")))
    (profile3 / hc.MARKER_NAME).write_text("bom", encoding="utf-8")
    holder = sqlite3.connect(db3)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO urls(id,url,title) VALUES(99,'https://s.taobao.com/x','锁')")
    r5 = hc.purge(profile3, timeout=0.2)
    holder.execute("ROLLBACK")
    holder.close()
    check("抢不到锁时不报错", isinstance(r5, dict) and r5.get("ok") is False, repr(r5))
    eq("标记为 locked（下一轮再试）", r5.get("skipped"), "locked")
    r6 = hc.purge(profile3, timeout=0.5)
    check("锁释放后能接着清", r6.get("ok") is True and r6.get("urls", 0) >= 3, repr(r6))

    print("== 7. 会话期间定时清理（历史面板随时打开都看不到） ==")
    profile4, db4 = build(Path(tempfile.mkdtemp(prefix="bom-hist-watch-")))
    (profile4 / hc.MARKER_NAME).write_text("bom", encoding="utf-8")
    w = hc.HistoryWatcher(profile4, interval=30, log=None)
    w.interval = 0.05  # 测试里不等 30 秒；钳制只发生在 __init__
    w.start()
    time.sleep(0.35)
    w.stop()
    eq("第一次就把淘宝记录清了", counts(db4)["urls"], 2)
    check("停掉后线程不在跑", w._thread is None)

    print("== 7b. 浏览器还开着就不动库，关了才兜底清 ==")
    profile5, db5 = build(Path(tempfile.mkdtemp(prefix="bom-hist-probe-")))
    (profile5 / hc.MARKER_NAME).write_text("bom", encoding="utf-8")
    alive = hc.HistoryWatcher(profile5, interval=30, log=None, probe=lambda: False)
    alive.interval = 0.05
    alive.start()
    time.sleep(0.25)
    alive.stop()
    eq("probe=False（浏览器在跑）时一条不删", counts(db5)["urls"], 5)
    closed = hc.HistoryWatcher(profile5, interval=30, log=None, probe=lambda: True)
    closed.interval = 0.05
    closed.start()
    time.sleep(0.25)
    closed.stop()
    eq("probe=True（浏览器已退出）后清掉淘宝", counts(db5)["urls"], 2)

    print("== 8. 接进会话生命周期 ==")
    runner = tr.SessionRunner(command=[sys.executable, "-c", "pass"])
    check("SessionRunner 认这个模块", isinstance(runner._history_watch, type(None)))
    real_purge = tr.history_cleaner.purge
    real_settings = tr.load_settings
    calls = []
    tr.history_cleaner.purge = lambda *a, **k: calls.append(1) or {"ok": True, "urls": 7, "visits": 9}
    tr.load_settings = lambda: {"clear_taobao_history": True}
    try:
        r7 = runner._clean_history("测试")
        eq("清理结果回传", r7.get("urls"), 7)
        check("清理动作真的调了", len(calls) == 1)
        check("日志里能看见条数", any("淘宝记录 7 条" in line for line in runner._logs), repr(list(runner._logs)[-3:]))
        tr.load_settings = lambda: {"clear_taobao_history": False}
        calls.clear()
        r8 = runner._clean_history("关掉时")
        check("关掉开关就不动手", r8 == {} and not calls)
    finally:
        tr.history_cleaner.purge = real_purge
        tr.load_settings = real_settings

    print("== 9. 源码契约 ==")
    src = Path("taobao_runner.py").read_text(encoding="utf-8")
    check("启动前清一次", '_clean_history("启动前")' in src)
    check("会话开始时启动定时清理", "self._start_history_watch()" in src)
    check("会话结束再清一次", '_clean_history("会话结束")' in src)
    check("重置状态时停掉线程", "_stop_history_watch()" in src and "def _reset_state" in src)
    check("状态里带清理结果", '"history": history_cleaner.last_result()' in src)
    cfg = Path("settings.example.json").read_text(encoding="utf-8")
    check("配置默认开启", '"clear_taobao_history": true' in cfg)
    check("间隔可调", '"history_cleanup_interval"' in cfg)
    diag_src = Path("diag.py").read_text(encoding="utf-8")
    check("诊断页能看到清理状态", '"history": _history_info()' in diag_src)

    print("== 10. 浏览器内扩展（运行中实时删，外部删不了独占锁） ==")
    import json as _json

    ext = Path("browser_ext") / "history_guard"
    man = _json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    eq("MV3 扩展", man.get("manifest_version"), 3)
    check("要了 history 权限", "history" in (man.get("permissions") or []), repr(man.get("permissions")))
    check("要了 alarms（worker 被回收后还能醒）", "alarms" in (man.get("permissions") or []))
    sw = (ext / "sw.js").read_text(encoding="utf-8")
    check("认淘宝域名", "taobao\\.com" in sw and "tmall\\.com" in sw)
    check("用浏览器自己的接口删", "chrome.history.deleteUrl" in sw)
    check("访问即删（onVisited）", "chrome.history.onVisited" in sw)
    check("有兜底定时", "chrome.alarms" in sw)
    check("启动器把扩展带上", "HISTORY_GUARD_EXT" in src and "--load-extension=" in src)
    check("扩展目录常量指向真实文件", tr.HISTORY_GUARD_EXT.is_dir() and (tr.HISTORY_GUARD_EXT / "sw.js").is_file())

    # 扩展是否真的加载：/json/list 里有没有 sw.js 的 service worker
    import io as _io

    real_open = tr.urllib.request.urlopen

    def fake_open(payload):
        def _open(url, timeout=1.0):
            return _io.BytesIO(json.dumps(payload).encode())
        return _open

    try:
        tr.urllib.request.urlopen = fake_open([{"type": "service_worker", "url": "chrome-extension://x/sw.js"}])
        eq("扩展在跑 → on", tr.history_guard_status("http://127.0.0.1:9222"), "on")
        tr.urllib.request.urlopen = fake_open([{"type": "page", "url": "https://www.taobao.com/"}])
        eq("扩展没加载 → off", tr.history_guard_status("http://127.0.0.1:9222"), "off")

        def boom(url, timeout=1.0):
            raise OSError("no cdp")

        tr.urllib.request.urlopen = boom
        eq("探测不到 → unknown", tr.history_guard_status("http://127.0.0.1:9222"), "unknown")
    finally:
        tr.urllib.request.urlopen = real_open
finally:
    import shutil

    for p in Path(tempfile.gettempdir()).glob("bom-hist*"):
        shutil.rmtree(p, ignore_errors=True)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
