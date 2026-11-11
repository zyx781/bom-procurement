#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BOM 选型台 —— 本地网站（决策 + 淘宝执行）。

    python server.py            # http://127.0.0.1:8765

只监听回环地址；执行层复用本地已登录的 Taobao-Search-Skill v4 session，
只做搜索/选 SKU/加购物车，不登录、不绕验证、不结算付款。
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import mimetypes
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import engine
import taobao_runner as tr

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SAMPLES = ROOT / "samples"
RUNS = ROOT / "runs"
MAX_BODY = 25 * 1024 * 1024

if os.environ.get("TAOBAO_FAKE_SESSION") == "1":
    tr.RUNNER = tr.SessionRunner(command=[sys.executable, str(ROOT / "tests" / "fake_session.py")], repo=ROOT)

atexit.register(lambda: tr.RUNNER.stop(timeout=5))


def _shutdown_service() -> None:
    """网页里点「退出服务」：先收尸再退进程。

    顺序：停批量 → 停会话（顺手关掉调试版 Edge）→ 退进程。
    先回 HTTP 200 再做事，否则前端拿不到响应会以为没点上。
    """
    def _run():
        try:
            st = tr.RUNNER.batch_status()
            if st.get("running"):
                tr.RUNNER.stop_batch()
        except Exception:  # noqa: BLE001
            pass
        try:
            tr.RUNNER.stop(timeout=6)
        except Exception:  # noqa: BLE001
            pass
        print("[site] shutdown requested from web UI", flush=True)
        os._exit(0)  # 服务是被网页主动关掉的，不必等线程收尾

    threading.Timer(0.6, _run).start()  # 留出 0.6 s 让 HTTP 响应先写回去


class Handler(BaseHTTPRequestHandler):
    server_version = "BomSite/0.1"

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):  # 静默常规访问日志，只留错误
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, path: Path):
        if not path.is_file():
            return self._send(404, {"error": "文件不存在"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _safe(self, base: Path, rel: str) -> Path | None:
        target = (base / rel).resolve()
        if target == base.resolve() or base.resolve() in target.parents:
            return target
        return None

    def _origin_ok(self) -> bool:
        """同源校验（CSRF 防线）。

        浏览器发起的跨站请求**一定**带 Origin；命令行/脚本（curl、urllib）不带。
        所以：没有 Origin → 放行（本地脚本）；有 Origin 且不是本机同源 → 拒绝。
        只靠「必须 JSON」不够：恶意页面可以用 fetch + text/plain 发简单请求，
        不触发预检，就能把 body 原样塞进来（删购物车、关服务都够用）。
        """
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        port = getattr(self.server, "server_port", 8765)
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("请求体为空")
        if n > MAX_BODY:
            raise ValueError("文件超过 25MB 限制")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------------- GET ----------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if path == "/api/health":
            return self._send(200, {"ok": True, "engine": engine.ENGINE_VERSION, "offline": False})
        # 注意：/api/shutdown **不能**挂在 GET 上 —— 任意网页放一个
        # <img src="http://127.0.0.1:8765/api/shutdown"> 就能把服务关掉。
        # 只保留 POST + 同源校验（见 do_POST）。
        if path == "/api/session/status":
            return self._send(200, tr.RUNNER.status())
        if path == "/api/taobao/batch":
            data = tr.RUNNER.batch_status()
            # 附带「上次进度」：前端据此把按钮改成「继续加购（从第 N 行）」
            data["progress"] = tr.batch_progress_summary()
            return self._send(200, data)
        if path == "/favicon.ico":
            return self._send(204, b"")
        if path.startswith("/static/"):
            target = self._safe(STATIC, path[len("/static/") :])
            return self._file(target) if target else self._send(404, {"error": "路径非法"})
        if path.startswith("/samples/"):
            target = self._safe(SAMPLES, path[len("/samples/") :])
            return self._file(target) if target else self._send(404, {"error": "路径非法"})
        if path.startswith("/runs/"):
            target = self._safe(RUNS, path[len("/runs/") :])
            return self._file(target) if target else self._send(404, {"error": "路径非法"})
        return self._send(404, {"error": "Not found"})

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._origin_ok():
            print(f"[site] cross-origin POST blocked: {path}", flush=True)
            return self._send(403, {"error": "跨站请求被拒绝", "code": "CROSS_ORIGIN_BLOCKED"})
        try:
            body = self._read_json()
            if path == "/api/shutdown":
                # 网页里的「退出服务」（前端用 POST；同源校验已在入口做掉）
                print("[site] shutdown requested (web)", flush=True)
                self._send(200, {"ok": True, "message": "服务正在关闭"})
                _shutdown_service()  # 0.6 s 后停会话/关浏览器/退进程
                return
            if path == "/api/parse":
                filename = str(body.get("filename") or "uploaded.csv")
                b64 = body.get("data") or ""
                try:
                    raw = base64.b64decode(b64, validate=True)
                except Exception:
                    raise ValueError("文件内容不是有效的 base64")
                try:
                    text_preview = raw[:2000].decode("utf-8-sig")
                except UnicodeDecodeError:
                    text_preview = ""
                result = engine.parse_workbook(filename, raw)
                if result["rows"] and text_preview:
                    pass
                return self._send(200, result)

            if path == "/api/analyze":
                rows = body.get("rows")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("没有可分析的行")
                return self._send(200, engine.analyze(rows))

            if path == "/api/score":
                line = body.get("line")
                if not isinstance(line, dict):
                    raise ValueError("缺少 line")
                csv_text = body.get("csv") or ""
                cands = body.get("candidates")
                return self._send(200, engine.score_candidates(line, csv_text, cands))

            if path == "/api/session/start":
                return self._send(200, tr.RUNNER.start())

            if path == "/api/session/stop":
                return self._send(200, tr.RUNNER.stop())

            if path == "/api/session/recheck":
                # 「验证已完成，重新检测」：人工完成验证（滑块/图片点选）后手动解卡
                ok = tr.RUNNER.recheck_verification()
                return self._send(200, {"ok": ok, "status": tr.RUNNER.status(auto_probe=False)})

            if path == "/api/taobao/window":
                # 跑批时浏览器默认藏在屏幕外（不盖住你正在用的软件）；
                # 想看一眼就 show，看完再 hide。
                action = str(body.get("action") or "show").lower()
                if action not in ("show", "hide"):
                    raise ValueError("action 只能是 show 或 hide")
                ok = tr.RUNNER.show_browser() if action == "show" else tr.RUNNER.hide_browser()
                return self._send(200, {"ok": ok, "action": action})

            if path == "/api/maintenance/clean-screenshots":
                # 手动清截图（批量结束/会话停止时会自动清，这里是兜底）
                return self._send(200, tr.RUNNER.cleanup_screenshots())

            if path == "/api/taobao/cart":
                return self._send(200, tr.RUNNER.cart_view())

            if path == "/api/taobao/cart-remove":
                keyword = str(body.get("keyword") or "").strip()
                remove_all = bool(body.get("all"))
                if not keyword and not remove_all:
                    raise ValueError("必须给 keyword（标题关键字）或 all=true，拒绝隐式清空购物车")
                if remove_all and str(body.get("confirm") or "") != "清空":
                    raise ValueError("清空整个购物车需要 confirm=\"清空\"")
                return self._send(200, tr.RUNNER.cart_remove(keyword=keyword or None, remove_all=remove_all))

            if path == "/api/taobao/batch-start":
                lines = body.get("lines")
                if (not isinstance(lines, list) or not lines) and bool(body.get("resume", True)):
                    # 服务重启后前端没重传行时，用上次落盘的行接着跑，不用重新上传 BOM
                    lines = tr.load_batch_lines()
                if not isinstance(lines, list) or not lines:
                    raise ValueError("没有可执行的行")
                return self._send(
                    200,
                    tr.RUNNER.start_batch(
                        lines,
                        prefer_cheap=bool(body.get("prefer_cheap", True)),
                        max_candidates=int(body.get("max_candidates") or 3),
                        # resume 默认开启：接着上次的进度跑，不重复加购
                        resume=bool(body.get("resume", True)),
                        # dry_run=True：演练模式，跑完选型但不点加购（回归测速度用）
                        dry_run=bool(body.get("dry_run", False)),
                    ),
                )

            if path == "/api/taobao/batch-reset":
                # 只清进度文件，不动购物车 —— 界面会提示先清购物车再重头跑
                tr.clear_batch_progress()
                return self._send(200, {"ok": True, "progress": tr.batch_progress_summary()})

            if path == "/api/taobao/batch-stop":
                return self._send(200, tr.RUNNER.stop_batch())

            if path.startswith("/api/taobao/"):
                line = body.get("line")
                if not isinstance(line, dict):
                    raise ValueError("缺少 line")
                if path == "/api/taobao/search":
                    return self._send(200, tr.RUNNER.search(line, body.get("query")))
                if path == "/api/taobao/open":
                    return self._send(200, tr.RUNNER.open_item(line, int(body.get("index"))))
                if path == "/api/taobao/add":
                    qty = int(body.get("qty") or 0)
                    if qty < 1 or qty > 99999:
                        raise ValueError("qty 必须是 1–99999 的正整数")
                    selections = body.get("selections") or []
                    return self._send(
                        200,
                        tr.RUNNER.add_to_cart(
                            line,
                            int(body.get("index")),
                            selections,
                            qty,
                            auto_qty=bool(body.get("auto")),
                            pack_hint=body.get("pack_hint") or None,
                            price_hint=body.get("price_hint") or None,
                        ),
                    )

            return self._send(404, {"error": "Not found"})
        except tr.SessionError as exc:
            return self._send(409, {"error": str(exc), "code": exc.code, "detail": exc.detail})
        except ValueError as exc:
            # 前端是按 d.code 分支的（像 CART_REMOVE_AMBIGUOUS 这种参数错误
            # 以前只给 error 文本，前端拿不到码只能显示「未知错误」）。
            return self._send(400, {"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": f"服务端异常：{exc}"})


def _take_over_logs() -> None:
    """把 stdout/stderr 接到 runs/server.{out,err}.log。

    服务是用 pythonw / CREATE_NO_WINDOW 起的（不留黑框），这种情况下
    sys.stdout 是 None —— 所有 print 会直接把进程搞崩，所以启动第一件事
    就是把日志接管过来。launcher 的 cmd 重定向也可以退休了。
    """
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        out = open(RUNS / "server.out.log", "a", encoding="utf-8", errors="replace", buffering=1)
        err = open(RUNS / "server.err.log", "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = out
        sys.stderr = err
    except Exception:  # noqa: BLE001
        # 日志接管失败不能挡住服务本身
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main(argv=None):
    _take_over_logs()  # 必须在任何 print 之前
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SERVING http://{args.host}:{args.port}", flush=True)
    print(f"ROOT {ROOT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
