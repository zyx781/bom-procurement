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
import lcsc
import taobao_runner as tr

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SAMPLES = ROOT / "samples"
RUNS = Path(os.environ.get("TAOBAO_RUNS_DIR") or (ROOT / "runs")).resolve()
MAX_BODY = 25 * 1024 * 1024
VERSION_FILE = ROOT.parent / "VERSION"


def _app_version() -> str:
    """页脚显示的应用版本（唯一来源：仓库根目录的 VERSION 文件）。"""
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        return v or "?"
    except OSError:
        return "?"

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


def _int_field(body: dict, key: str, low: int, high: int, label: str | None = None) -> int:
    """把请求体里的整数字段取出来并校验范围。

    以前直接 `int(body.get("index"))`：字段没传时是 None，int(None) 抛 TypeError，
    被最外层 except 兜成 500「服务端异常」—— 前端只能显示一句含糊的报错。
    这里统一转成 ValueError（400 + 明确的 code）。
    """
    name = label or key
    raw = body.get(key)
    if raw is None or raw == "":
        raise ValueError(f"缺少 {name}")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None
    if val < low or val > high:
        raise ValueError(f"{name} 必须在 {low}–{high} 之间")
    return val


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

    def _download(self, code, data: bytes, ctype: str, filename: str):
        """带 Content-Disposition 的下载响应（导出 CSV 用）。"""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 中文文件名用 RFC 5987 的 filename*，老浏览器回退到 ASCII 名
        from urllib.parse import quote

        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"export.csv\"; filename*=UTF-8''{quote(filename)}",
        )
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

    def _batch_payload(self) -> dict:
        """批量状态 + 上次进度摘要（GET 与 SSE 共用一份，别让两条路走散了）。"""
        data = tr.RUNNER.batch_status()
        # 附带「上次进度」：前端据此把按钮改成「继续加购（从第 N 行）」。
        # 带上当前 BOM 指纹，换表后旧进度对外不可见（但文件保留，精准清车还要用）。
        data["progress"] = tr.batch_progress_summary(
            active_fingerprint=tr.active_bom_fingerprint()
        )
        return data

    def _batch_stream(self):
        """SSE：把批量状态推给页面。

        为什么不再只靠前端 setInterval 轮询：定时器在**后台标签页**会被浏览器
        限流（实测隐藏标签页里 3s 的 setInterval 能掉到 1 分钟一次），用户切去
        看淘宝、或者页面被别的窗口盖住时，进度条就"卡住不动"——看着像没跑，
        其实跑得好好的。改成服务端推送后，标签页是否在前台都不影响刷新。

        连接寿命 10 分钟就主动收尾（EventSource 会自动重连），避免长时间
        占着一个服务端线程。
        """
        import time as _time

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        # 万一以后挂在反向代理后面，别让代理把流攒起来
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        deadline = _time.monotonic() + 600
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while _time.monotonic() < deadline:
                try:
                    payload = self._batch_payload()
                except Exception as exc:  # noqa: BLE001
                    payload = {"error": f"状态读取失败：{exc}"}
                payload["streamed_at"] = _time.strftime("%H:%M:%S")
                body = json.dumps(payload, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
                # 跑批中 1 秒推一次；闲着 3 秒推一次就够（还能顺带发现会话掉线）
                _time.sleep(1.0 if (payload.get("running") or payload.get("error")) else 3.0)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 页面关了/刷新了，正常收尾
        except Exception as exc:  # noqa: BLE001
            print(f"[site] batch stream aborted: {exc}", flush=True)

    # ---------------- GET ----------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if path == "/api/health":
            return self._send(
                200,
                {
                    "ok": True,
                    "engine": engine.ENGINE_VERSION,
                    "offline": False,
                    # 前端页脚显示的版本号，不再在 HTML 里写死（写死过一次，
                    # 服务升到 0.9.6 页面还挂着 0.9.1，排查时白白误导半天）
                    "version": _app_version(),
                },
            )
        if path == "/api/last-analysis":
            # 刷新/新开标签页时恢复上次分析结果：否则执行区整块隐藏，
            # 页面回到空白首屏，看不到任何跑批状态。
            return self._send(200, tr.load_last_analysis())
        # 注意：/api/shutdown **不能**挂在 GET 上 —— 任意网页放一个
        # <img src="http://127.0.0.1:8765/api/shutdown"> 就能把服务关掉。
        # 只保留 POST + 同源校验（见 do_POST）。
        if path == "/api/session/status":
            return self._send(200, tr.RUNNER.status())
        if path == "/api/taobao/batch":
            return self._send(200, self._batch_payload())
        if path == "/api/taobao/batch/stream":
            return self._batch_stream()
        if path == "/api/taobao/batch/export.csv":
            # 跑完一轮要人工核对上百行，屏幕上滚太痛苦 —— 导出后和 BOM 并排 diff
            import csv
            import io
            from datetime import datetime

            st = tr.RUNNER.batch_status()
            rows = tr.batch_export_rows(st.get("results") or [])
            buf = io.StringIO()
            csv.writer(buf).writerows(rows)
            # utf-8-sig：Excel 双击打开不乱码
            body = ("\ufeff" + buf.getvalue()).encode("utf-8")
            name = f"batch-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
            return self._download(200, body, "text/csv; charset=utf-8", name)
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
                result = engine.parse_workbook(filename, raw)
                # 嘉立创EDA 导出的 BOM 每行带立创编号（Supplier Part），这里先数一下，
                # 让用户一眼看出「这张表能不能按编号核准」。
                result["lcsc_rows"] = sum(1 for r in (result.get("rows") or []) if r.get("lcsc"))
                return self._send(200, result)

            if path == "/api/analyze":
                rows = body.get("rows")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("没有可分析的行")
                # 分析前先用立创商城把「立创编号 / 完整型号」换成权威规格：
                # 编号命中 → 型号/封装/阻值/耐压/精度/库存都是确定的，不用再从字符串猜。
                # 查不到（离线、编号失效）就退回纯规则分析，绝不因为联网失败挡住整表。
                lcsc_stats = {"resolved": 0, "by_code": 0, "by_model": 0, "skipped": [], "failed": []}
                try:
                    lcsc_stats = lcsc.enrich_rows(
                        rows, allow_model=bool(body.get("lcsc_model", True))
                    )
                except Exception as exc:  # noqa: BLE001  立创挂了也必须能分析
                    print(f"[site] lcsc enrich skipped: {exc}", flush=True)
                result = dict(engine.analyze(rows))
                result["lcsc"] = lcsc_stats
                result["lcsc_summary"] = lcsc.summary_text(lcsc_stats)
                # 记住当前这份 BOM；换表后把上一批进度隐藏掉，否则前端会弹
                # 「上次已完成 N 行，接着跑」，实际一行都不会执行。
                fp = tr.set_active_bom(result.get("lines") or [])
                # 分析完就把「这一批要跑的行」落盘：单行执行（/api/taobao/run-line）
                # 和重启后续跑都从这份文件取行，不落盘的话它们会拿到上一次分析的
                # 旧参数（实测：刚读出「双排」的新结论，跑单行时用的还是旧行，
                # 于是把 1*40P 单排条加进了购物车）。
                tr.save_batch_lines(result.get("lines") or [])
                result["progress"] = tr.batch_progress_summary(active_fingerprint=fp)
                # 落一份，供刷新/新标签页恢复（前端 sessionStorage 只在本标签页有效）
                tr.save_last_analysis(dict(result, bom_fingerprint=fp), rows=rows)
                return self._send(200, result)

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
                source = str(body.get("source") or "").strip()
                dry = bool(body.get("dry"))
                # 只删「上一批加购进去的」：按商品 id 精确匹配 + 数量守卫，
                # 用户自己加的商品保持不动。dry=true 只返回计划、不点按钮。
                if source == "last_batch":
                    return self._send(
                        200, tr.RUNNER.cart_remove_last_batch(dry=dry)
                    )
                keyword = str(body.get("keyword") or "").strip()
                remove_all = bool(body.get("all"))
                if not keyword and not remove_all:
                    raise ValueError(
                        "必须给 keyword（标题关键字）、source=\"last_batch\" 或 all=true，"
                        "拒绝隐式清空购物车"
                    )
                if remove_all and str(body.get("confirm") or "") != "清空":
                    raise ValueError("清空整个购物车需要 confirm=\"清空\"")
                return self._send(200, tr.RUNNER.cart_remove(keyword=keyword or None, remove_all=remove_all))

            if path == "/api/lines/update":
                # 网页上的人工修改：确认放行（status=ready）/ 改搜索词（query_plan）。
                # 只按 index + line_no 双重定位、只覆盖白名单字段，改错的行改不进来。
                idx = _int_field(body, "index", 0, 99999, label="index")
                patch = body.get("patch")
                if not isinstance(patch, dict) or not patch:
                    raise ValueError("缺少 patch")
                patch = dict(patch)
                patch["line_no"] = _int_field(body, "line_no", 0, 99999, label="line_no")
                return self._send(200, tr.update_saved_line(idx, patch))

            if path == "/api/taobao/run-line":
                # 单行执行：个别行失败/改完搜索词后只跑这一行，不重复加购其它行
                _ln = body.get("line_no")
                _idx = body.get("index")
                if _ln is None and _idx is None:
                    raise ValueError("缺少 line_no 或 index")
                return self._send(
                    200,
                    tr.RUNNER.run_line(
                        line_no=_ln,
                        index=None if _idx is None else _int_field(body, "index", 0, 99999, label="index"),
                    ),
                )

            if path == "/api/taobao/batch-start":
                lines = body.get("lines")
                if (not isinstance(lines, list) or not lines) and bool(body.get("resume", True)):
                    # 服务重启后前端没重传行时，用上次落盘的行接着跑，不用重新上传 BOM。
                    # 但必须是同一份 BOM：否则会拿上一批的行去跑新表（或跑出测试数据）。
                    active_fp = tr.active_bom_fingerprint()
                    saved_fp = tr.load_batch_lines_meta().get("fingerprint") or ""
                    if active_fp and saved_fp and active_fp != saved_fp:
                        raise ValueError("检测到 BOM 已更换，请重新分析后再启动批量")
                    lines = tr.load_batch_lines()
                if not isinstance(lines, list) or not lines:
                    raise ValueError("没有可执行的行")
                # 重跑语义必须显式传，绝不靠"看着像想重跑"来猜：
                # redo=true 整批重跑（清进度），redo_lines=[2,15] 只重跑这几行。
                _redo_lines = body.get("redo_lines")
                if _redo_lines is not None and not isinstance(_redo_lines, list):
                    raise ValueError("redo_lines 必须是行号数组")
                _redo_nos = []
                for _x in _redo_lines or []:
                    try:
                        _redo_nos.append(int(_x))
                    except (TypeError, ValueError):
                        raise ValueError(f"redo_lines 里的行号不是整数：{_x!r}") from None
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
                        redo=bool(body.get("redo", False)),
                        redo_lines=_redo_nos,
                    ),
                )

            if path == "/api/taobao/batch-reset":
                # 只清进度文件，不动购物车 —— 界面会提示先清购物车再重头跑
                tr.clear_batch_progress()
                return self._send(
                    200,
                    {"ok": True, "progress": tr.batch_progress_summary(
                        active_fingerprint=tr.active_bom_fingerprint()
                    )},
                )

            if path == "/api/taobao/batch-stop":
                return self._send(200, tr.RUNNER.stop_batch())

            if path.startswith("/api/taobao/"):
                line = body.get("line")
                if not isinstance(line, dict):
                    raise ValueError("缺少 line")
                if path == "/api/taobao/search":
                    return self._send(200, tr.RUNNER.search(line, body.get("query")))
                if path == "/api/taobao/open":
                    return self._send(200, tr.RUNNER.open_item(line, _int_field(body, "index", 0, 999)))
                if path == "/api/taobao/add":
                    qty = _int_field(body, "qty", 1, 99999, label="qty")
                    selections = body.get("selections") or []
                    return self._send(
                        200,
                        tr.RUNNER.add_to_cart(
                            line,
                            _int_field(body, "index", 0, 999),
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


def _cleanup_old_runs(keep_days: int = 14) -> None:
    """清掉 runs/ 里过期的日志与截图（只动 .log / .jpg，不碰 json 进度）。

    一天跑一轮会产生几十份会话日志和上百张截图，长期只涨不降。
    默认保留 14 天，可用 BOM_KEEP_DAYS 调整（0 = 不清理）。
    """
    import time

    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    # 本批证据图例外：不管多久以前，只要进度里还点着名就不能删 ——
    # 用户点「删上次加购」时要对着这些图核对买了什么。
    try:
        keep_shots = tr.batch_screenshot_names()
    except Exception:  # noqa: BLE001
        keep_shots = set()
    removed = freed = 0
    for pattern in ("*.log", "*.jpg"):
        for path in RUNS.glob(pattern):
            if path.name in keep_shots:
                continue
            try:
                st = path.stat()
                if st.st_mtime < cutoff:
                    path.unlink()
                    removed += 1
                    freed += st.st_size
            except OSError:
                continue
    if removed:
        print(f"[site] 清理过期日志/截图 {removed} 个，释放 {freed / 1048576:.1f} MB（>{keep_days} 天）", flush=True)


def main(argv=None):
    _take_over_logs()  # 必须在任何 print 之前
    try:
        _cleanup_old_runs(int(os.environ.get("BOM_KEEP_DAYS", "14")))
    except Exception:  # noqa: BLE001
        pass
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
