#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端集成测试（不访问网络，用 tests/fake_session.py 当执行层）。

补的是「纯函数测试覆盖不到」的两条链路：

  A. 调度闭环：批量跑 2 行 → 购物车件数增长 → 收尾对账 → 按关键字删 → 件数回落
  B. HTTP 链路：真起一个 server 进程（TAOBAO_FAKE_SESSION=1），验证
     session/start → cart-view → cart-remove 的参数校验 / 同源校验 / 导出 CSV

跑法：python test_integration.py
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# A 段在本进程跑批量，B 段还会真起一个 server 子进程；两边都会往 runs/ 写
# 批量进度。不隔离就会把用户真实的 batch-progress.json / batch-lines.json
# 覆盖成 1 行假数据（2026-11-11 实测踩到），下次真跑会被当成「已完成」空跑。
os.environ["TAOBAO_RUNS_DIR"] = tempfile.mkdtemp(prefix="bom-test-runs-")

import engine  # noqa: E402
import taobao_runner as tr  # noqa: E402

PASS = FAIL = 0

assert tr.PROGRESS_FILE.parent != BASE / "runs", "集成测试必须隔离 runs 目录"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _runner():
    return tr.SessionRunner(
        command=[sys.executable, str(BASE / "tests" / "fake_session.py")],
        repo=BASE,
        artifacts=BASE / "tests" / "artifacts",
    )


# ---------------------------------------------------------------- A. 调度闭环
print("== A. 批量 → 对账 → 删购物车（假会话） ==")
os.environ["FAKE_CART_GROW"] = "1"  # 让 cart-add 真的往假购物车里塞

# 进度落盘换成内存，别污染真实 runs/batch-progress.json
_bak_save, _bak_load = tr.save_batch_progress, tr.load_batch_progress
tr.save_batch_progress = lambda data: None
tr.load_batch_progress = lambda: {}
# 假会话没有真实 CDP 浏览器，别让 runner 去拉一个真的 Edge：
# 本机碰巧能拉起来时测试会「通过」，CI 上没有 GUI 浏览器就会在 start_batch
# 被「淘宝浏览器已关闭」守卫拦住，变成看机器脸色通过的假绿灯。
tr.cdp_endpoint_ok = lambda *a, **k: True          # 跳过自动拉起调试浏览器
tr.ensure_debug_browser = lambda *a, **k: None
tr._window_pos = None                              # 关掉窗口/浏览器存活守卫
runner = _runner()
try:
    runner.start()
    for _ in range(40):
        if runner.status().get("ready"):
            break
        time.sleep(0.25)
    check("假会话就绪", runner.status().get("ready") is True, runner.status())

    before = runner.cart_view()
    check("初始购物车 3 件", before.get("cart_item_count") == 3, before.get("cart_item_count"))
    check("cart-view 带回行明细", len(before.get("rows") or []) == 3, before.get("rows"))

    line = engine.analyze(
        [{"line_no": 1, "mpn": "RC0603FR-0710KL", "desc": "贴片电阻",
          "package": "0603", "params": "10kΩ ±1% 0.1W 50V", "qty": 2}]
    )["lines"]

    runner.start_batch(line, resume=False, max_candidates=3)
    for _ in range(120):
        if not runner.batch_status().get("running"):
            break
        time.sleep(0.25)
    st = runner.batch_status()
    res = (st.get("results") or [{}])[0]
    check("批量跑完且 ok", res.get("status") == "ok", res)
    check("开跑前件数已记录", st.get("cart_count_before") == 3, st.get("cart_count_before"))
    check("对账通过（没降级）", (st.get("reconcile") or {}).get("downgraded") == [],
          st.get("reconcile"))

    after = runner.cart_view()
    check("购物车涨到 4 件", after.get("cart_item_count") == 4, after.get("cart_item_count"))

    removed = runner.cart_remove(keyword="10K 5%")  # 只删刚加进去那件，别连带原车里的电阻
    check("按关键字删除生效", removed.get("ok") is True and removed.get("verified") is True, removed)
    check("件数回落", (removed.get("before"), removed.get("after")) == (4, 3),
          (removed.get("before"), removed.get("after")))

    bad = runner.cart_remove()
    check("不给关键字被拒", bad.get("ok") is False and bad.get("code") == "CART_REMOVE_AMBIGUOUS", bad)

    exported = tr.batch_export_rows(st.get("results") or [])
    check("导出表头 10 列", len(exported[0]) == 10, exported[0])
    check("导出含该行", any(r[0] == "1" and r[1] == "ok" for r in exported[1:]), exported[1:])
finally:
    try:
        runner.stop(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    tr.save_batch_progress, tr.load_batch_progress = _bak_save, _bak_load
    os.environ.pop("FAKE_CART_GROW", None)


# ---------------------------------------------------------------- B. HTTP 链路
print("== B. HTTP 链路（假会话 server 进程） ==")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"


def post(path, payload, origin=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path, timeout=20):
    with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as r:
        return r.status, r.read()


env = dict(os.environ, TAOBAO_FAKE_SESSION="1")
proc = subprocess.Popen(
    [sys.executable, "server.py", "--port", str(PORT)],
    cwd=str(BASE),
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    health = None
    for _ in range(80):
        try:
            health = get("/api/health")[0]
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    check("服务起来", health == 200, health)

    code, st = post("/api/session/start", {})
    ready = False
    for _ in range(80):
        _c, s = get("/api/session/status")
        s = json.loads(s.decode("utf-8"))
        if s.get("ready"):
            ready = True
            break
        time.sleep(0.25)
    check("会话通过 HTTP 就绪", ready, s)

    code, cv = post("/api/taobao/cart", {})
    check("GET 购物车 200 且有 rows", code == 200 and len(cv.get("rows") or []) >= 1, cv)

    code, err = post("/api/taobao/cart-remove", {})
    check("删购物车不给参数 → 400", code == 400 and err.get("code") == "BAD_REQUEST", (code, err))

    code, err = post("/api/taobao/cart-remove", {"all": True})
    check("清空缺 confirm → 400", code == 400, (code, err))

    code, err = post(
        "/api/taobao/cart-remove", {"keyword": "电阻"}, origin="https://evil.example"
    )
    check("跨站删购物车 → 403", code == 403 and err.get("code") == "CROSS_ORIGIN_BLOCKED", (code, err))
    check("跨站请求没真的删掉东西", post("/api/taobao/cart", {})[1].get("cart_item_count") == 3,
          post("/api/taobao/cart", {})[1].get("cart_item_count"))

    code, ok = post("/api/taobao/cart-remove", {"keyword": "电阻"})
    check("同源/命令行删除 200 且 verified", code == 200 and ok.get("verified") is True, (code, ok))

    code, body = get("/api/taobao/batch/export.csv")
    check("导出 CSV 200 + 表头", code == 200 and "行号" in body.decode("utf-8-sig").splitlines()[0], code)

    # 换 BOM 防护：空 lines + resume 默认开启时，服务端不能拿磁盘上旧 BOM 的行
    # 偷偷起一批（真实事故里测试数据就是这么混进真实进度的）。
    def _one(mpn, headline):
        return {
            "line_no": 1, "mpn": mpn, "desc": mpn, "package": "0603", "params": "",
            "qty": 1, "query_plan": [mpn],
            "decision": {"headline": headline, "search_queries": [mpn]},
        }

    tr.save_batch_lines([_one("OLD-PART", "旧表的一行")])
    tr.set_active_bom([_one("NEW-PART", "新表的一行")])
    code, err = post("/api/taobao/batch-start", {"lines": []})
    check("换 BOM 时空 lines 被拒，不偷跑旧表", code == 400 and "BOM" in str(err.get("error")), (code, err))
    code, raw = get("/api/taobao/batch")
    st = json.loads(raw.decode("utf-8"))
    check("拒跑后批量仍是未开始", st.get("total") == 0 and st.get("running") is False, st.get("phase"))

    code, err = post("/api/shutdown", {})
    check("命令行关服务仍放行（无 Origin）", code == 200, (code, err))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:  # noqa: BLE001
        proc.kill()

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
