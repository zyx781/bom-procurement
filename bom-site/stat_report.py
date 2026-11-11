"""生成 BOM 采购统计报告（含实付金额）。

数据来源：
  - runs/batch-progress.json  每行的状态 / 份数 / 到手只数 / 实付金额
  - 本机服务 /api/parse + /api/analyze  拿类别、封装、型号、用量

用法：
  python stat_report.py                 # 用默认 BOM，输出到 采购统计-<时间>.html
  python stat_report.py --bom D:/x.xlsx --out report.html
  python stat_report.py --offline       # 服务没开时只按进度文件出统计（无类别）
"""

import argparse
import base64
import collections
import datetime
import html
import json
import pathlib
import sys
import urllib.request

BASE = pathlib.Path(__file__).parent
PROGRESS = BASE / "runs" / "batch-progress.json"
DEFAULT_BOM = pathlib.Path(r"C:\Users\zyx\Desktop\BOM_MainControl_MainControl_2026-11-06.xlsx")
API = "http://127.0.0.1:8765"

CAT_CN = {
    "passive": "阻容感（被动件）",
    "connector": "连接器 / 排针",
    "ic": "芯片 IC",
    "power": "功率器件",
    "other": "其他（测试点 / 模块）",
}


def esc(v):
    return html.escape(str(v if v is not None else ""))


def api_call(path, payload, timeout=180):
    # 本机有 HTTP_PROXY，必须绕开，否则 127.0.0.1 会被代理接管
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def load_lines(bom: pathlib.Path):
    """解析 + 分析 BOM，返回 {line_no: line}。"""
    raw = bom.read_bytes()
    parsed = api_call(
        "/api/parse",
        {"filename": bom.name, "data": base64.b64encode(raw).decode()},
    )
    result = api_call("/api/analyze", {"rows": parsed["rows"]})
    return {int(l["line_no"]): l for l in result["lines"]}


def load_progress():
    try:
        data = json.loads(PROGRESS.read_text(encoding="utf-8"))
        return data.get("lines") or {}, data.get("updated_at") or ""
    except Exception:
        return {}, ""


def money(v):
    if v is None:
        return "—"
    return "¥" + f"{float(v):.2f}".rstrip("0").rstrip(".")


def build(bom, offline=False):
    prog, updated = load_progress()
    lines = {}
    if not offline:
        try:
            lines = load_lines(bom)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 读取 BOM 失败（{exc}），仅按进度文件统计", file=sys.stderr)

    rows = []
    for key, v in prog.items():
        try:
            ln = int(key)
        except ValueError:
            continue
        meta = lines.get(ln) or {}
        cat = meta.get("category") or "other"
        rows.append(
            {
                "line_no": ln,
                "status": v.get("status") or "",
                "headline": v.get("headline") or ((meta.get("decision") or {}).get("headline") or ""),
                "qty": v.get("qty") if v.get("qty") is not None else meta.get("qty"),
                "cost": v.get("cost"),
                "price": v.get("final_price") if v.get("final_price") is not None else v.get("price"),
                "pack": v.get("pack_size"),
                "pcs": v.get("actual_pcs"),
                "message": v.get("message") or "",
                "cat": cat,
                "mpn": meta.get("mpn") or "",
                "package": meta.get("package") or "",
            }
        )
    rows.sort(key=lambda r: r["line_no"])

    # 进度文件只记 ok/failed/uncertain，被验证中断而「没轮到」的行根本不在里面。
    # 能拿到 BOM 行时，把这些补成 unrun，否则报告会漏掉一截（看起来像 106 行就全了）。
    if lines:
        seen = {r["line_no"] for r in rows}
        for ln, meta in lines.items():
            if ln in seen:
                continue
            rows.append(
                {
                    "line_no": ln,
                    "status": "unrun",
                    "headline": (meta.get("decision") or {}).get("headline") or meta.get("mpn") or "",
                    "qty": meta.get("qty"),
                    "cost": None,
                    "price": None,
                    "pack": None,
                    "pcs": None,
                    "message": "未执行（批量中断，没有记录）",
                    "cat": meta.get("category") or "other",
                    "mpn": meta.get("mpn") or "",
                    "package": meta.get("package") or "",
                }
            )
        rows.sort(key=lambda r: r["line_no"])

    ok = [r for r in rows if r["status"] == "ok"]
    failed = [r for r in rows if r["status"] == "failed"]
    # uncertain：点过「加入购物车」但页面没给确认信号 —— 可能已经在车里，必须人工核对，
    # 不能和「没轮到」的未执行行混在一起（否则报告会显示成「未跑」，看起来像漏买）。
    uncertain = [r for r in rows if r["status"] == "uncertain"]
    other = [r for r in rows if r["status"] not in ("ok", "failed", "uncertain")]

    total_qty_all = sum(m.get("qty") or 0 for m in lines.values()) if lines else None
    ok_qty = sum(r["qty"] or 0 for r in ok)
    cost_total = round(sum(r["cost"] for r in ok if isinstance(r["cost"], (int, float))), 2)
    cost_lines = sum(1 for r in ok if isinstance(r["cost"], (int, float)))
    no_price = len(ok) - cost_lines

    cats = collections.OrderedDict()
    for r in rows:
        c = cats.setdefault(r["cat"], {"n": 0, "ok": 0, "fail": 0, "other": 0, "qty": 0, "cost": 0.0, "priced": 0})
        c["n"] += 1
        c["qty"] += r["qty"] or 0
        if r["status"] == "ok":
            c["ok"] += 1
            if isinstance(r["cost"], (int, float)):
                c["cost"] += r["cost"]
                c["priced"] += 1
        elif r["status"] == "failed":
            c["fail"] += 1
        else:
            c["other"] += 1

    over = [
        r
        for r in ok
        if r["pcs"] and r["qty"] and r["pcs"] > r["qty"] * 1.5
    ]
    over.sort(key=lambda r: -(r["pcs"] / max(r["qty"], 1)))

    return {
        "rows": rows,
        "ok": ok,
        "failed": failed,
        "uncertain": uncertain,
        "other": other,
        "total_lines": len(rows),
        "total_qty_all": total_qty_all,
        "ok_qty": ok_qty,
        "cost_total": cost_total,
        "cost_lines": cost_lines,
        "no_price": no_price,
        "cats": cats,
        "over": over,
        "updated": updated,
    }


def render(st, out_path):
    total = st["total_lines"] or 1
    okn, fail, skip = len(st["ok"]), len(st["failed"]), len(st["other"])
    unsure = len(st.get("uncertain") or [])
    unsure_html = "".join(
        f"<tr><td class='mono num'>#{r['line_no']}</td><td>{esc(r['headline'])}</td>"
        f"<td class='mono mute'>{esc(r['mpn'])}</td><td>{esc(r['package'])}</td>"
        f"<td class='mute'>{esc(r['message'][:60])}</td></tr>"
        for r in (st.get("uncertain") or [])
    )
    pct = okn / total * 100
    qty_pct = (st["ok_qty"] / st["total_qty_all"] * 100) if st["total_qty_all"] else None

    cat_rows = []
    for cat, c in st["cats"].items():
        rate = c["ok"] / c["n"] * 100 if c["n"] else 0
        cat_rows.append(
            "<tr>"
            f"<td class='name'>{esc(CAT_CN.get(cat, cat))}</td>"
            f"<td class='num'>{c['n']}</td><td class='num ok'>{c['ok']}</td>"
            f"<td class='num bad'>{c['fail']}</td><td class='num mute'>{c['other']}</td>"
            f"<td class='num'>{c['qty']}</td><td class='num money'>{money(c['cost']) if c['priced'] else '—'}</td>"
            f"<td><div class='mini'><i style='width:{rate:.0f}%'></i></div><span class='pct'>{rate:.0f}%</span></td>"
            "</tr>"
        )

    def money_rows(items):
        return "".join(
            f"<tr><td class='mono num'>#{r['line_no']}</td><td>{esc(r['headline'])}</td>"
            f"<td class='num'>{r['qty'] if r['qty'] is not None else ''}</td>"
            f"<td class='num'>{r['pack'] if r['pack'] and r['pack'] > 1 else ''}</td>"
            f"<td class='num'>{r['pcs'] if r['pcs'] else ''}</td>"
            f"<td class='num money'>{money(r['cost']) if r['cost'] is not None else '<span class=mute>未记价</span>'}</td></tr>"
            for r in items
        )

    groups = collections.OrderedDict()
    for r in st["failed"]:
        m = r["message"]
        if "division by zero" in m:
            k = "程序异常（已修复，可重跑）"
        elif "没有通过硬门槛" in m:
            k = "全网无满足硬门槛的候选"
        elif "SKU 无法确认" in m:
            k = "SKU 无法确认（需人工选规格）"
        elif "verification" in m.lower() or "CAPTCHA" in m:
            k = "撞淘宝验证，中断"
        else:
            k = "其他"
        groups.setdefault(k, []).append(r)

    fail_html = ""
    for k, items in groups.items():
        body = "".join(
            f"<tr><td class='mono num'>#{r['line_no']}</td><td>{esc(r['headline'])}</td>"
            f"<td class='num'>{r['qty'] if r['qty'] is not None else ''}</td>"
            f"<td class='mute'>{esc(r['message'][:120])}</td></tr>"
            for r in items
        )
        fail_html += (
            f"<div class='grp'><div class='grp-h'><span>{esc(k)}</span><b>{len(items)} 行</b></div>"
            f"<table class='t'><tbody>{body}</tbody></table></div>"
        )

    other_html = "".join(
        f"<tr><td class='mono num'>#{r['line_no']}</td><td>{esc(r['headline'])}</td>"
        f"<td class='mono mute'>{esc(r['mpn'])}</td><td>{esc(r['package'])}</td>"
        f"<td class='mute'>{esc(r['message'][:60])}</td></tr>"
        for r in st["other"]
    )

    over_html = "".join(
        f"<tr><td class='mono num'>#{r['line_no']}</td><td>{esc(r['headline'])}</td>"
        f"<td class='num'>{r['qty']}</td><td class='num'>{r['pcs']}</td>"
        f"<td class='num bad'>{r['pcs'] / max(r['qty'], 1):g}×</td>"
        f"<td class='num money'>{money(r['cost']) if r['cost'] is not None else '—'}</td></tr>"
        for r in st["over"]
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    avg = st["cost_total"] / st["cost_lines"] if st["cost_lines"] else None

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOM 采购统计 · {now}</title>
<style>
:root{{--bg:#f3f5f8;--panel:#fff;--line:#e4e8ee;--text:#0f172a;--muted:#64748b;--m2:#94a3b8;
--accent:#2563eb;--ok:#0f9d68;--warn:#c2760a;--warnbg:#fdf4e3;--bad:#d93636;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 22px 60px}}
header{{background:linear-gradient(135deg,#1e40af,#2563eb 55%,#3b82f6);color:#fff;border-radius:18px;padding:26px 28px;box-shadow:0 8px 26px rgba(37,99,235,.22)}}
header h1{{margin:0;font-size:22px;letter-spacing:.3px}}
header p{{margin:6px 0 0;opacity:.86;font-size:13px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:20px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:0 1px 3px rgba(15,23,42,.04)}}
.kpi .n{{font-size:28px;font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}}
.kpi .l{{color:var(--muted);font-size:12.5px;margin-top:2px}}
.kpi .sub{{color:var(--m2);font-size:11.5px;margin-top:3px}}
.kpi.a .n{{color:var(--accent)}} .kpi.o .n{{color:var(--ok)}} .kpi.b .n{{color:var(--bad)}} .kpi.w .n{{color:var(--warn)}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:18px;box-shadow:0 1px 3px rgba(15,23,42,.04)}}
h2{{font-size:15px;margin:0 0 14px;display:flex;align-items:center;gap:8px}}
h2::before{{content:"";width:3px;height:15px;border-radius:2px;background:var(--accent)}}
h2 small{{font-weight:400;color:var(--muted);font-size:12.5px;margin-left:auto}}
.bar{{height:14px;border-radius:8px;background:#eef1f5;display:flex;overflow:hidden;margin:4px 0 10px}}
.bar i{{display:block;height:100%}}
.bar .s{{background:linear-gradient(90deg,#12b981,#0f9d68)}}
.bar .f{{background:#f0a3a3}}
.bar .k{{background:#cfd7e3}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12.5px}}
.legend b{{color:var(--text);font-weight:600}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:1px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
.t th{{text-align:left;color:var(--muted);font-weight:600;font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
.t td{{padding:8px 10px;border-bottom:1px solid #f1f4f8;vertical-align:top}}
.t tr:last-child td{{border-bottom:none}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.money{{font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600}}
.mono{{font-family:"Cascadia Code",Consolas,monospace;font-size:12.5px}}
.name{{font-weight:600;white-space:nowrap}}
.ok{{color:var(--ok);font-weight:600}} .bad{{color:var(--bad);font-weight:600}} .mute{{color:var(--muted)}}
.mini{{display:inline-block;width:80px;height:6px;border-radius:4px;background:#eef1f5;overflow:hidden;vertical-align:middle;margin-right:7px}}
.mini i{{display:block;height:100%;background:linear-gradient(90deg,#12b981,#0f9d68)}}
.pct{{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}}
.grp{{border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:12px}}
.grp-h{{display:flex;align-items:center;gap:10px;padding:9px 14px;background:#f8fafc;border-bottom:1px solid var(--line);font-size:13px;font-weight:600}}
.grp-h b{{margin-left:auto;color:var(--bad);font-size:12.5px}}
.note{{background:var(--warnbg);border-left:3px solid var(--warn);border-radius:8px;padding:12px 14px;color:#7a4a06;font-size:13px;line-height:1.7}}
footer{{text-align:center;color:var(--m2);font-size:12px;margin-top:26px}}
@media (max-width:760px){{.kpis{{grid-template-columns:repeat(2,1fr)}}.wrap{{padding:16px 12px 40px}}}}
</style></head><body><div class="wrap">
<header>
  <h1>BOM 采购统计</h1>
  <p>共 {st['total_lines']} 种器件{f" / 合计 {st['total_qty_all']} 颗" if st['total_qty_all'] else ""} · 统计于 {now}{f" · 进度更新于 {st['updated']}" if st['updated'] else ""}</p>
</header>

<div class="kpis">
  <div class="kpi o"><div class="n">{money(st['cost_total'])}</div><div class="l">已下单合计金额</div><div class="sub">{st['cost_lines']} 行有实付价 · 均价 {money(avg) if avg else '—'}</div></div>
  <div class="kpi a"><div class="n">{okn}<span style="font-size:15px;color:var(--muted)"> / {st['total_lines']}</span></div><div class="l">已加入购物车（行）</div><div class="sub">按行 {pct:.1f}%{f" · 按用量 {qty_pct:.1f}%" if qty_pct else ""}</div></div>
  <div class="kpi b"><div class="n">{fail}</div><div class="l">失败需处理</div><div class="sub">{'见下方明细' if fail else '无'}</div></div>
  <div class="kpi w"><div class="n">{unsure}</div><div class="l">加购待核对</div><div class="sub">{'已点加购但页面没确认' if unsure else '无'}</div></div>
  <div class="kpi w"><div class="n">{skip}</div><div class="l">未执行</div><div class="sub">{'多为验证中断' if skip else '无'}</div></div>
</div>

<section>
  <h2>完成度 <small>用量 {st['ok_qty']}{f" / {st['total_qty_all']}" if st['total_qty_all'] else ''} 颗</small></h2>
  <div class="bar">
    <i class="s" style="width:{pct:.1f}%"></i><i class="f" style="width:{fail/total*100:.1f}%"></i><i class="k" style="width:{skip/total*100:.1f}%"></i>
  </div>
  <div class="legend">
    <span><span class="dot" style="background:#0f9d68"></span>已下单 <b>{okn}</b> 行 / {st['ok_qty']} 颗</span>
    <span><span class="dot" style="background:#f0a3a3"></span>失败 <b>{fail}</b> 行</span>
    <span><span class="dot" style="background:#cfd7e3"></span>未执行 <b>{skip}</b> 行</span>
    {f"<span title='续跑时没补历史金额'>未记价 <b>{st['no_price']}</b> 行</span>" if st['no_price'] else ""}
  </div>
</section>

<section>
  <h2>按类别拆分</h2>
  <table class="t"><thead><tr><th>类别</th><th class="num">行数</th><th class="num">成功</th><th class="num">失败</th><th class="num">未跑</th><th class="num">用量</th><th class="num">金额</th><th>成功率</th></tr></thead>
  <tbody>{''.join(cat_rows)}</tbody></table>
</section>

<section>
  <h2>已下单明细 <small>{okn} 行 · 合计 {money(st['cost_total'])}</small></h2>
  <table class="t"><thead><tr><th class="num">行</th><th>器件</th><th class="num">需求</th><th class="num">每份</th><th class="num">到手</th><th class="num">金额</th></tr></thead>
  <tbody>{money_rows(st['ok'])}</tbody></table>
</section>

<section>
  <h2>加购待核对 <small>{unsure} 行</small></h2>
  {unsure_html or "<div class='mute'>无</div>"}
</section>

<section>
  <h2>失败明细 <small>{fail} 行</small></h2>
  {fail_html or "<div class='mute'>无</div>"}
</section>

<section>
  <h2>未执行 <small>{skip} 行</small></h2>
  <table class="t"><thead><tr><th class="num">行</th><th>器件</th><th>型号</th><th>封装</th><th>原因</th></tr></thead>
  <tbody>{other_html or "<tr><td colspan='5' class='mute'>无</td></tr>"}</tbody></table>
</section>

<section>
  <h2>超量采购 <small>{len(st['over'])} 行：到手远多于需求</small></h2>
  <table class="t"><thead><tr><th class="num">行</th><th>器件</th><th class="num">需求</th><th class="num">到手</th><th class="num">倍数</th><th class="num">金额</th></tr></thead>
  <tbody>{over_html or "<tr><td colspan='6' class='mute'>无</td></tr>"}</tbody></table>
</section>

<footer>BOM 选型台 · 数据来自 runs/batch-progress.json</footer>
</div></body></html>"""

    out_path.write_text(doc, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bom", default=str(DEFAULT_BOM))
    ap.add_argument("--out", default="")
    ap.add_argument("--offline", action="store_true", help="不连服务，仅按进度文件统计")
    args = ap.parse_args()

    bom = pathlib.Path(args.bom)
    if not bom.exists() and not args.offline:
        print(f"[warn] BOM 不存在：{bom}", file=sys.stderr)

    st = build(bom, offline=args.offline)
    if not st["rows"]:
        print("进度文件里还没有记录：runs/batch-progress.json 是空的。")
        return 1

    out = pathlib.Path(args.out) if args.out else (
        BASE / f"采购统计-{datetime.datetime.now():%Y%m%d-%H%M}.html"
    )
    render(st, out)
    print(f"报告：{out}")
    print(
        f"行数 {st['total_lines']}｜成功 {len(st['ok'])}｜待核对 {len(st.get('uncertain') or [])}"
        f"｜失败 {len(st['failed'])}｜未执行 {len(st['other'])}｜合计 {money(st['cost_total'])}"
        f"（{st['cost_lines']} 行有价，{st['no_price']} 行未记价）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
