#!/usr/bin/env python3
"""诊断页导出链路测试：出问题时用户交给维护者的那份包，必须安全、可读、可定位。

守这几条：
1. 脱敏：cookie、token、手机号、邮箱、身份证、长随机串打码；
   但异常堆栈里的 `------` 分隔线不能打码（否则没法读）。
2. 白名单：包里不出现 profile、会话快照、订单、BOM 原文件；
   BOM 明细只带行号/型号/状态/失败原因，不带原始行、搜索词、商品链接。
3. 兜底：日志从尾部读、有上限；日志名只认清单，不能路径穿越。
4. 前端错误落盘：限长、限行、写失败不影响主流程。

全程用临时 runs，不碰真实 profile / 订单 / 登录态。
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
import tempfile
import zipfile

BASE = pathlib.Path(__file__).parent
sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="bom_diag_test_"))
os.environ["TAOBAO_RUNS_DIR"] = str(TMP)
(TMP / "logs").mkdir(exist_ok=True)

import diag  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("== 脱敏 ==")
check(
    "Cookie 整行打码",
    diag.redact("Cookie: cna=abc123; unb=2208123456789; _tb_token_=deadbeef") == "Cookie: ***",
)
check("键值打码", diag.redact("token=abcdefghijkl & x=1") == "token=*** & x=1")
check("手机号打码", diag.redact("电话 13812345678 结束") == "电话 138******78 结束")
check("邮箱打码", re.search(r"z\*\*\*@example\.com", diag.redact("z@example.com")) is not None)
check(
    "身份证打码",
    diag.redact("证件 110101199001011234") == "证件 110101********1234",
)
check("长随机串打码", "已省略" in diag.redact("key=" + "aB3dE5fG" * 6))
check("分隔线保留", diag.redact("-" * 40) == "-" * 40)
check("等号线保留", diag.redact("=" * 60) == "=" * 60)
check("空串不炸", diag.redact("") == "")

print()
print("== 日志读取 ==")
big = TMP / "big.log"
# 1.2MB：尾部读要截，不能整文件加载
big.write_text(("x" * 80 + "\n") * 16000, encoding="utf-8")
tail = diag.tail_text(big, max_lines=10, max_bytes=1024)
check("尾部读取有上限", len(tail.splitlines()) <= 10, f"lines={len(tail.splitlines())}")
check("尾部读取不含文件头", "x" * 80 in tail)
check("缺失文件返回空", diag.tail_text(TMP / "missing.log") == "")

# 白名单：只认 log_files() 里的名字
(TMP / "server.err.log").write_text("ERROR: 测试堆栈\n" + "-" * 40, encoding="utf-8")
(TMP / "server.out.log").write_text("SERVING\n", encoding="utf-8")
(TMP / "session-bom-20260101-000000.log").write_text("[site] session started\n", encoding="utf-8")
(TMP / "client.log").write_text('{"kind":"error"}\n', encoding="utf-8")
check("白名单内可解析", diag.resolve_log("server.err") == TMP / "server.err.log")
check("文件名也可解析", diag.resolve_log("server.err.log") == TMP / "server.err.log")
check("路径穿越被拒", diag.resolve_log("../server.py") is None)
check("任意文件被拒", diag.resolve_log("taobao-session.json") is None)
names = [x["key"] for x in diag.log_files()]
check("日志清单含四类", {"server.out", "server.err", "client"} <= set(names), str(names))
check("会话日志在清单里", any(n.startswith("session-") for n in names), str(names))

print()
print("== 诊断流水脱敏 ==")
(TMP / "diagnostics-2026-01-01.jsonl").write_text(
    json.dumps({"line": 7, "stage": "search", "query": "手机号 13812345678", "count": 3}, ensure_ascii=False)
    + "\n",
    encoding="utf-8",
)
clean = diag._redact_json_line(
    json.dumps({"a": "unb=123456789", "n": 1, "l": ["13812345678"]}, ensure_ascii=False)
)
obj = json.loads(clean)
check("JSONL 结构不坏", obj["n"] == 1 and len(obj["l"]) == 1, clean)
check("JSONL 值脱敏", "已省略" in obj["a"] or "***" in obj["a"], clean)
check("JSONL 数组脱敏", "138******78" in obj["l"][0], clean)

print()
print("== BOM 明细字段白名单 ==")
fake_bom = {
    "saved_at": "2026-01-01 00:00:00",
    "result": {
        "engine_version": "0.1.0",
        "summary": {"ready": 1},
        "lines": [
            {
                "line_no": 7,
                "mpn": "ABC123",
                "desc": "测试元件",
                "status": "needs_user",
                "hard_gates": ["NAME_MISSING"],
                # 下面三个绝不能进包
                "row": {"raw": "整行原始表格", "supplier": "某店铺"},
                "query_plan": ["搜索词一", "搜索词二"],
                "decision": {"url": "https://item.taobao.com/item.htm?id=123", "price": 99.9},
            }
        ],
    },
}
(TMP / "last-analysis.json").write_text(json.dumps(fake_bom, ensure_ascii=False), encoding="utf-8")
bom = diag.bom_lines()
check("BOM 可读", bom["available"] is True and bom["count"] == 1)
keys = set(bom["lines"][0])
check("保留行号与型号", {"line_no", "mpn", "status", "hard_gates"} <= keys, str(sorted(keys)))
check("不带原始行", "row" not in keys)
check("不带搜索词", "query_plan" not in keys)
check("不带链接与价格", "decision" not in keys)
check("不带商户信息", "supplier" not in json.dumps(bom, ensure_ascii=False))

print()
print("== 打包 ==")
name, data = diag.build_bundle(note="电话 13812345678，点了启动一直卡住", include_bom=True)
check("包名带时间前缀", name.startswith("BOM采购台-日志-") and name.endswith(".zip"), name)
check("包体积可控", 0 < len(data) < 8 * 1024 * 1024, str(len(data)))
with zipfile.ZipFile(io.BytesIO(data)) as zf:
    packed = zf.namelist()
    check("有 README", "README.txt" in packed)
    check("有环境摘要", "environment.txt" in packed and "environment.json" in packed)
    check("有服务日志", "logs/server.err.log" in packed)
    check("有会话日志", any(n.startswith("logs/session-") for n in packed))
    check("有诊断流水", any(n.startswith("diagnostics/") for n in packed))
    check("有会话状态", "state/session.json" in packed)
    check("有 BOM 明细", "state/bom-lines.json" in packed)

    bad_names = [n for n in packed if any(
        k in n.lower() for k in ("profile", "cookie", "taobao-session", ".cdp", "bom_")
    )]
    check("无敏感文件名", not bad_names, str(bad_names))

    all_text = "\n".join(
        zf.read(n).decode("utf-8", "replace") for n in packed if not n.endswith(".zip")
    )
    check("手机号已脱敏", "13812345678" not in all_text)
    check("cookie 值已脱敏", "2208123456789" not in all_text)
    check("商品链接不带出", "item.taobao.com" not in all_text)
    check("堆栈分隔线可读", "-" * 40 in all_text)

    readme = zf.read("README.txt").decode("utf-8")
    check("用户描述进 README", "点了启动一直卡住" in readme)
    check("未勾选时不带 BOM", "state/bom-lines.json" not in zipfile.ZipFile(
        io.BytesIO(diag.build_bundle(include_bom=False)[1])
    ).namelist())

print()
print("== 前端错误落盘 ==")
before = diag._client_error_count()
diag.append_client_log(
    {
        "kind": "error",
        "message": "Uncaught 13812345678 " + "X" * 2000,
        "page": "/",
        "extra": "app.js:1",
        "ua": "test",
    }
)
check("错误写入一行", diag._client_error_count() == before + 1)
line = json.loads((TMP / "client.log").read_text(encoding="utf-8").strip().splitlines()[-1])
check("消息限长", len(line["message"]) <= 800, str(len(line["message"])))
check("消息脱敏", "13812345678" not in line["message"] and "138******78" in line["message"])

# 写满后要自动截断，不能无限增长
for i in range(diag.MAX_CLIENT_LINES + 30):
    diag.append_client_log({"kind": "error", "message": f"loop-{i}", "page": "/", "ua": "t"})
check("错误日志限行", diag._client_error_count() <= diag.MAX_CLIENT_LINES, str(diag._client_error_count()))

print()
print("== 环境摘要 ==")
s = diag.summary()
for key in ("app_version", "engine_version", "python", "os", "deps", "browser", "session", "runs", "logs"):
    check(f"摘要含 {key}", key in s)
check("摘要不含 profile 路径", ".cdp-profile" not in json.dumps(s, ensure_ascii=False))
check("依赖探测齐全", {"playwright", "openpyxl", "ddddocr", "opencv"} <= set(s["deps"]))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
