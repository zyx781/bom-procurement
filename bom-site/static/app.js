/* BOM 选型台 · 前端（无框架、无外部依赖） */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const state = { rows: null, result: null, activeIndex: -1, lastScore: null, lastScoreIndex: -1, sets: 1 };
  const exec = { lineIndex: -1, search: null, chosen: null, open: null, result: null };
  let sessionReady = false;
  let batchRunning = false;
  let pollTimer = null;
  let serviceDown = false; // 「退出服务」后停止轮询，避免满屏报错

  async function shutdownService() {
    if (
      !window.confirm(
        "关闭本地服务和淘宝浏览器？\n\n购物车里的东西不受影响（在淘宝里）。\n下次要用，重新双击「启动BOM选型台.bat」即可。"
      )
    )
      return;
    const btn = $("#btn-shutdown");
    if (btn) btn.disabled = true;
    try {
      await api("/api/shutdown", {});
    } catch (e) {
      /* 服务可能已经开始退了，连不上是正常的 */
    }
    serviceDown = true;
    if (pollTimer) clearInterval(pollTimer);
    document.body.insertAdjacentHTML(
      "beforeend",
      `<div class="byebye">
         <svg class="mark" viewBox="0 0 28 28" aria-hidden="true" focusable="false">
           <rect x="8.5" y="8.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/>
           <path d="M11 8.5V4.5M17 8.5V4.5M11 23.5v-4M17 23.5v-4M8.5 11h-4M8.5 17h-4M23.5 11h-4M23.5 17h-4"
                 stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
         </svg>
         <h1>服务已关闭</h1>
         <p>本地网站和淘宝浏览器都停了，这个页面可以直接关掉。</p>
         <p class="fineprint">下次使用，双击「启动BOM选型台.bat」。</p>
       </div>`
    );
  }

  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);

  const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString("zh-CN"));
  const fmtMoney = (n) => (n == null ? "价格未知" : "¥" + Number(n).toFixed(2).replace(/\.00$/, ""));

  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 1600);
  }

  function setStatus(msg, cls = "") {
    const el = $("#input-status");
    el.textContent = msg || "";
    el.className = "status" + (cls ? " " + cls : "");
  }

  async function api(path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      throw new Error("服务返回了非 JSON 内容（HTTP " + res.status + "）");
    }
    if (!res.ok) {
      const err = new Error(data && data.error ? data.error : "HTTP " + res.status);
      if (data) {
        err.code = data.code;
        err.detail = data.detail;
      }
      throw err;
    }
    return data;
  }

  function bytesToBase64(bytes) {
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  /* ---------- 读入 ---------- */

  async function parseFile(file) {
    setStatus("读取 " + file.name + " …", "loading");
    const buf = new Uint8Array(await file.arrayBuffer());
    const parsed = await api("/api/parse", { filename: file.name, data: bytesToBase64(buf) });
    state.rows = parsed.rows;
    $("#bom-text").value = "";
    setStatus(
      `已解析 ${parsed.rows.length} 行（${file.name}）` +
        (parsed.warnings && parsed.warnings.length ? "；" + parsed.warnings.join("；") : ""),
      ""
    );
    await analyze(parsed.rows);
  }

  async function parsePasted(text) {
    const bytes = new TextEncoder().encode(text);
    const parsed = await api("/api/parse", { filename: "pasted.csv", data: bytesToBase64(bytes) });
    state.rows = parsed.rows;
    return parsed;
  }

  // 采购套数：BOM 的用量是「一套板子」的量，做几套就整体乘几
  function scaleRows(rows, sets) {
    const n = Math.max(1, Number(sets) || 1);
    if (n === 1) return rows;
    return rows.map((r) => ({ ...r, qty: (Number(r.qty) || 0) * n }));
  }

  async function analyze(rows) {
    const btn = $("#btn-analyze");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "分析中…";
    setStatus("正在按 SKILL 规则锁定规格…", "loading");
    try {
      const result = await api("/api/analyze", { rows: scaleRows(rows, state.sets) });
      state.result = result;
      // 换了一份 BOM：立刻用服务端算过的进度摘要，别让旧的「已完成 N 行」
      // 继续把按钮写成「继续加购」、把人骗进一次整批空跑。
      if (result.progress) batchProgress = result.progress;
      state.activeIndex = result.lines.length ? 0 : -1;
      state.lastScore = null;
      render();
      saveState();
      setStatus(`完成：${result.summary.total} 行，${result.summary.ready} 行可直接执行，${result.summary.needs_user} 行待确认。`);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  function saveState() {
    try {
      sessionStorage.setItem(
        "bom.result",
        JSON.stringify({ rows: state.rows, result: state.result, sets: state.sets, saved_at: Date.now() })
      );
    } catch {
      /* 存储满了就放弃缓存 */
    }
  }

  function restoreState() {
    try {
      const raw = sessionStorage.getItem("bom.result");
      if (!raw) return false;
      const saved = JSON.parse(raw);
      if (!saved || !saved.result || !Array.isArray(saved.result.lines) || !saved.result.lines.length) return false;
      state.rows = saved.rows || null;
      state.result = saved.result;
      state.sets = Number(saved.sets) || 1;
      state.activeIndex = 0;
      state.lastScore = null;
      render();
      setStatus(`已恢复上次分析结果：${saved.result.summary.total} 行（刷新页面不用重传 BOM）。`);
      return true;
    } catch {
      return false;
    }
  }

  /* ---------- 渲染主结果 ---------- */

  function render() {
    $("#result-panel").classList.remove("hidden");
    $("#score-panel").classList.remove("hidden");
    $("#exec-panel").classList.remove("hidden");
    const setsInput = $("#sets-input");
    if (setsInput) setsInput.value = state.sets || 1;
    renderSummary();
    renderLineList();
    renderScoreSelect();
    renderExecSelect();
    const idx = state.activeIndex >= 0 ? state.activeIndex : 0;
    if (state.result.lines.length) renderDetail(idx);
    pollSession();
  }

  function renderSummary() {
    const r = state.result;
    const s = r.summary;
    const cats = Object.entries(r.categories || {})
      .map(([k, v]) => `${catLabel(k)} ${v}`)
      .join("，");
    // 立创核准行数：有编号的按编号，没编号的按完整型号，分开数
    const lcst = r.lcsc || {};
    const lcCode = lcst.by_code || 0;
    const lcModel = lcst.by_model || 0;
    const lcChip =
      lcCode + lcModel > 0
        ? `<div class="chip ok" title="${esc(r.lcsc_summary || "")}"><b>${lcCode + lcModel}</b>立创核准</div>`
        : "";
    let html = `
      <div class="chip"><b>${s.total}</b>行</div>
      <div class="chip ok"><b>${s.ready}</b>可执行</div>
      <div class="chip warn"><b>${s.needs_user}</b>待确认</div>
      <div class="chip ${s.critical ? "bad" : ""}"><b>${s.critical}</b>关键件</div>
      ${lcChip}
      <div class="chip">${esc(cats)}</div>`;
    $("#summary").innerHTML = html;

    const old = $("#summary").nextElementSibling;
    if (old && old.classList.contains("warnbar")) old.remove();
    if (r.warnings && r.warnings.length) {
      const div = document.createElement("div");
      div.className = "warnbar";
      div.innerHTML = "BOM 检查提示：<ul>" + r.warnings.map((w) => `<li>${esc(w)}</li>`).join("") + "</ul>";
      $("#summary").insertAdjacentElement("afterend", div);
    }
  }

  function catLabel(c) {
    return { passive: "被动件", ic: "芯片", power: "功率", connector: "连接器", module: "模组", other: "其他" }[c] || c;
  }

  // 封装列只留第一个词：「0603 (1608)」→「0603」，宽度才够排五列
  function shortPkg(pkg) {
    return String(pkg || "").trim().split(/[\s(（]/)[0] || "—";
  }

  function renderLineList() {
    const lines = state.result.lines;
    const head = `<div class="ledger-head"><span>行</span><span>用料</span><span>封装</span><span>数量</span><span>状态</span></div>`;
    $("#line-list").innerHTML =
      head +
      lines
        .map((ln, i) => {
          const status = ln.status === "ready" ? "ready" : "needs_user";
          const critical = ln.criticality === "critical" ? `<span class="li-flag">关键</span>` : "";
          return `<button class="line-item ${i === state.activeIndex ? "active" : ""}" data-idx="${i}">
          <span class="li-no">${String(ln.line_no ?? i + 1).padStart(2, "0")}</span>
          <span class="li-body">
            <span class="li-title">${esc(ln.decision.headline || ln.mpn || "未识别")}${critical}</span>
            <span class="li-sub">${esc(ln.mpn || catLabel(ln.category))}${ln.lcsc && ln.lcsc.code ? ` · ${esc(ln.lcsc.code)}` : ""}</span>
          </span>
          <span class="li-pkg">${esc(shortPkg(ln.package))}</span>
          <span class="li-qty">${ln.qty ?? "—"}</span>
          <span class="badge ${status}">${status === "ready" ? "可执行" : "待确认"}</span>
        </button>`;
        })
        .join("");

    $("#line-list").querySelectorAll("[data-idx]").forEach((btn) => {
      btn.addEventListener("click", () => renderDetail(Number(btn.dataset.idx)));
    });
  }

  function renderDetail(idx) {
    state.activeIndex = idx;
    const ln = state.result.lines[idx];
    if (!ln) return;
    $("#line-list").querySelectorAll(".line-item").forEach((b, i) => {
      b.classList.toggle("active", i === idx);
    });

    const d = ln.decision;
    const statusBadge =
      ln.status === "ready"
        ? `<span class="badge">可执行</span>`
        : `<span class="badge needs_user">待确认</span>`;
    const critBadge =
      ln.criticality === "critical" ? `<span class="badge critical">关键件，G3 逐行</span>` : "";
    const meta = [
      ln.mpn ? `<span>型号 <b>${esc(ln.mpn)}</b></span>` : "",
      ln.qty ? `<span>数量 <b>${esc(ln.qty)}</b></span>` : `<span>数量 <b>缺失</b></span>`,
      ln.package ? `<span>封装 <b>${esc(ln.package)}</b></span>` : "",
      ln.refs ? `<span>位号 <b>${esc(ln.refs)}</b></span>` : "",
      ln.brand ? `<span>品牌 <b>${esc(ln.brand)}</b></span>` : "",
      `<span>替代 <b>${ln.substitution_allowed ? "允许（须 G2）" : "不允许"}</b></span>`,
    ]
      .filter(Boolean)
      .join("");

    const blocking =
      ln.blocking && ln.blocking.length
        ? `<div class="alert"><b>需要先补齐信息：</b>${ln.blocking.map((b) => esc(b)).join("；")}` +
          (ln.missing && ln.missing.length ? `（缺：${ln.missing.map(esc).join("、")}）` : "") +
          `</div>`
        : "";

    const gates = (d.buy_spec || []).map((g) => `<li>${esc(g)}</li>`).join("");
    const queries = (d.search_queries || [])
      .map(
        (q) => `<div class="query"><code>${esc(q)}</code><button type="button" data-copy="${esc(
          q
        )}">复制</button></div>`
      )
      .join("");
    const verify = (d.must_verify || []).map((v) => `<li>${esc(v)}</li>`).join("");
    const risks = (d.risk_notes || [])
      .map(
        (r) => `<div class="risk"><span class="code">${esc(r.code || "R")}</span><span>${esc(r.text)}</span></div>`
      )
      .join("");

    // 立创核准：把「编号 → 型号/封装/参数/库存」原样摆出来。
    // 按编号核对的用绿色左边框；按型号核对的用棕色，因为型号是 BOM 自己写的，
    // 只证明「这个型号是什么」，不证明「设计上就该用它」。
    const lc = ln.lcsc || null;
    let lcBlock = "";
    if (lc) {
      const byModel = lc.matched_by === "model";
      const attrs = Object.entries(lc.attrs || {})
        .slice(0, 10)
        .map(([k, v]) => `<span><i>${esc(k)}</i>${esc(v)}</span>`)
        .join("");
      lcBlock = `
      <div class="lcsc-box ${byModel ? "by-model" : "by-code"}">
        <div class="lcsc-head">
          <span class="lcsc-tag">${byModel ? "按型号核对" : "立创编号核准"}</span>
          <b>${esc(lc.code || "—")}</b>
          <span>${esc(lc.model || "")}</span>
          ${lc.brand ? `<span>${esc(lc.brand)}</span>` : ""}
          ${lc.package ? `<span>${esc(lc.package)}</span>` : ""}
          ${lc.stock != null ? `<span>库存 ${esc(String(lc.stock))}</span>` : ""}
          ${lc.packet ? `<span>${esc(lc.packet)}</span>` : ""}
        </div>
        ${attrs ? `<div class="lcsc-attrs">${attrs}</div>` : ""}
      </div>`;
    }

    $("#line-detail").innerHTML = `
      <div class="detail-head">
        <div>
          <div class="kicker"><span>第 ${ln.line_no} 行</span><span>${esc(
      catLabel(ln.category)
    )}</span>${ln.params_text ? `<span>${esc(ln.params_text)}</span>` : ""}</div>
          <h3>${esc(d.headline)}</h3>
        </div>
        <div class="detail-badges">${statusBadge}${critBadge}</div>
      </div>
      ${blocking}
      <div class="detail-meta">${meta}</div>
      ${lcBlock}
      <div class="detail-cols">
        <section>
          <h4>采购口径（硬门槛）</h4>
          <ul class="checklist">${gates || "<li>暂无足够参数，先补型号/规格</li>"}</ul>
        </section>
        <section>
          <h4>搜索词（按序使用，最多 3 条）</h4>
          ${queries || '<p class="fineprint">参数不足，未生成搜索词。</p>'}
        </section>
      </div>
      <h4>必须核对</h4>
      <ol class="verify">${verify}</ol>
      <h4>风险提示</h4>
      <div class="risks">${risks}</div>
      <div class="detail-foot">
        <span>人工门：${esc(d.gate)}；manual_approval_required = true</span>
        <button class="ghost small" data-score-idx="${idx}">用这行评分</button>
      </div>`;

    $("#line-detail")
      .querySelectorAll("[data-copy]")
      .forEach((b) => b.addEventListener("click", () => copyText(b.dataset.copy)));
    const scoreBtn = $("#line-detail").querySelector("[data-score-idx]");
    if (scoreBtn) {
      scoreBtn.addEventListener("click", () => {
        $("#score-line").value = String(idx);
        $("#score-panel").scrollIntoView({ behavior: "smooth", block: "start" });
        $("#cand-text").focus();
      });
    }
  }

  /* ---------- 评分 ---------- */

  function renderScoreSelect() {
    const sel = $("#score-line");
    sel.innerHTML = state.result.lines
      .map((ln, i) => `<option value="${i}">#${ln.line_no} ${esc(ln.decision.headline)}</option>`)
      .join("");
    if (state.activeIndex >= 0) sel.value = String(state.activeIndex);
  }

  async function runScore() {
    const idx = Number($("#score-line").value);
    const line = state.result.lines[idx];
    if (!line) return toast("先选一行 BOM");
    const text = $("#cand-text").value.trim();
    if (!text) return toast("请先粘贴候选数据");

    const btn = $("#btn-score");
    btn.disabled = true;
    btn.textContent = "评分中…";
    try {
      const data = await api("/api/score", { line, csv: text });
      state.lastScore = data;
      state.lastScoreIndex = idx;
      renderScore(data, line);
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "评分";
    }
  }

  function renderScore(data, line) {
    const st = data.stats || {};
    const top = data.candidates.find((c) => c.score > 0);
    let cls = "";
    if (!top || top.score < 60) cls = "bad";
    else if (top.score < 75 || data.ambiguous) cls = "warn";

    const stats = [
      `<div class="stat">样本 <b>${st.count || 0}</b></div>`,
      st.price_conclusion
        ? `<div class="stat">中位价 <b>${fmtMoney(st.median)}</b></div>
           <div class="stat">P10 <b>${fmtMoney(st.p10)}</b></div>
           <div class="stat">P25–P75 <b>${fmtMoney(st.p25)} – ${fmtMoney(st.p75)}</b></div>
           <div class="stat">P90 <b>${fmtMoney(st.p90)}</b></div>`
        : `<div class="stat">价格结论 <b>样本 &lt;5，不下结论</b></div>`,
      `<div class="stat">权重：一致性 <b>${data.weights.match}</b>，店铺 <b>${data.weights.shop}</b>，评价 <b>${data.weights.review}</b>，价格 <b>${data.weights.price}</b>，销量 <b>${data.weights.sales}</b></div>`,
    ].join("");

    const cards = data.candidates
      .map((c) => {
        const rejected = c.score === 0;
        const decCls = c.decision === "主选" ? "main" : c.decision === "备选" ? "alt" : "out";
        const meta = [
          c.sales != null ? `销量 ${fmtNum(c.sales)}` : "",
          c.rating != null ? `好评 ${c.rating <= 1 ? Math.round(c.rating * 100) + "%" : c.rating + "%"}` : "",
          c.shop_type || "",
          c.is_tmall ? "天猫" : "",
          c.real_photo ? "有实拍" : "无实拍",
        ]
          .filter(Boolean)
          .map((x) => `<span>${esc(x)}</span>`)
          .join("");
        const reasons = (c.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
        const risks = (c.risks || [])
          .map((r) => `<span class="risk"><span class="code">${esc(r.code)}</span>${esc(r.text)}</span>`)
          .join("");
        return `<div class="cand ${rejected ? "rejected" : ""} ${c.rank === 1 && !rejected ? "top" : ""}">
          <div class="cand-head">
            <span class="rank">#${c.rank}</span>
            <span class="score">${c.score ?? 0}<small>分</small></span>
            <span class="dec ${decCls}">${esc(c.decision)}</span>
            <span class="price">${fmtMoney(c.price)}</span>
          </div>
          <div class="cand-title">${esc(c.title)}</div>
          <div class="cand-meta">${meta}</div>
          ${reasons ? `<ul class="cand-reasons">${reasons}</ul>` : ""}
          ${risks ? `<div class="cand-risks">${risks}</div>` : ""}
        </div>`;
      })
      .join("");

    $("#score-result").innerHTML = `
      <div class="verdict ${cls}">${esc(data.verdict)}</div>
      <div class="statline">${stats}</div>
      <div class="fineprint note-gap">${esc(data.note)}</div>
      ${cards}`;
  }

  /* ---------- 导出 ---------- */

  function exportJson() {
    if (!state.result) return toast("还没有结果");
    const payload = {
      exported_at: new Date().toISOString(),
      engine_version: state.result.engine_version,
      offline: true,
      manual_approval_required: true,
      summary: state.result.summary,
      warnings: state.result.warnings,
      lines: state.result.lines,
      score: state.lastScore || null,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bom-selection-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast("已导出 JSON");
  }

  function exportCsv() {
    if (!state.result) return toast("还没有结果");
    const cell = (v) => {
      const s = String(v ?? "");
      return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const head = ["行号", "型号", "名称", "封装", "规格", "数量", "状态", "采购口径", "搜索词1", "搜索词2", "搜索词3", "风险码"];
    const rows = [head];
    for (const ln of state.result.lines) {
      const d = ln.decision;
      const q = (d.search_queries || []).slice(0, 3);
      while (q.length < 3) q.push("");
      const codes = (d.risk_notes || []).map((r) => r.code).join("/");
      rows.push([
        ln.line_no,
        ln.mpn,
        ln.desc,
        ln.package,
        ln.params_text,
        ln.qty ?? "",
        ln.status === "ready" ? "可执行" : "待确认",
        (d.buy_spec || []).join(" | "),
        ...q,
        codes,
      ]);
    }
    const csv = "\ufeff" + rows.map((r) => r.map(cell).join(",")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bom-purchase-list-" + new Date().toISOString().slice(0, 10) + ".csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast("已导出清单 CSV");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制搜索词");
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        toast("已复制搜索词");
      } catch {
        toast("复制失败，请手动选择");
      }
      ta.remove();
    }
  }

  /* ---------- 淘宝执行 ---------- */

  function currentExecLine() {
    if (!state.result) return null;
    const idx = Number($("#exec-line").value);
    return state.result.lines[idx] || null;
  }

  function renderExecSelect() {
    if (!state.result) return;
    const sel = $("#exec-line");
    sel.innerHTML = state.result.lines
      .map(
        (ln, i) =>
          `<option value="${i}">#${ln.line_no} ${esc(ln.decision.headline)}${
            ln.status === "ready" ? "" : "（待确认）"
          }</option>`
      )
      .join("");
    if (state.activeIndex >= 0) sel.value = String(state.activeIndex);
    exec.lineIndex = Number(sel.value);
  }

  async function pollSession() {
    try {
      const st = await fetch("/api/session/status").then((r) => r.json());
      updateSessionUI(st);
    } catch {
      /* 服务重启期间忽略 */
    }
  }

  function updateSessionUI(st) {
    const dot = $("#sess-dot");
    const text = $("#sess-text");
    const task = $("#sess-task");
    const warn = $("#sess-warn");
    const logs = $("#sess-logs");
    dot.className = "sdot";
    let label = "未启动";
    if (st.running) {
      if (st.verification) {
        label = "需要人工完成验证";
        dot.classList.add("bad");
      } else if (st.waiting_login) {
        label = "等待你在浏览器中登录…";
        dot.classList.add("warn");
      } else if (!st.ready) {
        label = "启动中（打开浏览器 / 加载页面）…";
        dot.classList.add("warn");
      } else if (st.login_state === "failed") {
        label = "会话就绪，但登录未完成";
        dot.classList.add("bad");
      } else {
        label = "就绪";
        dot.classList.add("ok");
      }
    } else if (st.desynced) {
      label = "会话状态异常";
      dot.classList.add("bad");
    }
    text.textContent = label;
    task.textContent = st.task_id ? `${st.task_id}  pid ${st.pid || "-"}` : "";
    warn.textContent = st.verification
      ? "请在浏览器里手动完成验证，完成后点右侧按钮（或等它自动恢复）。"
      : st.browser_gone
      ? "淘宝浏览器已关闭：点「结束会话」再「启动淘宝会话」即可恢复（也可以直接点启动）。"
      : st.desynced
      ? "请结束会话后重新启动。"
      : st.running
      ? ""
      : st.last_error || "";
    const recheck = $("#btn-sess-recheck");
    if (recheck) recheck.classList.toggle("hidden", !st.verification);
    if (st.logs) {
      logs.textContent = st.logs.slice(-30).join("\n");
      logs.scrollTop = logs.scrollHeight;
    }
    $("#btn-exec-search").disabled =
      !(st.ready && !st.verification && !st.desynced && !st.browser_gone) || !state.result;
    // 浏览器被关掉时「启动会话」要能点（结束会话才会真的重启）
    $("#btn-session-start").disabled = st.running && !st.browser_gone;
    $("#btn-session-stop").disabled = !st.running;
    sessionReady = !!(st.ready && !st.verification && !st.desynced && !st.browser_gone);
    syncBatchButtons();
    $("#btn-batch-stop").disabled = !batchRunning;
    if (!st.running) $("#btn-exec-open").disabled = true;
  }

  async function startSession() {
    $("#btn-session-start").disabled = true;
    $("#sess-text").textContent = "启动中…";
    try {
      const st = await api("/api/session/start", {});
      updateSessionUI(st);
      toast("会话启动中，浏览器窗口会打开；首次请手动登录淘宝");
    } catch (e) {
      toast(e.message);
    }
    pollSession();
  }

  async function stopSession() {
    if (!window.confirm("结束会话会关闭淘宝浏览器窗口，已加购的内容保留在购物车。确定？")) return;
    try {
      const st = await api("/api/session/stop", {});
      updateSessionUI(st);
      toast("会话已结束");
    } catch (e) {
      toast(e.message);
    }
    pollSession();
  }

  async function recheckVerification() {
    const btn = $("#btn-sess-recheck");
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/session/recheck", {});
      if (d.ok) {
        toast("验证已通过，按钮已恢复");
      } else {
        toast("浏览器里验证还没完成（滑块或图片点选），完成后再点一次");
      }
      if (d.status) updateSessionUI(d.status);
    } catch (e) {
      toast(e.message);
    } finally {
      if (btn) btn.disabled = false;
      pollSession();
    }
  }

  async function showBrowserWindow() {
    const btn = $("#btn-sess-window");
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/taobao/window", { action: "show" });
      toast(d.ok ? "淘宝窗口已移到屏幕内（不会抢你的前台）" : "没找到调试版浏览器窗口，先启动会话");
    } catch (e) {
      toast(e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function hideBrowserWindow() {
    const btn = $("#btn-sess-window-hide");
    if (btn) btn.disabled = true;
    try {
      const d = await api("/api/taobao/window", { action: "hide" });
      toast(d.ok ? "淘宝窗口已藏到屏幕外" : "没找到调试版浏览器窗口");
    } catch (e) {
      toast(e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function execSearch() {
    const line = currentExecLine();
    if (!line) return;
    const btn = $("#btn-exec-search");
    btn.disabled = true;
    btn.textContent = "搜索中…";
    $("#exec-candidates").innerHTML = "";
    $("#exec-search-note").textContent = "正在淘宝搜索…";
    $("#exec-detail").innerHTML = '<div class="empty">搜索完成后选一个候选打开。</div>';
    try {
      const data = await api("/api/taobao/search", { line });
      exec.search = data;
      exec.chosen = null;
      exec.open = null;
      renderExecCandidates(data);
    } catch (e) {
      $("#exec-search-note").innerHTML = `<span class="sess-warn">${esc(e.message)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "搜索候选";
      pollSession();
    }
  }

  function renderExecCandidates(data) {
    const box = $("#exec-candidates");
    const note = $("#exec-search-note");
    if (!data.ok) {
      const tried = (data.attempts || []).map((a) => esc(a.query)).join(" / ");
      note.innerHTML = `<span class="sess-warn">${esc(data.message || "搜索失败")}</span>${tried ? "（已试：" + tried + "）" : ""}`;
      box.innerHTML = data.screenshot ? `<img class="shot" src="${data.screenshot}">` : "";
      $("#btn-exec-open").disabled = true;
      return;
    }
    const cands = data.candidates || [];
    const usable = cands.filter((c) => c.score > 0);
    exec.chosen = usable.length ? usable[0].index : null;
    box.innerHTML = cands
      .map((c) => {
        const dead = c.score <= 0;
        const sel = exec.chosen === c.index;
        const over = c.over_ratio && c.over_ratio > 1.2 ? c.over_ratio : 0;
        return `<label class="cand-row ${sel ? "sel" : ""} ${dead ? "rej" : ""}">
          <input type="radio" name="exec-cand" value="${c.index}" ${sel ? "checked" : ""} ${dead ? "disabled" : ""}>
          <span class="score">${c.score}</span>
          <span class="title">${esc(c.title)}<br>
            <span class="cand-meta">销量 ${fmtNum(c.sales)}，好评 ${
          c.rating != null ? Math.round(c.rating * 100) + "%" : "—"
        }，${c.is_tmall ? "天猫" : "店铺未知"}${
          c.pack_size ? `，${c.pack_size}只/份` : ""
        }${c.actual_pcs ? `，到手 ${c.actual_pcs} 只` : ""}</span>${
          over ? `<span class="cand-flag">超量 ${over >= 10 ? Math.round(over) : over.toFixed(1)} 倍</span>` : ""
        }</span>
          <span class="price">${fmtMoney(c.price)}${
          c.pack_size && c.unit_price != null ? `<br><span class="cand-meta">单只 ${fmtMoney(c.unit_price)}</span>` : ""
        }${c.cost != null ? `<br><span class="cand-meta">合计 ${fmtMoney(c.cost)}</span>` : ""}</span>
          <span class="dec ${c.decision === "主选" ? "main" : c.decision === "备选" ? "alt" : "out"}">${esc(
          c.decision
        )}</span>
        </label>`;
      })
      .join("");
    box.querySelectorAll('input[name="exec-cand"]').forEach((r) => {
      r.addEventListener("change", () => {
        exec.chosen = Number(r.value);
        box.querySelectorAll(".cand-row").forEach((x) => x.classList.remove("sel"));
        r.closest(".cand-row").classList.add("sel");
        $("#btn-exec-open").disabled = false;
      });
    });
    $("#btn-exec-open").disabled = exec.chosen === null;
    note.textContent = `搜索词「${data.query}」，${data.items_count} 条，${data.verdict}`;
  }

  async function execOpen() {
    if (exec.chosen === null) return toast("先选一个候选");
    const line = currentExecLine();
    const btn = $("#btn-exec-open");
    btn.disabled = true;
    btn.textContent = "打开中…";
    try {
      const data = await api("/api/taobao/open", { line, index: exec.chosen });
      exec.open = data;
      renderSkuPanel(data, line);
    } catch (e) {
      toast(e.message);
      $("#exec-detail").innerHTML = `<div class="exec-result bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "打开选中项并匹配规格";
      pollSession();
    }
  }

  function renderSkuPanel(data, line) {
    const item = data.item || {};
    const groups = (data.sku_groups || []).filter((g) => g.label && (g.values || []).length);
    const sug = {};
    (data.suggestions || []).forEach((s) => (sug[s.label] = s));
    const unresolved = {};
    (data.unresolved || []).forEach((u) => (unresolved[u.label] = u));
    const rows = groups
      .map((g) => {
        const s = sug[g.label];
        const current = (g.values.find((v) => v.selected) || {}).text || "";
        const chosen = s ? s.value : current;
        const opts = g.values
          .map(
            (v) =>
              `<option value="${esc(v.text)}" ${v.text === chosen ? "selected" : ""} ${
                v.disabled ? "disabled" : ""
              }>${esc(v.text)}${v.disabled ? "（缺货）" : ""}</option>`
          )
          .join("");
        const bad = unresolved[g.label];
        return `<div class="sku-item ${bad ? "warn" : ""}">
          <label>${esc(g.label)}</label>
          <select data-group="${esc(g.label)}"><option value="">请选择</option>${opts}</select>
          ${bad ? `<div class="why">自动匹配失败：${esc(bad.reason || "")}</div>` : ""}
        </div>`;
      })
      .join("");

    const packInfo = data.pack_size && data.pack_size > 1 ? data.pack_size : 0;
    const actual = data.actual_pcs || (packInfo ? (data.suggested_qty || 1) * packInfo : null);
    $("#exec-detail").innerHTML = `
      <div class="sku-panel">
        <div class="kicker">已打开商品</div>
        <h3 class="sku-title">${esc(item.title || "")}</h3>
        <div class="detail-meta">
          <span>详情价 <b>${fmtMoney(data.detail_price)}</b></span>
          ${packInfo ? `<span>每份 <b>${packInfo} 只</b></span>` : ""}
          ${packInfo && data.unit_price != null ? `<span>单只 <b>${fmtMoney(data.unit_price)}</b></span>` : ""}
          <span>SKU 组 <b>${groups.length}</b></span>
          <span>BOM 用量 <b>${line.qty || "?"}</b></span>
        </div>
        ${
          packInfo
            ? `<div class="alert tight">识别到<b>整包卖</b>：1 份 = ${packInfo} 只${
                data.pack_source ? `（依据「${esc(data.pack_source)}」）` : ""
              }。BOM 要 ${data.bom_qty || "?"} 只 → 加购 <b>${data.suggested_qty}</b> 份，实际到手 <b>${actual}</b> 只${
                actual && data.bom_qty ? `（多 ${actual - data.bom_qty} 只）` : ""
              }。</div>`
            : ""
        }
        ${rows || '<div class="alert">没有读到 SKU 组：可能无需选规格，或页面结构未识别。请人工确认后再加购。</div>'}
        <div class="qty-row">
          <label for="exec-qty">${packInfo ? "加购份数" : "加购数量"}</label>
          <input id="exec-qty" type="number" min="1" max="99999" value="${
            data.suggested_qty || line.qty || 1
          }">
          <span class="fineprint">${
            packInfo
              ? `按「份」填：每份 ${packInfo} 只，BOM ${data.bom_qty || "?"} 只 → ${data.suggested_qty} 份`
              : "按 BOM 用量；整包/起订量请按实际改"
          }</span>
        </div>
        <button class="primary" id="btn-exec-add" ${groups.length ? "" : "disabled"}>加入购物车</button>
        <div id="exec-add-result"></div>
        ${data.screenshot ? `<img class="shot" src="${data.screenshot}">` : ""}
      </div>`;
    const add = $("#btn-exec-add");
    if (add) add.addEventListener("click", execAdd);
  }

  async function execAdd() {
    const line = currentExecLine();
    if (!line) return;
    const sels = [...$("#exec-detail").querySelectorAll("select[data-group]")]
      .map((s) => ({ label: s.dataset.group, value: s.value }))
      .filter((x) => x.value);
    const groupCount = $("#exec-detail").querySelectorAll("select[data-group]").length;
    if (sels.length < groupCount) return toast("还有 SKU 组没选");
    if (!sels.length) return toast("没有可选的 SKU");
    const qty = Number($("#exec-qty").value || 0);
    if (!Number.isInteger(qty) || qty < 1) return toast("数量必须是正整数");

    const summary = sels.map((s) => `${s.label}=${s.value}`).join("；");
    const pack = (exec.open && exec.open.pack_size) || 0;
    const qtyText =
      pack > 1 ? `${qty} 份（每份 ${pack} 只 = 到手 ${qty * pack} 只，BOM 要 ${line.qty || "?"} 只）` : `${qty} 只`;
    const ok = window.confirm(
      `即将加入购物车：\n\n${line.decision.headline}\n数量：${qtyText}\nSKU：${summary}\n\n只到购物车，不结算、不付款。确认？`
    );
    if (!ok) return;

    const btn = $("#btn-exec-add");
    btn.disabled = true;
    btn.textContent = "加购中…";
    try {
      const open = exec.open || {};
      const data = await api("/api/taobao/add", {
        line,
        index: exec.chosen,
        qty,
        selections: sels,
        auto: qty === open.suggested_qty,
        pack_hint: open.pack_size || null,
        price_hint: open.detail_price || null,
      });
      exec.result = data;
      renderAddResult(data);
    } catch (e) {
      $("#exec-add-result").innerHTML = `<div class="exec-result bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "加入购物车";
      pollSession();
    }
  }

  function renderAddResult(data) {
    const box = $("#exec-add-result");
    if (!data.ok) {
      const hint = data.code === "VERIFICATION_REQUIRED" ? "请在浏览器里手动完成验证，然后重试。" : "";
      box.innerHTML =
        `<div class="exec-result bad"><b>未加购（${esc(data.code || "失败")}）</b><br>${esc(data.message || "")} ${esc(
          hint
        )}</div>` + (data.screenshot ? `<img class="shot" src="${data.screenshot}">` : "");
      return;
    }
    box.innerHTML =
      `<div class="exec-result ok"><b>已加入购物车</b>
        <ul>
          <li>数量：${data.qty}${data.pack_size && data.pack_size > 1 ? ` 份 × ${data.pack_size} 只 = 到手 ${data.actual_pcs || data.qty * data.pack_size} 只` : " 只"}（${
        data.qty_confirmed === true ? "已确认" : data.qty_confirmed === false ? "未确认" : "未回读"
      }）</li>
          ${data.unit_price != null && data.pack_size && data.pack_size > 1 ? `<li>单只价：${fmtMoney(data.unit_price)}</li>` : ""}
          ${data.cost != null ? `<li>实付金额：<b>${fmtMoney(data.cost)}</b>${data.final_price != null ? `（每份 ${fmtMoney(data.final_price)} × ${data.qty_sent || data.qty} 份）` : ""}</li>` : ""}
          <li>SKU：${data.sku_verified ? "已校验" : "未校验"}（${data.sku_selection_count || 0} 组）</li>
          <li>购物车是终点：结算、付款、地址、发票请你在淘宝完成。</li>
        </ul></div>` +
      (data.screenshot ? `<img class="shot" src="${data.screenshot}">` : "") +
      `<div class="row row-left"><button class="ghost small" id="btn-exec-cart">查看购物车</button></div>`;
    const b = $("#btn-exec-cart");
    if (b) b.addEventListener("click", execCart);
  }

  async function execCart() {
    try {
      const d = await api("/api/taobao/cart", {});
      // SKU 面板还没渲染过就没有 #exec-add-result（从「购物车维护」直接点
      // 查看明细时就是这样）——兜底塞进购物车维护区，别让 null 崩掉。
      let box = $("#exec-add-result");
      if (!box) {
        box = $("#cart-remove-result");
        if (box) box.innerHTML = "";
      }
      if (!box) return;
      if (!d.ok) {
        // I5 的新错误码（CART_ROWS_NOT_RENDERED 等）也在这里亮出来
        box.insertAdjacentHTML(
          "beforeend",
          `<div class="exec-result bad">读取购物车失败${d.code ? `［${esc(d.code)}］` : ""}：${esc(d.message || "未知错误")}</div>` +
            (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "")
        );
        return;
      }
      // I4：行明细直接渲染，不再让用户对着截图数件数
      const rows = d.rows || [];
      const rowsHtml = rows.length
        ? `<table class="cart-rows"><thead><tr><th>#</th><th>商品</th><th>规格</th><th>单价</th></tr></thead><tbody>` +
          rows
            .map(
              (r) =>
                `<tr><td>${r.index ?? "?"}</td><td>${esc((r.title || "").slice(0, 60))}</td>` +
                `<td>${esc((r.spec || "—").slice(0, 40))}</td>` +
                `<td>${r.price != null ? "¥" + r.price : "—"}</td></tr>`
            )
            .join("") +
          `</tbody></table>`
        : d.cart_empty
        ? `<div class="exec-result ok">购物车是空的（没有商品）</div>`
        : `<div class="exec-result bad">购物车里没有读到行${d.rows_degraded ? "（DOM 降级，看截图核对）" : ""}</div>`;
      box.insertAdjacentHTML(
        "beforeend",
        `<div class="exec-result ok">购物车 <b>${d.cart_item_count ?? "?"}</b> 件` +
          `｜读到 ${rows.length} 行` +
          (d.pages_read > 1 ? `（${d.pages_read} 页）` : "") +
          (d.rows_degraded ? "｜⚠ 明细降级，以截图为准" : "") +
          `</div>` +
          rowsHtml +
          (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "")
      );
    } catch (e) {
      toast(e.message);
    }
  }

  /* ---------- 一键全部加购 ---------- */

  const BATCH_LABEL = { pending: "待处理", running: "进行中", ok: "已加购", failed: "失败", uncertain: "待核对", skipped: "跳过" };

  let batchProgress = null;

  async function startBatch(opts) {
    if (!state.result) return toast("先分析 BOM");
    if (!sessionReady) return toast("先启动淘宝会话");
    const mode = (opts && opts.mode) || "auto"; // auto | redo | retry
    const lines = state.result.lines;
    // 型号/名称/参数全空的行没有搜索词，跑起来必然搜不出东西：提前说清楚会被跳过
    const noQuery = lines.filter(
      (l) => !((l.query_plan && l.query_plan.length) || (l.decision && (l.decision.search_queries || []).length))
    );
    const skipTip = noQuery.length
      ? `\n\n其中 ${noQuery.length} 行（#${noQuery.map((l) => l.line_no).join("、#")}）型号/名称都没填，没有搜索词，会被直接跳过。`
      : "";
    const pg = batchProgress || {};
    const resumeN = pg.ok_count || 0;
    const problemN = pg.problem_count || 0;
    const problemNos = pg.problem_line_nos || [];
    const runnableN = lines.length - noQuery.length;
    // 「都处理过了」= ok + failed/待核对 已经盖住所有可跑行：再点就是重跑，
    // 不能假装还有活干（服务端也会直接拒绝，见 BATCH_ALL_DONE）。
    const allProcessed =
      pg.total > 0 && pg.total === lines.length && runnableN > 0 && resumeN + problemN >= runnableN;
    const autoRetry = mode === "auto" && allProcessed && problemN > 0;
    const autoRedo = mode === "auto" && allProcessed && problemN === 0;
    let ok;
    let payload = { lines, prefer_cheap: true, max_candidates: 3, resume: true };

    if (mode === "retry" || autoRetry) {
      // 只重跑问题行：已成功的行照旧跳过，不会重复加购
      if (!problemNos.length) return toast("没有需要重跑的行");
      ok = window.confirm(
        `重跑有问题的 ${problemNos.length} 行：\n#${problemNos.join("、#")}\n\n` +
          (resumeN > 0 ? `已成功加入购物车的 ${resumeN} 行会跳过，不会重复加购。\n` : "") +
          `「待核对」的行如果上次其实已经进了购物车，重跑会再加一份 —— 建议先去购物车看一眼。\n\n` +
          `只到购物车，不结算、不付款。继续？` + skipTip
      );
      payload.resume = true;
      payload.redo_lines = problemNos;
    } else if (mode === "redo" || autoRedo) {
      // 整批重跑：必须明确警告，这一步会让购物车里出现重复商品
      ok = window.confirm(
        `⚠ 重新跑全部 ${lines.length} 行。\n\n` +
          `这不会清除购物车 —— 上次加进去的 ${resumeN} 行还在，重跑会**重复加一遍**，数量翻倍。\n` +
          `正确做法：先用「删除上一批」清掉上次加进购物车的商品，再重跑。\n\n` +
          `只到购物车，不结算、不付款。确定重跑全部？` + skipTip
      );
      payload.resume = false;
      payload.redo = true;
    } else if (resumeN > 0) {
      const rest = runnableN - resumeN;
      const nextNo = (lines.find((l) => !(pg.ok_line_nos || []).includes(l.line_no)) || {}).line_no;
      ok = window.confirm(
        `上次已完成 ${resumeN} 行（已加入购物车），这次接着跑剩下的 ${rest} 行` +
          (nextNo ? `，从 #${nextNo} 开始` : "") +
          `。\n\n已完成的不会被重复加购。\n只到购物车，不结算、不付款。继续？` +
          skipTip
      );
    } else {
      const list = lines.map((l) => `#${l.line_no} ${l.decision.headline} ×${l.qty ?? "?"}`).join("\n");
      ok = window.confirm(
        `按顺序把 ${lines.length} 行逐个加入购物车：\n\n${list}\n\n每行取「通过硬门槛的候选里最便宜的一个」，失败自动换下一个候选。\n只到购物车，不结算、不付款。开始？` +
          skipTip
      );
    }
    if (!ok) return;
    $("#btn-batch-start").disabled = true;
    $("#batch-progress").classList.remove("hidden");
    $("#batch-note").textContent = "正在启动批量任务…";
    try {
      const st = await api("/api/taobao/batch-start", payload);
      batchRunning = !!st.running;
      renderBatch(st);
    } catch (e) {
      toast(e.message);
      // BATCH_ALL_DONE 说明这批已经处理过：刷一次按钮，让它变成
      // 「重跑问题行」/「重新跑全部」，别让用户点了没反应还不知道下一步干什么
      if (/上次已全部跑完/.test(e.message || "")) {
        $("#btn-batch-start").disabled = false;
        syncBatchButtons();
      }
    }
    pollBatch();
  }

  async function stopBatch() {
    try {
      const st = await api("/api/taobao/batch-stop", {});
      renderBatch(st);
      toast("已请求停止：当前行处理完就停");
    } catch (e) {
      toast(e.message);
    }
  }

  const BASE_TITLE = document.title;
  let verifyTitleTimer = null;

  /** 批量停在「等你手动过验证」时，页面顶部一直挂着提示条 + 倒计时。 */
  function renderVerifyBanner(st) {
    const el = $("#verify-banner");
    if (!el) return;
    const v = st && st.verification;
    const waiting = !!(v && v.waiting);
    el.classList.toggle("hidden", !waiting);

    if (!waiting) {
      if (verifyTitleTimer) {
        clearInterval(verifyTitleTimer);
        verifyTitleTimer = null;
        document.title = BASE_TITLE;
      }
      return;
    }

    const left = Math.max(0, Number(v.remaining_s) || 0);
    const mm = String(Math.floor(left / 60)).padStart(2, "0");
    const ss = String(left % 60).padStart(2, "0");
    const timer = $("#verify-timer");
    if (timer) timer.textContent = `${mm}:${ss}`;
    const sub = $("#verify-sub");
    if (sub) {
      const mins = Math.max(1, Math.round((Number(v.total_s) || 1800) / 60));
      sub.textContent =
        `刷新后仍没出商品。窗口在屏幕外，点「打开淘宝窗口」去完成验证，最多等你 ${mins} 分钟`;
    }
    // 标签页不在前台时也能看到
    if (!verifyTitleTimer) {
      let flip = false;
      verifyTitleTimer = setInterval(() => {
        document.title = flip ? "需要完成淘宝验证" : BASE_TITLE;
        flip = !flip;
      }, 1600);
    }
  }

  function renderBatch(st) {
    const box = $("#batch-progress");
    const results = (st && st.results) || [];
    if (!results.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    box.innerHTML = results
      .map((r) => {
        const cls =
          r.status === "ok" ? "ok" : r.status === "failed" ? "bad" : r.status === "uncertain" ? "run" : r.status === "running" ? "run" : r.status === "skipped" ? "skip" : "";
        const cand = r.candidate || {};
        const price = cand.price != null ? fmtMoney(cand.price) : r.price_from != null ? "起 " + fmtMoney(r.price_from) : "";
        const unit =
          r.unit_price != null && r.pack_size && r.pack_size > 1 ? `单只 ${fmtMoney(r.unit_price)}` : "";
        const sku = (r.sku || []).map((s) => `${s.label}=${s.value}`).join("，");
        // 标题里已经带了需要量（×120），这里只在买了多份或一份多只时才补一句
        const multi = r.pack_size && r.pack_size > 1;
        const qty = r.qty && (r.qty > 1 || multi)
          ? `买 ${r.qty} 份${multi ? `（1份=${r.pack_size}只，到手${r.actual_pcs || r.qty * r.pack_size}只）` : ""}`
          : "";
        const tries = (r.attempts || []).length > 1 ? `试了 ${r.attempts.length} 个候选` : "";
        const sub = [r.message, sku, tries].filter(Boolean).join(" ｜ ");
        // 有实付金额就显示合计（买的份数 × 每份价），否则退回候选标价
        const money =
          r.cost != null
            ? `<b>${fmtMoney(r.cost)}</b>${unit ? `<span class="bsub">${esc(unit)}</span>` : ""}`
            : `${esc(price)}${unit ? `<span class="bsub">${esc(unit)}</span>` : ""}`;
        return `<div class="brow ${cls}">
          <span class="bst">${BATCH_LABEL[r.status] || esc(r.status || "")}</span>
          <span class="bbody">
            <span class="bt">#${esc(r.line_no)} ${esc(r.headline || "")} ${esc(qty)}</span>
            <span class="bs">${esc(sub || "等待中…")}</span>
          </span>
          <span class="bprice">${money}</span>
        </div>`;
      })
      .join("");
  }

  async function pollBatch() {
    let st;
    try {
      st = await fetch("/api/taobao/batch").then((r) => r.json());
    } catch {
      return;
    }
    batchRunning = !!st.running;
    batchProgress = st.progress || batchProgress;
    renderBatch(st);
    renderVerifyBanner(st);
    syncBatchButtons();
    $("#btn-batch-stop").disabled = !batchRunning;
    if (st.warning) {
      // 整批空跑 / 进度错配这类严重情况，直接把原因摆在进度条上
      $("#batch-note").textContent = st.warning;
    } else if (batchRunning) {
      const money = st.cost_total ? `，已花 ${fmtMoney(st.cost_total)}` : "";
      $("#batch-note").textContent = `${st.phase || "进行中"}，已完成 ${st.done}/${st.total}${money}；每行约 20 到 60 秒，浏览器窗口请别关`;
    } else if (st.total) {
      const okn = st.ok_count != null ? st.ok_count : (st.results || []).filter((r) => r.status === "ok").length;
      const pg = st.progress || {};
      const { allProcessed } = batchMode();
      const tail = allProcessed
        ? "；这批已跑完，要重跑点「重跑问题行」或「重新跑全部」（会重复加购，先删上一批）"
        : pg.ok_count > 0
        ? "；进度已保存，下次点「继续加购」从断点接着跑"
        : "";
      const money = st.cost_total
        ? `，合计 ${fmtMoney(st.cost_total)}${st.cost_lines ? `（${st.cost_lines} 行有实付价）` : ""}`
        : "";
      // 跳过/失败的行要在收尾一句话里点名，否则「已加购 108 行」看不出还有 11 行没处理
      const skipped = (st.results || []).filter((r) => r.status === "skipped").length;
      const failed = (st.results || []).filter((r) => r.status === "failed").length;
      const aside = [skipped ? `跳过 ${skipped} 行` : "", failed ? `失败 ${failed} 行` : ""].filter(Boolean).join("、");
      $("#batch-note").textContent =
        `上次批量：已加购 ${okn} 行${money}${aside ? `，${aside}` : ""}` +
        `${st.stopped_reason ? "（" + st.stopped_reason + "）" : ""}${tail}`;
    } else if (st.progress && st.progress.ok_count > 0) {
      const money = st.cost_total ? `，合计 ${fmtMoney(st.cost_total)}` : "";
      const { allProcessed } = batchMode();
      $("#batch-note").textContent = allProcessed
        ? `这批 ${st.progress.ok_count + (st.progress.problem_count || 0)} 行都已处理${money}；要重跑点「重跑问题行」或「重新跑全部」（会重复加购，先删上一批）`
        : `有上次的进度：已完成 ${st.progress.ok_count} 行${money}，点「继续加购」从断点接着跑`;
    }
  }

  function batchMode() {
    const pg = batchProgress || {};
    const n = pg.ok_count || 0;
    const p = pg.problem_count || 0;
    const lines = (state.result && state.result.lines) || [];
    const runnableN = lines.filter(
      (l) => (l.query_plan && l.query_plan.length) || (l.decision && (l.decision.search_queries || []).length)
    ).length;
    // 「都处理过了」= 进度里的总数和当前行数一致，且 ok + 问题行盖住了所有可跑行
    const allProcessed = pg.total > 0 && pg.total === lines.length && runnableN > 0 && n + p >= runnableN;
    return { n, p, runnableN, allProcessed };
  }

  function syncBatchButtons() {
    const btn = $("#btn-batch-start");
    const { n, p, runnableN, allProcessed } = batchMode();
    btn.disabled = !sessionReady || !state.result || batchRunning;
    if (!batchRunning) {
      // 都处理过之后按钮不能还是「继续加购」—— 那会跳过全部、一行不跑。
      // 有问题行就主推「重跑问题行」，全成功才是「重新跑全部」。
      btn.textContent = allProcessed
        ? p > 0
          ? `重跑问题行 ${p}`
          : `重新跑全部 ${runnableN} 行`
        : n > 0
        ? `继续加购 ${n} 行`
        : "一键全部加购";
    }
    const probBtn = $("#btn-batch-retry");
    if (probBtn) {
      // 主按钮已经承担「重跑问题行」时不再重复出现
      probBtn.classList.toggle("hidden", !(p > 0) || allProcessed || batchRunning);
      probBtn.textContent = `重跑问题行 ${p}`;
      probBtn.disabled = !sessionReady || !state.result || batchRunning;
    }
    const reset = $("#btn-batch-reset");
    if (reset) reset.classList.toggle("hidden", !(n > 0 || p > 0) || batchRunning);
  }

  async function resetBatchProgress() {
    if (!window.confirm("只清空「进度记录」，购物车不动。\n\n清完再点「一键全部加购」会从第 1 行重新跑 —— 那样会和新加的商品重复，所以请先清空购物车。\n\n确认清空进度？")) return;
    try {
      await api("/api/taobao/batch-reset", {});
      batchProgress = { ok_count: 0, ok_line_nos: [] };
      $("#batch-progress").classList.add("hidden");
      $("#batch-note").textContent = "进度已清空。要重头跑请先清空购物车，再点「一键全部加购」。";
      syncBatchButtons();
    } catch (e) {
      toast(e.message);
    }
  }

  /* ---------- 购物车维护 ---------- */

  async function cartRemove(removeAll) {
    if (!sessionReady) return toast("先启动淘宝会话");
    let payload;
    if (removeAll) {
      const t = window.prompt(
        "这会删掉购物车里的全部商品（包括你自己加的非 BOM 商品），不可撤销。\n如果确定，请输入：清空"
      );
      if (t !== "清空") return toast("已取消");
      payload = { all: true, confirm: "清空" };
    } else {
      const kw = $("#cart-keyword").value.trim();
      if (!kw) return toast("先填标题关键字，例如 0603");
      if (!window.confirm(`删除购物车里标题包含「${kw}」的商品？不可撤销。`)) return;
      payload = { keyword: kw };
    }

    const box = $("#cart-remove-result");
    box.textContent = "删除中…（会打开购物车页面）";
    try {
      const d = await api("/api/taobao/cart-remove", payload);
      if (!d.ok) {
        // I5 错误码透出：CART_ROWS_NOT_RENDERED / CART_NO_MATCH /
        // CART_MATCH_FOUND_BUT_NO_ROW / CART_DELETE_NOT_EFFECTIVE 各代表不同处置
        box.innerHTML =
          `<span class="sess-warn">删除失败${d.code ? `［${esc(d.code)}］` : ""}：${esc(d.message || "")}</span>` +
          (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "");
        return;
      }
      const n = d.removed || 0;
      box.innerHTML =
        `<div class="exec-result ${n > 0 ? "ok" : "bad"}">` +
        `<b>${n > 0 ? "已删除 " + n + " 件" : "没有删除成功"}</b> ｜ 购物车 ${d.before ?? "?"} → ${d.after ?? "?"}` +
        (d.verified ? "（已回读确认）" : "（数量未回读，请看截图核对）") +
        (d.matched && d.matched.length ? `<br>命中：${esc(d.matched.join("；"))}` : "") +
        `</div>` +
        (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "");
    } catch (e) {
      box.innerHTML = `<span class="sess-warn">${esc(e.message)}</span>`;
    }
  }

  /** 只删上一批加购进去的商品：先预览（dry），确认后再真删。 */
  async function cartRemoveLastBatch() {
    if (!sessionReady) return toast("先启动淘宝会话");
    const box = $("#cart-remove-result");
    box.textContent = "正在核对上一批的加购记录…（会打开购物车页面）";

    let plan;
    try {
      plan = await api("/api/taobao/cart-remove", { source: "last_batch", dry: true });
    } catch (e) {
      box.innerHTML = `<span class="sess-warn">${esc(e.message)}</span>`;
      return;
    }
    if (!plan.ok) {
      box.innerHTML =
        `<span class="sess-warn">${esc(plan.message || "没有可删除的记录")}` +
        `${plan.code ? `［${esc(plan.code)}］` : ""}</span>`;
      return;
    }

    const del = plan.delete || [];
    const skip = plan.skip || [];
    const miss = plan.missing || [];
    const merged = plan.merged || [];
    const shown = del.slice(0, 8).map((d) => `<li>${esc(d.title)}（${d.row_qty} 件）</li>`).join("");
    box.innerHTML =
      `<div class="exec-result ${del.length ? "ok" : "bad"}">` +
      `<b>将删除 ${del.length} 件</b>：本工具上一批加进去的商品` +
      (skip.length
        ? `<br>跳过 ${skip.length} 件（你自己也往这些行加过数量）：${esc(
            skip.map((s) => `${s.title}（${s.reason || ""}）`).join("；")
          )}`
        : "") +
      (merged.length ? `<br>已合并 ${merged.length} 条（同一商品买过多次，购物车里只有一行）` : "") +
      (miss.length ? `<br>没找到 ${miss.length} 件（可能已手动删过，或在购物车其它页）` : "") +
      (del.length ? `<ul>${shown}${del.length > 8 ? `<li>…还有 ${del.length - 8} 件</li>` : ""}</ul>` : "") +
      `</div>`;

    if (!del.length) return;
    if (!window.confirm(`确认删除这 ${del.length} 件？只删本工具加购的，其余不动。`)) {
      box.insertAdjacentHTML("beforeend", `<div class="fineprint">已取消，未删除任何商品。</div>`);
      return;
    }

    box.insertAdjacentHTML("beforeend", `<div class="fineprint">删除中…</div>`);
    try {
      const d = await api("/api/taobao/cart-remove", { source: "last_batch" });
      const n = d.removed || 0;
      box.innerHTML =
        `<div class="exec-result ${n > 0 && d.verified ? "ok" : "bad"}">` +
        `<b>${n > 0 ? `已删除 ${n} 件` : "没有删除成功"}</b>｜购物车 ${d.before ?? "?"} → ${d.after ?? "?"}` +
        (d.verified ? "（已回读确认）" : "（件数未回读，请看截图核对）") +
        (d.code ? `［${esc(d.code)}］` : "") +
        (d.skip && d.skip.length ? `<br>跳过 ${d.skip.length} 件（你加过同款，未动）` : "") +
        (d.merged && d.merged.length ? `<br>已合并 ${d.merged.length} 条（本来就只有一行）` : "") +
        (d.missing && d.missing.length ? `<br>没找到 ${d.missing.length} 件` : "") +
        `</div>` +
        (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "");
    } catch (e) {
      box.innerHTML = `<span class="sess-warn">${esc(e.message)}</span>`;
    }
  }

  /* ---------- 事件 ---------- */

  function wire() {
    $("#file").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) parseFile(f).catch((err) => {
        setStatus(err.message, "err");
        toast(err.message);
      });
      e.target.value = "";
    });

    const dz = $("#dropzone");
    ["dragenter", "dragover"].forEach((ev) =>
      dz.addEventListener(ev, (e) => {
        e.preventDefault();
        dz.classList.add("drag");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => {
        e.preventDefault();
        dz.classList.remove("drag");
      })
    );
    dz.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) parseFile(f).catch((err) => setStatus(err.message, "err"));
    });

    $("#btn-sample").addEventListener("click", async () => {
      try {
        const text = await fetch("/samples/demo_bom.csv").then((r) => r.text());
        $("#bom-text").value = text;
        const parsed = await parsePasted(text);
        setStatus(`已载入示例（${parsed.rows.length} 行），正在分析…`);
        await analyze(parsed.rows);
      } catch (err) {
        setStatus(err.message, "err");
      }
    });

    $("#btn-analyze").addEventListener("click", async () => {
      const text = $("#bom-text").value.trim();
      try {
        if (text) {
          const parsed = await parsePasted(text);
          await analyze(parsed.rows);
        } else if (state.rows) {
          await analyze(state.rows);
        } else {
          toast("先上传文件或粘贴 CSV");
        }
      } catch (err) {
        setStatus(err.message, "err");
        toast(err.message);
      }
    });

    $("#btn-export").addEventListener("click", exportJson);
    $("#btn-export-csv").addEventListener("click", exportCsv);

    $("#btn-cand-sample").addEventListener("click", async () => {
      try {
        const text = await fetch("/samples/demo_candidates.csv").then((r) => r.text());
        $("#cand-text").value = text;
        toast("已载入示例候选（合成数据）");
      } catch {
        toast("示例候选加载失败");
      }
    });

    $("#btn-score").addEventListener("click", runScore);

    $("#score-line").addEventListener("change", () => {
      state.lastScore = null;
      $("#score-result").innerHTML = "";
    });

    $("#btn-session-start").addEventListener("click", startSession);
    $("#btn-session-stop").addEventListener("click", stopSession);
    const shutdownBtn = $("#btn-shutdown");
    if (shutdownBtn) shutdownBtn.addEventListener("click", shutdownService);
    const winBtn = $("#btn-sess-window");
    if (winBtn) winBtn.addEventListener("click", showBrowserWindow);
    const winHideBtn = $("#btn-sess-window-hide");
    if (winHideBtn) winHideBtn.addEventListener("click", hideBrowserWindow);
    const setsInput = $("#sets-input");
    if (setsInput) {
      setsInput.value = state.sets || 1;
      setsInput.addEventListener("change", async () => {
        const n = Math.max(1, Math.min(999, parseInt(setsInput.value, 10) || 1));
        setsInput.value = n;
        if (n === state.sets) return;
        state.sets = n;
        if (state.rows) {
          await analyze(state.rows);
          saveState();
          toast(`已按 ${n} 套重新计算用量`);
        }
      });
    }
    const recheckBtn = $("#btn-sess-recheck");
    if (recheckBtn) recheckBtn.addEventListener("click", recheckVerification);
    $("#btn-batch-start").addEventListener("click", () => startBatch({}));
    const retryBtn = $("#btn-batch-retry");
    if (retryBtn) retryBtn.addEventListener("click", () => startBatch({ mode: "retry" }));
    const resetBtn = $("#btn-batch-reset");
    if (resetBtn) resetBtn.addEventListener("click", resetBatchProgress);
    $("#btn-batch-stop").addEventListener("click", stopBatch);
    const exportBtn = $("#btn-batch-export");
    if (exportBtn)
      exportBtn.addEventListener("click", () => {
        // 直接下 CSV：导出的是「本次/上次批量」的逐行结果（含状态、金额、确认方式）
        window.location.href = "/api/taobao/batch/export.csv";
      });
    $("#btn-cart-view").addEventListener("click", execCart);
    $("#btn-cart-remove").addEventListener("click", () => cartRemove(false));
    $("#btn-cart-remove-batch").addEventListener("click", cartRemoveLastBatch);
    $("#btn-verify-show").addEventListener("click", showBrowserWindow);
    $("#btn-verify-recheck").addEventListener("click", recheckVerification);
    $("#btn-cart-clear").addEventListener("click", () => cartRemove(true));
    $("#btn-exec-search").addEventListener("click", execSearch);
    $("#btn-exec-open").addEventListener("click", execOpen);
    $("#exec-line").addEventListener("change", () => {
      exec.search = null;
      exec.chosen = null;
      exec.open = null;
      $("#exec-candidates").innerHTML = "";
      $("#exec-search-note").textContent = "";
      $("#btn-exec-open").disabled = true;
      $("#exec-detail").innerHTML = '<div class="empty">选一个候选打开，SKU 会按 BOM 参数自动匹配。</div>';
    });
  }

  wire();
  // 截图会在批量结束后自动清掉，旧图 404 时直接收起，别在页面上留个裂图
  document.addEventListener(
    "error",
    (e) => {
      const t = e.target;
      if (t && t.tagName === "IMG" && t.classList.contains("shot")) t.style.display = "none";
    },
    true
  );
  const restored = restoreState();
  pollTimer = setInterval(() => {
    if (serviceDown) return;
    pollSession();
    pollBatch();
  }, 3000);
  pollSession();
  pollBatch();

  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => {
      const el = $("#engine-ver");
      if (el) el.textContent = h.engine;
    })
    .catch(() => {});

  // 便于分享/自检：?demo=1 自动载入示例并分析；再加 &score=1 自动评分
  const params = new URLSearchParams(location.search);
  if (params.get("demo") === "1" && !restored) {
    setTimeout(async () => {
      $("#btn-sample").click();
      if (params.get("score") === "1") {
        for (let i = 0; i < 80 && !state.result; i++) {
          await new Promise((r) => setTimeout(r, 100));
        }
        await new Promise((r) => setTimeout(r, 250));
        $("#btn-cand-sample").click();
        await new Promise((r) => setTimeout(r, 400));
        $("#btn-score").click();
      }
    }, 120);
  }
})();
