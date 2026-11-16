import atexit
import base64
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
ROOT = pathlib.Path(__file__).parent

# 这套测试打的是**正在运行的真实服务**，而 /api/analyze 会写 runs/active-bom.json。
# 不还原的话，跑完测试用户页面上的「当前 BOM」就被换成了测试表格，
# 真实进度的指纹对不上 → 界面报「换过 BOM」，19 行进度看着像失效了。
_ACTIVE_BOM = ROOT / "runs" / "active-bom.json"
_active_bom_bak = _ACTIVE_BOM.read_bytes() if _ACTIVE_BOM.exists() else None


def _restore_active_bom():
    try:
        if _active_bom_bak is None:
            _ACTIVE_BOM.unlink(missing_ok=True)
        else:
            _ACTIVE_BOM.write_bytes(_active_bom_bak)
    except Exception:
        pass


atexit.register(_restore_active_bom)

# 本机 HTTP 一律绕开系统代理：这台机器设了 HTTP_PROXY，127.0.0.1 的请求
# 被代理接走后返回 502，测试会误报成「服务挂了」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    with _OPENER.open(BASE + path, timeout=10) as r:
        return r.status, r.read()


print("GET /", get("/")[0])
print("GET /static/app.js", get("/static/app.js")[0])
print("GET /samples/demo_bom.csv", get("/samples/demo_bom.csv")[0])
print("GET /api/health", json.loads(get("/api/health")[1]))

raw = (ROOT / "samples/demo_bom.csv").read_bytes()
code, parsed = post("/api/parse", {"filename": "demo_bom.csv", "data": base64.b64encode(raw).decode()})
print("PARSE", code, "rows", len(parsed["rows"]), "cols", list(parsed["columns"].values()))

code, result = post("/api/analyze", {"rows": parsed["rows"]})
print("ANALYZE", code, result["summary"], result["categories"])
for ln in result["lines"]:
    print("  ", ln["line_no"], ln["category"], ln["package"], "|", ln["status"], "|", ln["decision"]["headline"][:60])
print("  queries#1:", result["lines"][0]["decision"]["search_queries"])

cand = (ROOT / "samples/demo_candidates.csv").read_text(encoding="utf-8")
code, score = post("/api/score", {"line": result["lines"][0], "csv": cand})
print("SCORE", code, score["stats"])
print("  verdict:", score["verdict"])
for c in score["candidates"]:
    print("  ", c["rank"], c["score"], c["decision"], c["title"][:32])

code, err = post("/api/parse", {"filename": "x.txt", "data": base64.b64encode(b"a,b\n1,2").decode()})
print("BAD HEADER", code, err)

code, err = post("/api/score", {"line": result["lines"][0], "csv": "foo,bar\n1,2"})
print("BAD CAND", code, err)

# xlsx 路径
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.append(["行号", "型号", "名称", "封装", "参数", "数量"])
ws.append([1, "RC0603FR-0710KL", "贴片电阻", "0603", "10kΩ ±1% 0.1W", 100])
ws.append([2, "STM32F103C8T6", "MCU", "LQFP48", "64KB Flash", 10])
with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
    wb.save(f.name)
    xlsx_bytes = pathlib.Path(f.name).read_bytes()
os.unlink(f.name)
code, parsed_x = post("/api/parse", {"filename": "bom.xlsx", "data": base64.b64encode(xlsx_bytes).decode()})
print("PARSE XLSX", code, "rows", len(parsed_x["rows"]), parsed_x["rows"][0]["mpn"])
code, res_x = post("/api/analyze", {"rows": parsed_x["rows"]})
print("ANALYZE XLSX", code, res_x["summary"])

# 批量 / 删除的入参校验（都不会真的动购物车）
code, err = post("/api/taobao/cart-remove", {})
print("CART REMOVE NO ARG", code, err.get("error"))

code, err = post("/api/taobao/cart-remove", {"all": True})
print("CART CLEAR NO CONFIRM", code, err.get("error"))

# 必须显式关掉 resume：空 lines + resume=True 会让服务端拿磁盘上「上次的行」
# 真的启动一批（曾经把测试数据写进真实进度、还跑了一次淘宝搜索）。
code, err = post("/api/taobao/batch-start", {"lines": [], "resume": False})
print("BATCH EMPTY", code, err.get("error"))

# 重跑参数必须校验：redo_lines 传错类型时要在进执行层之前就 400，
# 不能带着脏数据去猜「用户大概想重跑哪一行」。
code, err = post(
    "/api/taobao/batch-start",
    {"lines": [{"line_no": 1}], "resume": False, "redo_lines": "1,2"},
)
print("BATCH REDO_LINES NOT LIST", code, err.get("error"))
code, err = post(
    "/api/taobao/batch-start",
    {"lines": [{"line_no": 1}], "resume": False, "redo_lines": ["二"]},
)
print("BATCH REDO_LINES NOT INT", code, err.get("error"))

print("BATCH STATUS", json.loads(get("/api/taobao/batch")[1]).keys())

# ---- CSRF 防线（2026-11-11）----
# 1) 关服务不能挂在 GET 上：否则任意网页 <img src=".../api/shutdown"> 就能关掉服务
try:
    code = get("/api/shutdown")[0]
    print("SHUTDOWN VIA GET", code, "(应 404)")
except urllib.error.HTTPError as e:
    print("SHUTDOWN VIA GET", e.code, "(应 404)")

# 2) 跨站 POST 必须被拒：恶意页面可用 fetch + text/plain 绕过 JSON 预检
req = urllib.request.Request(
    BASE + "/api/taobao/cart-remove",
    data=json.dumps({"all": True, "confirm": "清空"}).encode("utf-8"),
    headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
    method="POST",
)
try:
    with _OPENER.open(req, timeout=10) as r:
        print("CROSS-ORIGIN POST", r.status, "(应 403)")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8")
    print("CROSS-ORIGIN POST", e.code, body[:60], "(应 403)")

# 3) 同源（无 Origin 的命令行调用）照常放行：这里是只读接口
print("SAME-ORIGIN STILL OK", post("/api/type-check", {})[0], "(404 = 路由不存在，说明没被 403 误杀)")

# 4) 批量结果导出（CSV，Excel 直接打开不乱码）
with _OPENER.open(BASE + "/api/taobao/batch/export.csv", timeout=10) as r:
    _csv = r.read().decode("utf-8-sig")
    print(
        "BATCH EXPORT",
        r.status,
        "| 表头:",
        _csv.splitlines()[0][:32],
        "| 行数:",
        len(_csv.splitlines()),
        "|",
        (r.headers.get("Content-Disposition") or "")[:28],
    )

# 5) 实时推送（SSE）：页面靠这条流刷新进度。断成死流前端会退回轮询，
#    但「能连上却永远不发第一帧」是查不出来的静默故障，所以这里必须验第一帧。
try:
    _s = _OPENER.open(BASE + "/api/taobao/batch/stream", timeout=10)
    _ctype = _s.headers.get("Content-Type") or ""
    _first = _s.readline().decode("utf-8", "replace").strip()
    _data = ""
    for _ in range(6):
        _ln = _s.readline().decode("utf-8", "replace").strip()
        if _ln.startswith("data: "):
            _data = _ln[6:]
            break
    _s.close()
    _keys = sorted(json.loads(_data).keys())[:5] if _data else []
    print("BATCH STREAM", _ctype.split(";")[0], "| first:", _first, "| keys:", _keys)
except Exception as e:  # noqa: BLE001
    print("BATCH STREAM FAILED", type(e).__name__, e, "(应能连上并发出第一帧)")

print("ALL HTTP TESTS DONE")

