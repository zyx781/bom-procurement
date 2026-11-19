"""页脚地址不能写死端口（复测 P2）。

复测现场：服务跑在 127.0.0.1:8768，页面页脚仍写「本地运行 · 127.0.0.1:8765」，
用户照着这个地址排查，越查越乱。

规则：页脚一律以 location.host 为准；HTML 里不许出现具体端口。
"""
import pathlib
import re

BASE = pathlib.Path(__file__).parent
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
app = (BASE / "static" / "app.js").read_text(encoding="utf-8")
srv = (BASE / "server.py").read_text(encoding="utf-8")

print("== 页脚不再写死端口 ==")
check("index.html 里没有硬编码的 127.0.0.1:端口", "127.0.0.1:8765" not in html,
      [l for l in html.splitlines() if "127.0.0.1" in l][:3])
check("页脚元素有稳定 id", 'id="foot-local"' in html)
m = re.search(r'<span class="foot-local" id="foot-local">([^<]*)</span>', html)
check("页脚初始文案不含端口", bool(m) and ":" not in (m.group(1) if m else ""), m.group(1) if m else "缺元素")

print("== 前端按实际访问地址生成 ==")
check("用了 location.host", "location.host" in app)
check("写进 #foot-local", 'footLocal.textContent = `本地运行 · ${location.host}`' in app)
idx_host = app.find("location.host")
idx_wire = app.find("\n  wire();")
check("在页面初始化时执行（wire 之前就设好）", 0 <= idx_host < idx_wire if idx_wire > 0 else idx_host > 0,
      (idx_host, idx_wire))

print("== 服务端端口可配置 ==")
check("--port 参数存在", "--port" in srv)
check("默认仍是 8765", re.search(r'"--port",\s*type=int,\s*default=8765', srv) is not None)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
