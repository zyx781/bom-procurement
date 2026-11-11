import base64
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
ROOT = pathlib.Path(__file__).parent


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
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

code, err = post("/api/taobao/batch-start", {"lines": []})
print("BATCH EMPTY", code, err.get("error"))

print("BATCH STATUS", json.loads(get("/api/taobao/batch")[1]).keys())

print("ALL HTTP TESTS DONE")
