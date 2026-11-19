/* BOM 选型台 · 前端（无框架、无外部依赖） */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const state = { rows: null, result: null, activeIndex: -1, lastScore: null, lastScoreIndex: -1, sets: 1,
                  batch: null, filter: "all", intakeName: "" };
  const exec = { lineIndex: -1, search: null, chosen: null, open: null, result: null };
  let sessionReady = false;
  let batchRunning = false;
  let batchHasRows = false; // 服务端有批次数据（前端就算没分析结果，也要把执行区露出来）
  let pollTimer = null;
  let serviceDown = false; // 「退出服务」后停止轮询，避免满屏报错
  // 实时进度：优先用服务端推送（EventSource）。定时轮询只作退路 ——
  // 后台标签页里浏览器会把 setInterval 限流到 1 分钟一次，进程在跑、
  // 页面却一动不动，用户就以为没跑。
  let batchES = null;
  let liveMode = "connecting"; // connecting | sse | poll
  let liveAt = 0; // 最近一次收到状态的时刻
  let liveRetry = null;

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
    if (batchES) {
      try {
        batchES.close();
      } catch {
        /* ignore */
      }
      batchES = null;
    }
    renderLiveDot();
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
    dismissResume(); // 开始分析新清单：上次那条提示不再有意义
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

  // sessionStorage 只活在一个标签页里：新开标签页 / 浏览器重启后它一定是空的。
  // 那时候如果只靠它，整个执行区（含批次进度）都隐藏，页面回到空白首屏 ——
  // 跑批明明在跑，用户看到的是「什么都没发生」。
  //
  // 但只在**正在跑批**时自动接上；平时服务端那份可能已经是几天前的旧表，
  // 一打开就铺满台面，用户会以为自己刚传过（实测反馈）。改为投放区挂一条
  // 「上次分析」提示，点「继续查看」才接上，「清空」则把落盘记录删掉。
  let pendingRestore = null;

  function whenText(iso) {
    const s = String(iso || "");
    return s.length >= 16 ? `${s.slice(5, 10)} ${s.slice(11, 16)}` : "";
  }

  function applyRestore(d, note) {
    const res = d && d.result;
    if (!res || !Array.isArray(res.lines) || !res.lines.length) return false;
    state.rows = Array.isArray(d.rows) && d.rows.length ? d.rows : null;
    state.result = res;
    state.sets = Number(res.sets) || 1;
    state.activeIndex = 0;
    state.lastScore = null;
    render();
    saveState(); // 接上之后刷新页面也还在，不用再点一次
    const total = (res.summary && res.summary.total) || res.lines.length;
    setStatus(note || `已接上上次分析：${total} 行（购物车和跑批进度都还在）。`);
    return true;
  }

  function showResumeBar(d) {
    pendingRestore = d;
    const res = (d && d.result) || {};
    const total = (res.summary && res.summary.total) || (res.lines || []).length;
    const meta = $("#resume-meta");
    if (meta) {
      const when = whenText(d.saved_at);
      meta.textContent = `${total ? `${total} 行` : "一份清单"}${when ? ` · ${when}` : ""}`;
    }
    const bar = $("#resume-bar");
    if (bar) bar.classList.remove("hidden");
  }

  function dismissResume() {
    pendingRestore = null;
    const bar = $("#resume-bar");
    if (bar) bar.classList.add("hidden");
  }

  async function restoreFromServer() {
    try {
      const [d, b] = await Promise.all([
        fetch("/api/last-analysis", { cache: "no-store" }).then((r) => r.json()),
        fetch("/api/taobao/batch", { cache: "no-store" })
          .then((r) => r.json())
          .catch(() => ({})),
      ]);
      const res = d && d.result;
      if (!res || !Array.isArray(res.lines) || !res.lines.length) return false;
      if (b && b.running) return applyRestore(d, "已接上正在跑的这一批（刷新/新开页面都不会断）。");
      showResumeBar(d);
      return false;
    } catch {
      return false;
    }
  }

  /* ---------- 渲染主结果 ---------- */
  function render() {
    $("#result-panel").classList.remove("hidden");
    const setsInput = $("#sets-input");
    if (setsInput) setsInput.value = state.sets || 1;
    // 有清单了就把它压成一条：投放区只在「还没东西」的时候需要占地方
    const intake = $("#input-panel");
    if (intake) intake.classList.add("collapsed");
    if ($("#intake-toggle")) $("#intake-toggle").setAttribute("aria-expanded", "false");
    renderIntakeBar();
    renderSummary();
    renderLineList();
    // 分析结果到位后要重判一次进度条：批次状态往往先于清单到达，
    // 那时 batchFits() 算不出「是不是这份清单」，会一直挂着「上一批（另一份清单）」。
    if (state.batch) renderBatch(state.batch);
    renderScoreSelect();
    renderExecSelect();
    const idx = state.activeIndex >= 0 ? state.activeIndex : 0;
    if (state.result.lines.length) renderDetail(idx);
    pollSession();
  }

  // 折叠条上要能一眼看出「现在看的是哪份、几行、几行能跑」
  function renderIntakeBar() {
    const r = state.result;
    if (!r) return;
    const name = $("#intake-name");
    const meta = $("#intake-meta");
    if (name) name.textContent = state.intakeName || "投放清单";
    if (meta) {
      const s = r.summary || {};
      meta.innerHTML = `<b>${s.total ?? r.lines.length}</b> 行 · <b>${s.ready ?? 0}</b> 可执行` +
        (s.needs_user ? ` · <b>${s.needs_user}</b> 待确认` : "");
    }
  }


  function renderSummary() {
    const r = state.result;
    if (!r) return;
    const s = r.summary || {};
    const results = batchFits() ? (state.batch && state.batch.results) || [] : [];
    const okN = results.filter((x) => x.status === "ok").length;
    const issueN = results.filter((x) => x.status === "failed" || x.status === "uncertain").length;
    const lcst = r.lcsc || {};
    const lcN = (lcst.by_code || 0) + (lcst.by_model || 0);
    const f = state.filter || "all";
    const chip = (key, label, count, cls, title) =>
      `<button class="chip ${cls}" data-filter="${key}" aria-pressed="${f === key ? "true" : "false"}"` +
      `${title ? ` title="${esc(title)}"` : ""}><b>${count}</b>${label}</button>`;

    let html =
      chip("all", "行", s.total ?? r.lines.length, "") +
      chip("ready", "可执行", s.ready ?? 0, "ok") +
      chip("buyok", "已加购", okN, "ok") +
      chip("issue", "待处理", (s.needs_user || 0) + issueN, "warn") +
      (lcN ? `<span class="chip plain" title="${esc(r.lcsc_summary || "")}"><b>${lcN}</b>立创核准</span>` : "");
    $("#summary").innerHTML = html;
    $("#summary").querySelectorAll("[data-filter]").forEach((b) =>
      b.addEventListener("click", () => {
        state.filter = b.dataset.filter;
        renderSummary();
        renderLineList();
      })
    );

    const old = $("#summary").parentElement.parentElement.querySelector(".warnbar");
    if (old) old.remove();
    if (r.warnings && r.warnings.length) {
      const div = document.createElement("div");
      div.className = "warnbar";
      div.innerHTML = "BOM 检查提示：<ul>" + r.warnings.map((w) => `<li>${esc(w)}</li>`).join("") + "</ul>";
      const bar = $("#summary").closest(".statusbar");
      if (bar) bar.insertAdjacentElement("afterend", div);
    }
  }


  function catLabel(c) {
    return { passive: "被动件", ic: "芯片", power: "功率", connector: "连接器", module: "模组", other: "其他" }[c] || c;
  }

  // 封装列只留第一个词：「0603 (1608)」→「0603」，宽度才够排五列
  function shortPkg(pkg) {
    return String(pkg || "").trim().split(/[\s(（]/)[0] || "—";
  }

  const BATCH_SHORT = {
    ok: ["已加购", "s-ok"],
    failed: ["失败", "s-bad"],
    uncertain: ["待核对", "s-unc"],
    running: ["加购中", "s-run"],
    pending: ["等待", "s-idle"],
    skipped: ["跳过", "s-idle"],
    removed: ["已删车", "s-idle"],
  };

  // 批次属于哪一份清单：服务端 progress 已按 BOM 指纹过滤过，total 对得上才是这一份。
  // 对不上就不能往行上写状态 —— 上一批 18 行会把这份 6 行的 #1..#6 全标成「已删车」，
  // 用户看到的是「这批都买过了」，其实是另一份 BOM 的记录。
  function batchFits() {
    const n = ((state.result && state.result.lines) || []).length;
    const st = state.batch || {};
    const total = st.total || (st.results || []).length || 0;
    if (!n || !total) return false;
    const pgTotal = (st.progress || {}).total || 0;
    return pgTotal ? pgTotal === n : total === n;
  }

  function batchRecMap() {
    const m = new Map();
    if (!batchFits()) return m;
    for (const r of (state.batch && state.batch.results) || []) m.set(Number(r.line_no), r);
    return m;
  }

  function buyCell(rec) {
    if (!rec) return '<span class="bstate s-idle"><b>未跑</b></span>';
    const [label, cls] = BATCH_SHORT[rec.status] || ["—", "s-idle"];
    const price = rec.cost != null ? rec.cost : rec.candidate && rec.candidate.price;
    let sub = price != null ? fmtMoney(price) : "";
    if (rec.status === "ok" && rec.cart_unverified) sub = "未核实";
    if ((rec.status === "failed" || rec.status === "uncertain") && rec.message) {
      // 错误分类：先给「可重试 / 需人工」一句话结论，再截原因。
      // 完整错误码挂在 title 里，悬停可见，不占列宽。
      const hint = rec.error_needs_human ? "需人工" : rec.error_retryable ? "可重试" : "";
      sub = (hint ? hint + "·" : "") + rec.message.slice(0, 10);
    }
    const tip = [rec.error_code, rec.message].filter(Boolean).join(" · ");
    return `<span class="bstate ${cls}"${tip ? ` title="${esc(tip)}"` : ""}><b>${esc(label)}</b>${sub ? `<i>${esc(sub)}</i>` : ""}</span>`;
  }

  function costCell(rec) {
    const v = rec ? (rec.cost != null ? rec.cost : rec.candidate && rec.candidate.price) : null;
    // 已删车 / 跳过 的金额是历史值，灰掉：别让人以为这一件还在购物车里
    const dim = rec && ["removed", "skipped"].includes(rec.status);
    return `<span class="li-cost${v == null || dim ? " none" : ""}">${v == null ? "—" : fmtMoney(v)}</span>`;
  }

  // 筛选：一眼只看「该处理的」或「已经买到的」
  function visibleRows(lines, bm) {
    const f = state.filter || "all";
    const rows = lines.map((ln, i) => ({ ln, i }));
    if (f === "all") return rows;
    return rows.filter(({ ln, i }) => {
      const rec = bm.get(Number(ln.line_no ?? i + 1));
      if (f === "ready") return ln.status === "ready";
      if (f === "buyok") return !!rec && rec.status === "ok";
      if (f === "issue")
        return ln.status !== "ready" || (!!rec && ["failed", "uncertain", "skipped"].includes(rec.status));
      return true;
    });
  }

  function renderLineList() {
    if (!state.result) return;
    const lines = state.result.lines;
    const bm = batchRecMap();
    const shown = visibleRows(lines, bm);
    const box = $("#line-list");
    if (!shown.length) {
      box.innerHTML =
        '<div class="empty"><span>这个筛选下没有行</span><span class="empty-sub">点上面第一颗「全部」看回所有行</span></div>';
      return;
    }
    box.innerHTML = shown
      .map(({ ln, i }) => {
        const no = ln.line_no ?? i + 1;
        const rec = bm.get(Number(no));
        const ready = ln.status === "ready";
        const critical = ln.criticality === "critical" ? '<span class="li-flag">关键</span>' : "";
        const nobuy = ln.dont_buy ? '<span class="li-flag nobuy">不买</span>' : "";
        return `<button class="line-item ${i === state.activeIndex ? "active" : ""}${ln.dont_buy ? " nobuy-row" : ""}" data-idx="${i}">
        <span class="li-no">${String(no).padStart(2, "0")}</span>
        <span class="li-body">
          <span class="li-title">${esc(ln.decision.headline || ln.mpn || "未识别")}${critical}${nobuy}</span>
          <span class="li-sub">${esc(ln.mpn || catLabel(ln.category))}</span>
        </span>
        <span class="li-pkg col-pkg">${esc(shortPkg(ln.package))}</span>
        <span class="li-qty col-qty">${ln.qty ?? "—"}</span>
        <span class="li-code col-code">${ln.lcsc && ln.lcsc.code ? esc(ln.lcsc.code) : "—"}</span>
        <span><span class="badge ${ready ? "" : "needs_user"}">${ready ? "可执行" : "待确认"}</span></span>
        <span>${buyCell(rec)}</span>
        <span class="col-cost">${costCell(rec)}</span>
      </button>`;
      })
      .join("");

    box.querySelectorAll("[data-idx]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const idx = Number(btn.dataset.idx);
        state.activeIndex = idx;
        box.querySelectorAll(".line-item").forEach((el) => el.classList.toggle("active", el === btn));
        renderDetail(idx);
        openDrawer("detail");
      })
    );
  }


  function renderDetail(idx) {
    state.activeIndex = idx;
    const ln = state.result.lines[idx];
    if (!ln) return;
    $("#line-list").querySelectorAll(".line-item").forEach((b, i) => {
      b.classList.toggle("active", i === idx);
    });

    const d = ln.decision;
    const rec = batchRecMap().get(Number(ln.line_no));
    const statusBadge =
      ln.status === "ready"
        ? `<span class="badge">可执行</span>`
        : `<span class="badge needs_user">待确认</span>`;
    const nobuyBadge = ln.dont_buy ? `<span class="badge idle">不买</span>` : "";
    const critBadge =
      ln.criticality === "critical" ? `<span class="badge critical">关键件，G3 逐行</span>` : "";
    const meta = [
      ln.mpn ? `<span>型号 <b>${esc(ln.mpn)}</b></span>` : "",
      ln.qty ? `<span>数量 <b>${esc(ln.qty)}</b></span>` : `<span>数量 <b>缺失</b></span>`,
      ln.package ? `<span>封装 <b>${esc(ln.package)}</b></span>` : "",
      ln.refs ? `<span>位号 <b>${esc(ln.refs)}</b></span>` : "",
      ln.brand ? `<span>品牌 <b>${esc(ln.brand)}</b></span>` : "",
      rec && (rec.shop || (rec.candidate && rec.candidate.shop))
        ? `<span>店铺 <b>${esc(rec.shop || rec.candidate.shop)}</b></span>`
        : "",
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
        (q, qi) =>
          `<div class="query edit"><input type="text" class="q-input" value="${esc(
            q
          )}" data-qi="${qi}" aria-label="搜索词 ${qi + 1}"><button type="button" class="q-copy" data-copy="${esc(
            q
          )}">复制</button></div>`
      )
      .join("");
    const queriesEmpty =
      (d.search_queries || []).length
        ? ""
        : '<p class="fineprint">参数不足，未生成搜索词。</p>';
    const verify = (d.must_verify || []).map((v) => `<li>${esc(v)}</li>`).join("");
    // 行级删车：只对这一行已经加购（或待核对）的记录出现。删完这行会变成
    // 「已删车」，可以重新跑，别的行和购物车里其它商品都不动。
    const cartDelBtn =
      rec && ["ok", "uncertain"].includes(rec.status)
        ? `<button class="ghost small danger" data-cartdel-idx="${idx}" type="button" title="只把这一行买的东西从购物车删掉，其它行不动">从购物车删掉</button>`
        : rec && rec.status === "removed"
          ? `<button class="ghost small" type="button" disabled title="这一行已经从购物车删除">已删车</button>`
          : "";
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
        <div class="detail-badges">${statusBadge}${nobuyBadge}${critBadge}</div>
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
          <h4>搜索词（按序使用，最多 4 条，可直接改）</h4>
          ${queries || queriesEmpty}
          <div class="query-actions">
            <button class="ghost small" id="btn-add-query" type="button">+ 加一条</button>
            <button class="primary small" id="btn-save-queries" type="button">保存搜索词</button>
          </div>
        </section>
      </div>
      <h4>必须核对</h4>
      <ol class="verify">${verify}</ol>
      <h4>风险提示</h4>
      <div class="risks">${risks}</div>
      <div class="detail-foot">
        <span>人工门：${esc(d.gate)}；manual_approval_required = true</span>
        <span class="foot-btns">
          <button class="primary small" data-run-idx="${idx}" type="button" ${
            ln.dont_buy
              ? 'disabled title="已标记不买，先点右边「恢复采购」"'
              : 'title="只执行这一行：搜索→评分→选规格→加购，不影响其它行"'
          }>只跑这一行</button>
          ${
            ln.status !== "ready"
              ? `<button class="primary small" data-confirm-idx="${idx}" type="button">我已人工核对，标记为可执行</button>`
              : ln.user_approved
                ? `<span class="badge ok-badge">人工已确认</span>`
                : ""
          }
          <button class="ghost small" data-dontbuy-idx="${idx}" type="button">${
            ln.dont_buy ? "恢复采购" : "这行不买了"
          }</button>
          ${cartDelBtn}
          <button class="ghost small" data-score-idx="${idx}" type="button">用这行评分</button>
        </span>
      </div>`;

    $("#line-detail")
      .querySelectorAll("[data-copy]")
      .forEach((b) =>
        b.addEventListener("click", () => {
          // 搜索词可编辑：复制按钮优先取输入框里的**当前**内容
          const input = b.closest(".query") && b.closest(".query").querySelector(".q-input");
          copyText(input ? input.value.trim() : b.dataset.copy);
        })
      );

    // 搜索词编辑：加一条 / 保存（写回 state + 落盘，刷新和续跑都在）
    const saveQueries = $("#line-detail").querySelector("#btn-save-queries");
    if (saveQueries) {
      saveQueries.addEventListener("click", async () => {
        const vals = [...$("#line-detail").querySelectorAll(".q-input")]
          .map((el) => el.value.trim())
          .filter(Boolean);
        if (!vals.length) {
          toast("至少要留一条搜索词，否则这一行没法跑");
          return;
        }
        if (vals.length > 4) {
          toast("最多 4 条，删掉几条再保存");
          return;
        }
        ln.query_plan = vals.slice();
        if (ln.decision) ln.decision.search_queries = vals.slice();
        try {
          await api("/api/lines/update", {
            index: idx,
            line_no: ln.line_no,
            patch: { query_plan: vals.slice() },
          });
        } catch (e) {
          // 服务端没存上（还没分析过/文件缺失）也不拦：前端内存是本次会话的事实来源
          console.warn("搜索词落盘失败：", e.message);
        }
        renderDetail(idx);
        toast("搜索词已保存");
      });
    }
    const addQueryBtn = $("#line-detail").querySelector("#btn-add-query");
    if (addQueryBtn) {
      addQueryBtn.addEventListener("click", () => {
        const list = $("#line-detail").querySelectorAll(".query.edit");
        if (list.length >= 4) {
          toast("最多 4 条搜索词");
          return;
        }
        const wrap = document.createElement("div");
        wrap.className = "query edit";
        wrap.innerHTML = `<input type="text" class="q-input" value="" aria-label="搜索词 ${
          list.length + 1
        }"><button type="button" class="q-copy">复制</button>`;
        const sec = addQueryBtn.closest("section");
        sec.insertBefore(wrap, sec.querySelector(".query-actions"));
        const input = wrap.querySelector(".q-input");
        input.addEventListener("input", () => {
          wrap.querySelector(".q-copy").dataset.copy = input.value;
        });
        input.focus();
      });
    }

    // 人工确认：把「待确认」放行成「可执行」。改的是这行的判定结果，
    // 不是引擎的规则 —— 该补的参数还是要在 BOM 里补。
    const confirmBtn = $("#line-detail").querySelector("[data-confirm-idx]");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", async () => {
        try {
          await api("/api/lines/update", {
            index: idx,
            line_no: ln.line_no,
            patch: { status: "ready", blocking: [], missing: [], user_approved: true },
          });
        } catch (e) {
          console.warn("确认落盘失败：", e.message);
        }
        ln.status = "ready";
        ln.blocking = [];
        ln.missing = [];
        ln.user_approved = true;
        if (state.result.summary) {
          state.result.summary.ready = (state.result.summary.ready || 0) + 1;
          state.result.summary.needs_user = Math.max(0, (state.result.summary.needs_user || 0) - 1);
        }
        renderSummary();
        renderIntakeBar();
        renderLineList();
        renderDetail(idx);
        toast(`第 ${ln.line_no} 行已人工确认，标记为可执行`);
      });
    }

    // 只跑这一行：个别行失败（换词/临时验证）时不用整批重跑 —— 整批会把
    // 已成功的行再加一遍车。走 /api/taobao/run-line，结果只落这一行。
    const runBtn = $("#line-detail").querySelector("[data-run-idx]");
    if (runBtn) {
      runBtn.addEventListener("click", async () => {
        if (runBtn.disabled) return;
        const oldText = runBtn.textContent;
        runBtn.disabled = true;
        runBtn.textContent = "正在跑…";
        try {
          const res = await api("/api/taobao/run-line", { line_no: ln.line_no, index: idx });
          try {
            const st = await fetch("/api/taobao/batch", { cache: "no-store" }).then((r) => r.json());
            applyBatchStatus(st);
          } catch {
            /* 状态刷新失败不影响结果本身 */
          }
          renderSummary();
          renderLineList();
          if (res.status === "ok") {
            toast(
              `第 ${ln.line_no} 行已加入购物车` +
                (res.final_price != null ? `（¥${res.final_price}）` : "")
            );
          } else if (res.status === "uncertain") {
            toast(`第 ${ln.line_no} 行已加购，但需要核对：${res.message || ""}`);
          } else {
            toast(`第 ${ln.line_no} 行没跑成：${res.message || res.code || "未知原因"}`);
          }
          renderDetail(idx);
        } catch (e) {
          const hint =
            e.code === "NO_SESSION"
              ? "先点右上角「启动淘宝会话」"
              : e.code === "BATCH_RUNNING"
                ? "等批量任务跑完再试"
                : e.message;
          toast(`这一行没跑起来：${hint}`);
          runBtn.disabled = false;
          runBtn.textContent = oldText;
        }
      });
    }

    // 标记不买 / 恢复采购：人工的采购决策（结构件、自制件、自己有库存…）。
    // 只影响跑批 —— 标了不买的行直接跳过并记「跳过：人工标记不买」，进度不欠账。
    const dontbuyBtn = $("#line-detail").querySelector("[data-dontbuy-idx]");
    if (dontbuyBtn) {
      dontbuyBtn.addEventListener("click", async () => {
        const to = !ln.dont_buy;
        try {
          await api("/api/lines/update", {
            index: idx,
            line_no: ln.line_no,
            patch: { dont_buy: to },
          });
        } catch (e) {
          console.warn("不买标记落盘失败：", e.message);
        }
        ln.dont_buy = to;
        renderLineList();
        renderDetail(idx);
        toast(
          to
            ? `第 ${ln.line_no} 行已标记不买，批量加购会跳过它`
            : `第 ${ln.line_no} 行已恢复采购`
        );
      });
    }

    // 行级删车：买错了/不想买这一件时，只把这行的商品从购物车删掉，
    // 不用整批重来，也不会动别的行。删完这行改回「待加购」，可重新跑。
    const cartDelEl = $("#line-detail").querySelector("[data-cartdel-idx]");
    if (cartDelEl) {
      cartDelEl.addEventListener("click", async () => {
        if (cartDelEl.disabled) return;
        const what = (rec && rec.candidate && rec.candidate.title) || ln.mpn || `第 ${ln.line_no} 行`;
        const ok = window.confirm(
          `把这一行买的东西从购物车删掉？\n\n` +
            `「${what}」\n\n` +
            `只删这一件（整行删除，包括你自己加的同款数量），购物车里其它商品不动。\n` +
            `删完这一行会改回「待加购」，可以重新跑。`
        );
        if (!ok) return;
        const oldText = cartDelEl.textContent;
        cartDelEl.disabled = true;
        cartDelEl.textContent = "正在删…";
        try {
          const res = await api("/api/taobao/cart-remove-line", { line_no: ln.line_no });
          try {
            const st = await fetch("/api/taobao/batch", { cache: "no-store" }).then((r) => r.json());
            applyBatchStatus(st);
          } catch {
            /* 状态刷新失败不影响删除结果 */
          }
          renderSummary();
          renderLineList();
          if (res.removed) {
            toast(
              `第 ${ln.line_no} 行已从购物车删除` +
                (res.before != null && res.after != null ? `（车内 ${res.before} → ${res.after} 件）` : "")
            );
          } else {
            toast(`购物车里没找到这一件，已把第 ${ln.line_no} 行改回「待加购」`);
          }
          renderDetail(idx);
        } catch (e) {
          const hint =
            e.code === "NO_SESSION"
              ? "先点右上角「启动淘宝会话」"
              : e.code === "BATCH_RUNNING"
                ? "等批量任务跑完再试"
                : e.message;
          toast(`这一行没删成：${hint}`);
          cartDelEl.disabled = false;
          cartDelEl.textContent = oldText;
        }
      });
    }

    const t = $("#drawer-title");
    const _ln = state.result.lines[idx] || {};
    if (t) t.textContent = `行 ${String(_ln.line_no ?? idx + 1).padStart(2, "0")}`;
    const scoreBtn = $("#line-detail").querySelector("[data-score-idx]");
    if (scoreBtn) {
      scoreBtn.addEventListener("click", () => {
        $("#score-line").value = String(idx);
        const fold = $(".score-fold");
        if (fold) fold.open = true;
        openDrawer("detail");
        const ta = $("#cand-text");
        if (ta) {
          ta.scrollIntoView({ behavior: "smooth", block: "center" });
          ta.focus();
        }
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
        (ln.status === "ready" ? "可执行" : "待确认") + (ln.dont_buy ? "（标记不买）" : ""),
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
      } else if (st.stalled) {
        // 进程活着但一直没就绪、也没进登录等待：这是卡住，不是慢
        label = "启动卡住了（浏览器没响应）";
        dot.classList.add("bad");
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
      ? "请在浏览器里手动完成验证，完成后点右侧按钮（或等它自动恢复）。窗口看不见就点上方「显示淘宝窗口」。"
      : st.waiting_login
      ? "登录页就在那个调试版 Edge 窗口里（不是新窗口）。找不到窗口就点上方「显示淘宝窗口」把它移回屏幕内。"
      : st.stalled
      ? `会话进程还在，但浏览器 ${
          Math.round(st.startup_timeout || 45)
        } 秒内没有响应。点「结束会话」再点「启动淘宝会话」重试；如果每次都卡在这里，先关掉杀毒软件对本目录的拦截（它可能拦了 Playwright 的 node.exe）。`
      : st.browser_gone
      ? "淘宝浏览器已关闭：点「结束会话」再「启动淘宝会话」即可恢复（也可以直接点启动）。"
      : st.desynced
      ? "请结束会话后重新启动。"
      : st.running && !st.ready && !st.waiting_login
      ? `正在打开 Edge 并加载淘宝，一般十几秒。超过 ${Math.round(
          st.startup_timeout || 45
        )} 秒窗口还没动静会自动提示「卡住了」——浏览器在别的窗口后面时，可以用任务栏找一下。`
      : st.running
      ? ""
      : st.last_error || "";
    const recheck = $("#btn-sess-recheck");
    if (recheck) recheck.classList.toggle("hidden", !st.verification);
    // 没启动时「启动淘宝会话」用实心主按钮样式 —— 用户反馈过找不到它在哪，
    // 灰 ghost 小按钮混在顶栏里毫无存在感；启动后就退回普通样式并禁用。
    const startBtn = $("#btn-session-start");
    startBtn.classList.toggle("cta", !st.running);
    if (st.running) startBtn.classList.remove("cta");
    // 投放区那行「先启动淘宝会话」的引导：会话跑起来后就撤掉，别占地方
    const hint = $("#sess-hint");
    if (hint) hint.classList.toggle("hidden", !!st.running);
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

  // 自动跑完这一行：和批量同一套逻辑（搜索→评分选优→匹配 SKU→加购），
  // 只是范围限于当前行。个别行失败时不用整批重跑，避免重复加购。
  async function execAuto() {
    const line = currentExecLine();
    if (!line) return toast("先在「BOM 行」里选一行");
    const idx = state.result.lines.indexOf(line);
    const btn = $("#btn-exec-auto");
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "正在跑…";
    const note = $("#exec-search-note");
    note.textContent = "正在跑这一行（搜索→评分→选规格→加购），通常几秒到十几秒…";
    try {
      const res = await api("/api/taobao/run-line", { line_no: line.line_no, index: idx });
      try {
        applyBatchStatus(await fetch("/api/taobao/batch", { cache: "no-store" }).then((r) => r.json()));
      } catch {
        /* 状态刷新失败不影响结果 */
      }
      renderSummary();
      renderLineList();
      if (res.status === "ok") {
        note.innerHTML = `<b>已加入购物车</b>${res.final_price != null ? `，¥${res.final_price}` : ""}${
          res.message ? `（${esc(res.message)}）` : ""
        }`;
        toast(`第 ${line.line_no} 行已加入购物车`);
      } else if (res.status === "uncertain") {
        note.innerHTML = `<span class="sess-warn">已加购但需核对：${esc(res.message || "")}</span>`;
        toast(`第 ${line.line_no} 行已加购，但需要核对`);
      } else {
        note.innerHTML = `<span class="sess-warn">没跑成：${esc(res.message || res.code || "未知原因")}</span>`;
        toast(`第 ${line.line_no} 行没跑成`);
      }
      if (idx === state.activeIndex) renderDetail(idx);
    } catch (e) {
      const hint =
        e.code === "NO_SESSION"
          ? "先点右上角「启动淘宝会话」"
          : e.code === "BATCH_RUNNING"
            ? "等批量任务跑完再试"
            : e.message;
      note.innerHTML = `<span class="sess-warn">没跑起来：${esc(hint)}</span>`;
      toast(`这一行没跑起来：${hint}`);
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
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
        }，${c.shop ? esc(c.shop) : c.is_tmall ? "天猫" : "店铺未知"}${
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
    if (b) b.addEventListener("click", () => execCart());
  }

  async function execCart(targetSel) {
    // 结果必须写在「点按钮的那个面板」里：
    // - 单行执行页的「查看购物车」→ #exec-add-result（SKU 面板里）
    // - 维护页的「查看明细」→ 传 #cart-view-result（维护页里）
    // 不能一律写 #exec-add-result：跑过单行执行后它一直存在，从维护页
    // 点「查看明细」结果就跑进隐藏的单行执行页，用户看着像按不动。
    const box =
      (targetSel && $(targetSel)) || $("#exec-add-result") || $("#cart-remove-result");
    if (!box) return;
    // 读车要 3 秒左右，先给个即时反馈，别让人以为按钮没反应
    box.innerHTML = `<div class="exec-result">正在读取购物车…</div>`;
    try {
      const d = await api("/api/taobao/cart", {});
      box.innerHTML = "";
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
      box.innerHTML = "";
      toast(e.message);
    }
  }

  /* ---------- 一键全部加购 ---------- */

  const BATCH_LABEL = { pending: "待处理", running: "进行中", ok: "已加购", failed: "失败", uncertain: "待核对", skipped: "跳过", removed: "已删车·待重加" };
  // 演练的 ok 是「选型过了」，不是「买到了」。写「已加购」会让人以为车里有货。
  function batchLabel(status, dry) {
    if (status === "ok" && dry) return "选型通过";
    return BATCH_LABEL[status] || status || "";
  }

  let batchProgress = null;

  async function startBatch(opts) {
    // 新标签页里前端可能没有结果（只有服务端有）：先补一次再判断
    if (!state.result && batchHasRows) await restoreFromServer();
    if (!state.result) return toast("先分析 BOM");
    if (!sessionReady) return toast("先启动淘宝会话");
    batchScrolled = false; // 新一轮跑批允许再滚一次到进度区
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
        `自动过滑块和刷新都没成功。窗口在屏幕外，点「打开淘宝窗口」完成验证，最多等你 ${mins} 分钟`;
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
    const total = st.total || results.length || 0;
    if (!total) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    const settled = results.filter((x) =>
      ["ok", "failed", "uncertain", "skipped", "removed"].includes(x.status)
    ).length;
    const done = st.done != null ? st.done : settled;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const now = results.find((x) => x.status === "running");
    const stale = !batchFits() && !st.running;
    const nowTxt = now
      ? `正在跑 #${now.line_no} ${now.headline || ""}`
      : st.running
      ? st.phase || "准备中"
      : stale
      ? "上一批（另一份清单）的记录，不写进这张表"
      : st.single_line && st.single_line.line_no != null
      ? `单行执行 #${st.single_line.line_no}${BATCH_LABEL[st.single_line.status] ? " " + BATCH_LABEL[st.single_line.status] : ""}`
      : "已停止";
    box.innerHTML = `<span class="rail-num">${done}/${total}</span>
      <span class="rail-track"><span class="rail-fill" style="width:${pct}%"></span></span>
      <span class="rail-now">${esc(nowTxt)}</span>`;
  }


  async function pollBatch() {
    // 推送还活着就别再问了：两套一起跑只会让「到底谁在刷新」更难查
    if (batchES && batchES.readyState === 1 && liveMode === "sse") return;
    let st;
    try {
      st = await fetch("/api/taobao/batch", { cache: "no-store" }).then((r) => r.json());
    } catch {
      renderLiveDot(); // 连不上：让状态点自己说「断开多久了」
      return;
    }
    if (liveMode === "sse") return; // 推送恢复，轮询结果丢掉，免得覆盖
    setLiveMode("poll");
    liveAt = Date.now();
    applyBatchStatus(st);
    renderLiveDot();
  }

  /* ---------- 实时进度（服务端推送 + 轮询兜底） ---------- */

  function setLiveMode(mode) {
    if (liveMode !== mode) liveMode = mode;
  }

  function startBatchStream() {
    if (serviceDown || !window.EventSource || batchES) return;
    try {
      batchES = new EventSource("/api/taobao/batch/stream");
    } catch {
      batchES = null;
      return;
    }
    batchES.onopen = () => {
      liveAt = Date.now();
      setLiveMode("sse");
      renderLiveDot();
    };
    batchES.onmessage = (ev) => {
      let st;
      try {
        st = JSON.parse(ev.data);
      } catch {
        return;
      }
      liveAt = Date.now();
      if (liveMode !== "sse") setLiveMode("sse");
      if (!st.error) applyBatchStatus(st);
      renderLiveDot();
    };
    // 服务端 10 分钟会主动收尾、服务重启也会断流 —— 退回轮询，15 秒后再挂回去
    batchES.onerror = () => {
      if (batchES) {
        batchES.close();
        batchES = null;
      }
      setLiveMode("poll");
      renderLiveDot();
      if (!liveRetry && !serviceDown) {
        liveRetry = setTimeout(() => {
          liveRetry = null;
          startBatchStream();
        }, 15000);
      }
    };
  }

  /** 连接状态点：让人一眼看出「页面还在收实时进度 / 已经断了多久」。 */
  function renderLiveDot() {
    const el = $("#live-dot");
    if (!el) return;
    el.classList.remove("ok", "warn", "bad", "hidden");
    if (serviceDown) {
      el.classList.add("hidden");
      return;
    }
    const age = liveAt ? Math.round((Date.now() - liveAt) / 1000) : null;
    if (age != null && age > 15) {
      el.classList.add("bad");
      el.textContent = age > 60 ? "进度已断开（刷新页面）" : `已断开 ${age}s`;
      return;
    }
    if (liveMode === "sse") {
      el.classList.add("ok");
      el.textContent = "实时";
    } else if (liveMode === "poll") {
      el.classList.add("warn");
      el.textContent = "轮询中";
    } else {
      el.textContent = "连接中…";
    }
  }

  function openDrawer(tab) {
    const d = $("#drawer");
    if (!d) return;
    d.classList.remove("hidden");
    if (tab) setDrawerTab(tab);
  }

  function closeDrawer() {
    const d = $("#drawer");
    if (d) d.classList.add("hidden");
  }

  function setDrawerTab(name) {
    document.querySelectorAll(".dtab").forEach((b) =>
      b.classList.toggle("active", b.dataset.tab === name)
    );
    document.querySelectorAll(".drawer-pane").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.pane !== name)
    );
    const d = $("#drawer");
    if (d) d.classList.remove("hidden");
  }

  // 旧版要把下半屏的「执行区」露出来；现在只有一个工作台，跑批时开抽屉的单行执行页
  function ensureExecVisible() {
    const d = $("#drawer");
    if (d && d.classList.contains("hidden")) return; // 不替用户抢开抽屉
  }


  // 进度轨就在首屏顶栏下面，开跑时不需要滚动；只有用户滚到很下面时才拉回视野一次
  let batchScrolled = false;
  function revealBatch() {
    if (batchScrolled) return;
    const box = $("#batch-progress");
    if (!box || box.classList.contains("hidden")) return;
    const r = box.getBoundingClientRect();
    if (r.top < 0) box.scrollIntoView({ behavior: "smooth", block: "start" });
    batchScrolled = true;
  }


  function applyBatchStatus(st) {
    state.batch = st;
    batchRunning = !!st.running;
    batchProgress = st.progress || batchProgress;
    // 服务端有批次数据就要把执行区露出来：刷新/新开标签页后前端记忆可能没了，
    // 但跑批的实时状态一直在服务端。把整块藏起来等于「页面不显示真实状态」。
    const nRows = (st.results || []).length;
    batchHasRows = nRows > 0 || (st.total || 0) > 0 || !!((st.progress || {}).total);
    if (batchHasRows) ensureExecVisible();
    renderBatch(st);
    // 行状态就写在台账里：不重渲的话「已加购」只存在于进度条，表格永远停在「未跑」
    if (state.result) {
      renderLineList();
      renderSummary();
    }
    renderVerifyBanner(st);
    syncBatchButtons();
    $("#btn-batch-stop").disabled = !batchRunning;
    const isDry = !!st.dry_run;
    if (st.warning) {
      // 整批空跑 / 进度错配这类严重情况，直接把原因摆在进度条上
      $("#batch-note").textContent = st.warning;
    } else if (batchRunning) {
      const money = !isDry && st.cost_total ? `，已花 ${fmtMoney(st.cost_total)}` : "";
      $("#batch-note").textContent = isDry
        ? `演练中（不点加购）：${st.phase || ""}，已完成 ${st.done}/${st.total}`
        : `${st.phase || "进行中"}，已完成 ${st.done}/${st.total}${money}；每行约 20 到 60 秒，浏览器窗口请别关`;
      revealBatch();
    } else if (st.total) {
      const okn = st.ok_count != null ? st.ok_count : (st.results || []).filter((r) => r.status === "ok").length;
      const pg = st.progress || {};
      const { allProcessed } = batchMode();
      // 跳过/失败的行要在收尾一句话里点名，否则「已加购 108 行」看不出还有 11 行没处理
      const skipped = (st.results || []).filter((r) => r.status === "skipped").length;
      const failed = (st.results || []).filter((r) => r.status === "failed").length;
      const aside = [skipped ? `跳过 ${skipped} 行` : "", failed ? `失败 ${failed} 行` : ""].filter(Boolean).join("、");
      // 收尾对账没做成：先说清楚「这次没核实」，再报结果，别让人以为都核对过了
      const rec = st.reconcile || {};
      const unverified = (st.results || []).filter((r) => r.cart_unverified).length;
      const warn = rec.anomaly
        ? `⚠ 收尾对账没做成（${rec.anomaly_reason || "购物车没读对"}），${unverified} 行标了「未核实」，请到购物车核对；`
        : "";
      if (isDry) {
        // 演练一件都没买：不报金额，也别劝人「重跑」（本来就没花钱）。
        $("#batch-note").textContent =
          `${warn}演练（未加购）：选型通过 ${okn} 行${aside ? `，${aside}` : ""}` +
          `${st.stopped_reason ? "（" + st.stopped_reason + "）" : ""}；` +
          `这次一件都没买，确认没问题再点「一键全部加购」`;
        return;
      }
      const tail = allProcessed
        ? "；这批已跑完，要重跑点「重跑问题行」或「重新跑全部」（会重复加购，先删上一批）"
        : pg.ok_count > 0
        ? "；进度已保存，下次点「继续加购」从断点接着跑"
        : "";
      const money = st.cost_total
        ? `，合计 ${fmtMoney(st.cost_total)}${st.cost_lines ? `（${st.cost_lines} 行有实付价）` : ""}`
        : "";
      // 同店合并的战果：货散在几家店、运费算了几个。合并生效时直接报省了多少，
      // 没读到店名也要说出来，别让人以为「系统已经帮你合并过了」。
      const shops = st.shops || {};
      let shopNote = "";
      if (!isDry && shops.shop_count) {
        shopNote =
          `；货在 ${shops.shop_count} 家店（${shops.line_count} 行），预计运费 ${fmtMoney(shops.freight_est)}` +
          (shops.freight_saved > 0 ? `，同店合并省了约 ${fmtMoney(shops.freight_saved)}` : "");
      } else if (!isDry && shops.unknown) {
        shopNote = "；没读到店铺名，同店合并这次没生效";
      }
      // 单行执行过：进度条上的 1/53 是「这一行定局了」，别让人当成整批跑完
      const sl = st.single_line && st.single_line.line_no != null ? st.single_line : null;
      const slNote = sl
        ? `单行执行：第 ${sl.line_no} 行${BATCH_LABEL[sl.status] || ""}（进度 ${st.done}/${st.total}${
            st.done < st.total ? "，整批未跑完" : ""
          }）；`
        : "";
      $("#batch-note").textContent =
        `${warn}${slNote}${sl ? "整批" : "上次批量"}：已加购 ${okn} 行${money}${shopNote}${aside ? `，${aside}` : ""}` +
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
    // 前端没有分析结果、但服务端还有落盘的行时也要能点：
    // 刷新/新标签页后 state.result 是空的，可上一批的行明明还在 runs/batch-lines.json 里。
    btn.disabled = !sessionReady || batchRunning || !(state.result || batchHasRows);
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

  /** 整车删除的统一二次确认：必须手输「清空」，防止误点。 */
  function confirmClearCart() {
    const t = window.prompt(
      "这会删掉购物车里的全部商品（包括你自己加的非 BOM 商品），不可撤销。\n如果确定，请输入：清空"
    );
    if (t !== "清空") {
      toast("已取消");
      return false;
    }
    return true;
  }

  async function cartRemove(removeAll) {
    if (!sessionReady) return toast("先启动淘宝会话");
    if (batchRunning) return toast("正在加购，先点「停止」再删购物车商品");
    let payload;
    if (removeAll) {
      if (!confirmClearCart()) return;
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

  /**
   * 页面顶部的常驻「清空购物车」：不依赖上传清单、不依赖点过某一行。
   * 和行内删车走同一个后端接口、同一套二次确认，只是结果写在页面顶部，
   * 免得用户在没打开行详情时点了按钮却看不到反馈。
   */
  async function cartQuickClear() {
    if (!sessionReady) return toast("先启动淘宝会话");
    if (batchRunning) return toast("正在加购，先点「停止」再清空购物车");
    if (!confirmClearCart()) return;

    const btn = $("#btn-cart-quick-clear");
    const box = $("#cart-quick-result");
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "清空中…";
    box.className = "cq-result";
    box.textContent = "正在打开购物车并删除…";
    try {
      const d = await api("/api/taobao/cart-remove", { all: true, confirm: "清空" });
      if (!d.ok) {
        box.classList.add("bad");
        box.textContent = `删除失败${d.code ? `［${d.code}］` : ""}：${d.message || "未知错误"}`;
        toast("清空购物车失败");
        return;
      }
      const n = d.removed || 0;
      const emptyAlready = n === 0 && d.after === 0;
      const good = n > 0 || emptyAlready;
      const label = n > 0 ? `已清空 ${n} 件` : emptyAlready ? "购物车本来就是空的" : "没有删除成功";
      box.classList.add(good ? "ok" : "bad");
      box.textContent =
        `${label}｜购物车 ${d.before ?? "?"} → ${d.after ?? "?"}` +
        (d.verified ? "（已回读确认）" : "（数量未回读，请到淘宝核对）");
      toast(label);
    } catch (e) {
      box.classList.add("bad");
      box.textContent = e.message;
      toast(`清空失败：${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  /** 只删上一批加购进去的商品：先预览（dry），确认后再真删。 */
  async function cartRemoveLastBatch() {
    if (!sessionReady) return toast("先启动淘宝会话");
    const box = $("#cart-remove-result");
    box.textContent = "正在核对最近一次加购记录…（会打开购物车页面）";

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
    const bt = plan.batch || {};
    // 删的到底是哪一批，必须写死在这句话里：用户说的「上一批」常常指更早那份 BOM，
    // 而这里能动的是**最近跑完的这批**。2026-11-16 真机踩到：新 BOM 刚加完 17 件，
    // 点「删上次加购的」把新加的当上一批全删了，还以为是清旧货。
    const when = String(bt.finished_at || bt.started_at || "").replace("T", " ").slice(0, 16);
    const ageMin = bt.finished_at ? (Date.now() - new Date(bt.finished_at).getTime()) / 60000 : null;
    const fresh = ageMin != null && ageMin >= 0 && ageMin < 30;
    const shown = del.slice(0, 8).map((d) => `<li>${esc(d.title)}（${d.row_qty} 件）</li>`).join("");
    box.innerHTML =
      `<div class="exec-result ${del.length ? "ok" : "bad"}">` +
      `<b>将删除 ${del.length} 件</b>：${esc(when || "时间未知")} 这批加购的（共 ${bt.total ?? "?"} 行` +
      `${bt.from_archive ? "，取自历史底档" : ""}）` +
      (skip.length
        ? `<br>跳过 ${skip.length} 件（你自己也往这些行加过数量）：${esc(
            skip.map((s) => `${s.title}（${s.reason || ""}）`).join("；")
          )}`
        : "") +
      (merged.length ? `<br>已合并 ${merged.length} 条（同一商品买过多次，购物车里只有一行）` : "") +
      (miss.length ? `<br>没找到 ${miss.length} 件（可能已手动删过，或在购物车其它页）` : "") +
      (del.length ? `<ul>${shown}${del.length > 8 ? `<li>…还有 ${del.length - 8} 件</li>` : ""}</ul>` : "") +
      (fresh ? `<br><b>注意：这批是 ${Math.max(1, Math.round(ageMin))} 分钟前才加进去的。</b>` : "") +
      `</div>`;

    if (!del.length) return;
    const ask =
      `确认删除这 ${del.length} 件？\n\n` +
      `删的是 ${when || "时间未知"} 这批（共 ${bt.total ?? "?"} 行），不是更早的 BOM。` +
      (fresh ? "\n\n⚠️ 这批刚加购不久，删掉后需要重新跑一遍加购。" : "") +
      `\n\n只删本工具加购的，其余不动。`;
    if (!window.confirm(ask)) {
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
        (d.progress_invalidated
          ? `<br>进度已把这 ${d.progress_invalidated} 行改回「待加购」：点「一键全部加购」会重新加回去（不会因为「已加过」被跳过）。`
          : "") +
        `</div>` +
        (d.screenshot ? `<img class="shot" src="${d.screenshot}">` : "");
      if (n > 0) pollBatch().catch(() => {});
    } catch (e) {
      box.innerHTML = `<span class="sess-warn">${esc(e.message)}</span>`;
    }
  }

  /* ---------- 事件 ---------- */

  function wire() {
    // 投放条折叠 / 抽屉开关 / 详情页签
    const it = $("#intake-toggle");
    if (it) {
      it.addEventListener("click", () => {
        const box = $("#input-panel");
        const collapsed = box.classList.toggle("collapsed");
        it.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
    }
    const dc = $("#drawer-close");
    if (dc) dc.addEventListener("click", closeDrawer);
    document.querySelectorAll(".dtab").forEach((b) =>
      b.addEventListener("click", () => setDrawerTab(b.dataset.tab))
    );
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });

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

    $("#btn-resume-open").addEventListener("click", () => {
      const d = pendingRestore;
      if (!d) return;
      dismissResume();
      if (applyRestore(d)) toast("已接上上次分析，购物车和跑批进度都还在");
    });

    $("#btn-resume-clear").addEventListener("click", async () => {
      if (!window.confirm("清空「上次分析」的记录？\n\n购物车和跑批进度都不动；清完请重新上传或粘贴清单。")) return;
      dismissResume();
      try {
        sessionStorage.removeItem("bom.result");
      } catch {
        /* 存不存无所谓 */
      }
      try {
        await api("/api/last-analysis/clear", {});
        setStatus("上次分析已清空，请上传或粘贴新的清单。");
      } catch (err) {
        toast(`清空失败：${err.message}`);
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
    $("#btn-cart-view").addEventListener("click", () => execCart("#cart-view-result"));
    $("#btn-cart-remove").addEventListener("click", () => cartRemove(false));
    $("#btn-cart-remove-batch").addEventListener("click", cartRemoveLastBatch);
    $("#btn-verify-show").addEventListener("click", showBrowserWindow);
    $("#btn-verify-recheck").addEventListener("click", recheckVerification);
    $("#btn-cart-clear").addEventListener("click", () => cartRemove(true));
    $("#btn-cart-quick-clear").addEventListener("click", cartQuickClear);
    $("#btn-exec-search").addEventListener("click", execSearch);
    $("#btn-exec-auto").addEventListener("click", execAuto);
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

  // 页脚地址跟着实际访问地址走：服务可以用 --port 8768 起，写死 8765 会让人照着去排查。
  // 复测 P2：8768 上跑着，页脚还写着 127.0.0.1:8765。
  const footLocal = $("#foot-local");
  if (footLocal && location.host) footLocal.textContent = `本地运行 · ${location.host}`;

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
  // 本标签页没记忆就从服务端补（新标签页/浏览器重启的主路径）
  if (!restored) restoreFromServer();
  pollTimer = setInterval(() => {
    if (serviceDown) return;
    pollSession();
    pollBatch();
    // 推送连着却很久没消息（服务端线程卡住 / 中间层把流攒住了）：
    // 主动掐断退回轮询，再挂回去。宁可退化成 3 秒轮询，也不能页面静默不动。
    if (batchES && liveAt && Date.now() - liveAt > 20000) {
      try {
        batchES.close();
      } catch {
        /* 已经断了 */
      }
      batchES = null;
      setLiveMode("poll");
      if (!liveRetry && !serviceDown) {
        liveRetry = setTimeout(() => {
          liveRetry = null;
          startBatchStream();
        }, 15000);
      }
    }
    renderLiveDot();
  }, 3000);
  // 切回这个标签页、或窗口重新获得焦点时立刻对齐一次：
  // 后台标签页里定时器会被浏览器限流，回来时不能等下一拍才刷新。
  const wake = () => {
    if (serviceDown) return;
    pollSession();
    pollBatch();
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) wake();
  });
  window.addEventListener("focus", wake);
  startBatchStream();
  pollSession();
  pollBatch();

  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => {
      const el = $("#engine-ver");
      if (el) el.textContent = h.engine;
      // 页脚版本号以服务端为准（HTML 里写死过 v0.9.1，服务升到 0.9.6 都没跟着变）
      const ver = $("#app-ver");
      if (ver) ver.textContent = h.version ? "v" + h.version : "—";
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
