#!/usr/bin/env python3
"""「上次分析」不再自动铺台面（2026-11-18 用户反馈）。

现象：一打开选型台，出来的就是上次上传的那份记录，看着像自己刚传的。
根因：服务端把分析结果和当前 BOM 指纹落了盘，前端启动时无条件接上。

规则改成：
* 只有**正在跑批**时才自动接上（刷新/新标签页要接上现场）；
* 平时只在投放区挂一条「上次分析 · N 行 · 时间」，点「继续查看」才打开；
* 点「清空」删掉落盘记录（购物车与跑批进度不动），跑批中服务端拒绝。

这组测试用临时目录，不碰真实 runs/last-analysis.json。
"""

import json
import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).parent
sys.path.insert(0, str(BASE))

import taobao_runner as tr  # noqa: E402

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


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bom-resume-"))
    orig = (tr.LAST_ANALYSIS_FILE, tr.ACTIVE_BOM_FILE, tr.ACTIVE_BOM_FINGERPRINT)
    tr.LAST_ANALYSIS_FILE = tmp / "last-analysis.json"
    tr.ACTIVE_BOM_FILE = tmp / "active-bom.json"
    tr.ACTIVE_BOM_FINGERPRINT = "fp-test"
    try:
        tr.ACTIVE_BOM_FILE.write_text(
            json.dumps({"fingerprint": "fp-test", "updated_at": "2026-11-18T11:24:10"}),
            encoding="utf-8",
        )
        tr.LAST_ANALYSIS_FILE.write_text(
            json.dumps(
                {
                    "saved_at": "2026-11-18T11:24:10",
                    "result": {
                        "summary": {"total": 1, "ready": 1, "needs_user": 0},
                        "lines": [{"line_no": 1, "status": "ready"}],
                        "bom_fingerprint": "fp-test",
                    },
                    "rows": [{"line_no": 1, "desc": "10k"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        d = tr.load_last_analysis()
        check("同一份 BOM：能读回上次分析", bool(d.get("result")), str(d)[:120])
        check("带 saved_at，前端能标时间", d.get("saved_at") == "2026-11-18T11:24:10")

        check("清空返回 True（真删了东西）", tr.clear_last_analysis() is True)
        check("last-analysis.json 已删", not tr.LAST_ANALYSIS_FILE.exists())
        check("active-bom.json 已删", not tr.ACTIVE_BOM_FILE.exists())
        check("清空后读回空", tr.load_last_analysis() == {})
        check("再清一次返回 False（没东西可删）", tr.clear_last_analysis() is False)
        check("内存里的指纹也清了", tr.ACTIVE_BOM_FINGERPRINT == "")

        # 静态约束：前端只在 running 时自动接上，其余走提示条
        app = (BASE / "static" / "app.js").read_text(encoding="utf-8")
        html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
        css = (BASE / "static" / "style.css").read_text(encoding="utf-8")
        srv = (BASE / "server.py").read_text(encoding="utf-8")
        check("前端有「上次分析」提示条逻辑", "function showResumeBar" in app)
        check("只有 running 才自动接上", "if (b && b.running) return applyRestore" in app)
        check("提示条有继续查看/清空按钮", 'id="btn-resume-open"' in html and 'id="btn-resume-clear"' in html)
        check("提示条样式存在", ".resume{" in css)
        check("服务端有清空路由", '"/api/last-analysis/clear"' in srv)
        check("跑批中拒绝清空", "正在跑批，先停批再清空" in srv)
        check("开始分析新清单会收起提示条", "dismissResume(); // 开始分析新清单" in app)
    finally:
        tr.LAST_ANALYSIS_FILE, tr.ACTIVE_BOM_FILE, tr.ACTIVE_BOM_FINGERPRINT = orig
        for p in (tmp / "last-analysis.json", tmp / "active-bom.json"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
