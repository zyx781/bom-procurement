/* 运行日志页：读环境摘要、预览日志、导出脱敏包。
   所有外部文本一律走 textContent —— 日志里可能带用户输入的商品名，
   拼 innerHTML 等于给自己开一个 XSS 口子。 */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };

  var el = {
    ver: $("#ver"),
    env: $("#env"),
    tabs: $("#tabs"),
    log: $("#log"),
    logNote: $("#log-note"),
    note: $("#note"),
    bom: $("#with-bom"),
    hint: $("#hint"),
    exportBtn: $("#btn-export"),
    copyBtn: $("#btn-copy"),
    refreshBtn: $("#btn-refresh")
  };

  var summary = null;
  var activeKey = null;

  /* ---------- 小工具 ---------- */
  function fmtSize(bytes) {
    var n = Number(bytes) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function setHint(text, kind) {
    el.hint.textContent = text || "";
    el.hint.className = "diag-hint" + (kind ? " " + kind : "");
  }

  function row(label, value, cls) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value == null || value === "" ? "—" : String(value);
    if (cls) dd.className = cls;
    return [dt, dd];
  }

  function group(title) {
    var d = document.createElement("div");
    d.className = "grp";
    d.textContent = title;
    return d;
  }

  function sessionLabel(s) {
    if (!s || s.error) return { text: "读取失败", cls: "bad" };
    if (!s.running) return { text: "未启动", cls: "muted" };
    if (s.stalled) return { text: "启动卡住了", cls: "bad" };
    if (s.verification) return { text: "等待验证", cls: "warn" };
    if (s.waiting_login) return { text: "等待登录", cls: "warn" };
    if (s.ready) {
      return s.login_state === "ok"
        ? { text: "已就绪（已登录）", cls: "ok" }
        : { text: "已就绪（登录状态未确认）", cls: "warn" };
    }
    return { text: "启动中", cls: "warn" };
  }

  function depText(info) {
    if (!info) return "未知";
    if (!info.installed) return "未安装";
    return info.version || "已安装";
  }

  /* ---------- 渲染 ---------- */
  function renderEnv(data) {
    var frag = document.createDocumentFragment();
    var s = data.session || {};
    var sess = sessionLabel(s);

    frag.appendChild(group("服务"));
    row("应用版本", data.app_version).forEach(function (n) { frag.appendChild(n); });
    row("引擎版本", data.engine_version).forEach(function (n) { frag.appendChild(n); });
    row("进程 PID", data.pid).forEach(function (n) { frag.appendChild(n); });
    if (data.disk && data.disk.free_mb != null) {
      var low = data.disk.free_mb < 1024;
      row("磁盘剩余", data.disk.free_mb + " MB / " + data.disk.total_mb + " MB", low ? "bad" : "")
        .forEach(function (n) { frag.appendChild(n); });
    }

    frag.appendChild(group("运行环境"));
    row("Python", data.python).forEach(function (n) { frag.appendChild(n); });
    row("系统", data.os).forEach(function (n) { frag.appendChild(n); });
    var browser = data.browser || {};
    var found = browser.found || [];
    var bname = found.length ? String(found[0]).split(/[\\/]/).pop() : "未找到 Edge / Chrome";
    var bnode = row("浏览器", bname, found.length ? "" : "bad");
    if (found.length) bnode[1].title = found[0];
    bnode.forEach(function (n) { frag.appendChild(n); });
    row("CDP 端口",
        browser.cdp_port + (browser.cdp_listening ? " 可连接" : " 未监听"),
        s.running && !browser.cdp_listening ? "warn" : "")
      .forEach(function (n) { frag.appendChild(n); });

    frag.appendChild(group("依赖"));
    var deps = data.deps || {};
    [["playwright", deps.playwright, true],
     ["openpyxl", deps.openpyxl, true],
     ["ddddocr", deps.ddddocr, false],
     ["opencv", deps.opencv, false]].forEach(function (item) {
      var name = item[0], info = item[1], required = item[2];
      var text = depText(info) + (info && info.installed ? "" : required ? "（必需）" : "（可选）");
      row(name, text, info && info.installed ? "" : required ? "bad" : "muted")
        .forEach(function (n) { frag.appendChild(n); });
    });

    frag.appendChild(group("淘宝会话"));
    var sessNode = row("状态", sess.text, sess.cls);
    sessNode.forEach(function (n) { frag.appendChild(n); });
    if (s.running && s.started_at) {
      row("启动时间", s.started_at).forEach(function (n) { frag.appendChild(n); });
    }
    if (s.last_error) {
      var errNode = row("最近错误", s.last_error, "bad");
      errNode.forEach(function (n) { frag.appendChild(n); });
    }
    var h = data.history || {};
    var hl = h.last || {};
    var htext = hl.at
      ? "扩展实时删；关窗后兜底 " + hl.at.slice(11) + " 清 " + (Number(hl.urls) || 0) + " 条"
      : "扩展实时删除（关窗后再兜底清库）";
    if (h.enabled === false) htext = "已关闭（settings.json: clear_taobao_history）";
    row("淘宝历史", htext, h.enabled === false ? "warn" : "").forEach(function (n) {
      frag.appendChild(n);
    });

    frag.appendChild(group("日志"));
    var runs = data.runs || {};
    row("日志文件", (data.logs || []).length + " 个").forEach(function (n) { frag.appendChild(n); });
    row("日志体积", (runs.log_size_mb || 0) + " MB").forEach(function (n) { frag.appendChild(n); });
    var cerr = Number(data.client_errors) || 0;
    row("页面错误", cerr ? cerr + " 条（见 client.log）" : "无", cerr ? "warn" : "ok")
      .forEach(function (n) { frag.appendChild(n); });

    el.env.textContent = "";
    el.env.appendChild(frag);
  }

  function renderTabs(logs) {
    el.tabs.textContent = "";
    if (!logs || !logs.length) {
      var empty = document.createElement("span");
      empty.className = "empty";
      empty.textContent = "还没有日志文件";
      el.tabs.appendChild(empty);
      return;
    }
    logs.forEach(function (item) {
      var b = document.createElement("button");
      b.type = "button";
      b.dataset.key = item.key;
      b.textContent = item.name;
      b.title = item.name + " · " + fmtSize(item.size) + " · " + item.mtime;
      if (item.key === activeKey) b.className = "active";
      b.addEventListener("click", function () { selectLog(item.key); });
      el.tabs.appendChild(b);
    });
  }

  async function selectLog(key) {
    activeKey = key;
    Array.prototype.forEach.call(el.tabs.children, function (b) {
      if (b.dataset) b.classList.toggle("active", b.dataset.key === key);
    });
    el.log.textContent = "正在读取…";
    el.logNote.textContent = "";
    try {
      var r = await fetch("/api/diag/log?name=" + encodeURIComponent(key), { cache: "no-store" });
      var d = await r.json();
      if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
      el.log.textContent = d.text || "（这个文件是空的）";
      var item = (summary.logs || []).filter(function (x) { return x.key === key; })[0];
      el.logNote.textContent = item
        ? "只显示最后 800 行（共 " + fmtSize(item.size) + "，更新于 " + item.mtime + "）。完整内容在导出的日志包里。"
        : "";
      /* 最新的在最下面，打开就滚到底 */
      el.log.scrollTop = el.log.scrollHeight;
    } catch (e) {
      el.log.textContent = "读不到这个日志：" + e.message;
    }
  }

  /* ---------- 加载 ---------- */
  async function loadSummary(keepLog) {
    el.refreshBtn.disabled = true;
    try {
      var r = await fetch("/api/diag/summary", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      summary = await r.json();
      el.ver.textContent = summary.app_version || "—";
      renderEnv(summary);
      var logs = summary.logs || [];
      if (!logs.length) {
        activeKey = null;
        el.log.textContent = "还没有日志文件。启动过一次服务后这里才会有内容。";
        el.logNote.textContent = "";
      } else {
        var exists = logs.some(function (x) { return x.key === activeKey; });
        if (!keepLog || !exists) activeKey = logs[0].key;
        renderTabs(logs);
        await selectLog(activeKey);
      }
    } catch (e) {
      el.env.textContent = "";
      var d = document.createElement("div");
      d.className = "diag-loading";
      d.textContent = "读取失败：" + e.message + "。服务可能已经停了，重开一次再试。";
      el.env.appendChild(d);
      setHint("环境信息读取失败", "bad");
    } finally {
      el.refreshBtn.disabled = false;
    }
  }

  /* ---------- 导出 ---------- */
  async function exportBundle() {
    el.exportBtn.disabled = true;
    setHint("正在打包…");
    try {
      var r = await fetch("/api/diag/bundle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          note: el.note.value || "",
          include_bom: !!el.bom.checked
        })
      });
      var ctype = r.headers.get("Content-Type") || "";
      if (!r.ok || ctype.indexOf("zip") < 0) {
        var msg = "HTTP " + r.status;
        try {
          var j = await r.json();
          msg = j.error || msg;
        } catch (e) { /* 不是 JSON，就用状态码 */ }
        throw new Error(msg);
      }
      var blob = await r.blob();
      var cd = r.headers.get("Content-Disposition") || "";
      var name = "bom-logs.zip";
      var m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
      if (m) {
        try { name = decodeURIComponent(m[1]); } catch (e) { /* 保留默认名 */ }
      }
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      setHint("已下载 " + name + "（" + fmtSize(blob.size) + "）—— 把这个文件发出去就行。", "ok");
      /* 导出成功顺手刷新一次环境，维护者看到的和用户发的一致 */
      loadSummary(true);
    } catch (e) {
      setHint("导出失败：" + e.message + "。可以先看下面的日志，或把 runs 文件夹压缩发过去。", "bad");
      if (window.__reportProblem) window.__reportProblem("export-failed", e.message, "");
    } finally {
      el.exportBtn.disabled = false;
    }
  }

  /* ---------- 复制 ---------- */
  function envText() {
    if (!summary) return "";
    var s = summary.session || {};
    var deps = summary.deps || {};
    var found = (summary.browser || {}).found || [];
    var lines = [
      "BOM 采购台 v" + (summary.app_version || "?"),
      "时间: " + (summary.generated_at || ""),
      "系统: " + (summary.os || ""),
      "Python: " + (summary.python || ""),
      "依赖: playwright " + depText(deps.playwright) +
        " | openpyxl " + depText(deps.openpyxl) +
        " | ddddocr " + depText(deps.ddddocr) +
        " | opencv " + depText(deps.opencv),
      "浏览器: " + (found.length ? found[0] : "未找到") +
        " | CDP " + ((summary.browser || {}).cdp_port || "-") +
        ((summary.browser || {}).cdp_listening ? " 可连接" : " 未监听"),
      "会话: " + sessionLabel(s).text + (s.last_error ? " | " + s.last_error : ""),
      "日志: " + (summary.logs || []).length + " 个 | " +
        ((summary.runs || {}).log_size_mb || 0) + " MB | 页面错误 " +
        (Number(summary.client_errors) || 0) + " 条"
    ];
    return lines.join("\n");
  }

  async function copyEnv() {
    var text = envText();
    if (!text) {
      setHint("环境信息还没读出来，稍等一下再点。", "bad");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setHint("环境信息已复制，可以直接粘给维护者。", "ok");
      return;
    } catch (e) { /* 继续走降级方案 */ }
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      setHint("环境信息已复制，可以直接粘给维护者。", "ok");
    } catch (e) {
      setHint("复制失败，可以直接截图这一段，或导出日志包。", "bad");
    }
  }

  /* ---------- 绑定 ---------- */
  el.exportBtn.addEventListener("click", exportBundle);
  el.copyBtn.addEventListener("click", copyEnv);
  el.refreshBtn.addEventListener("click", function () {
    setHint("已刷新", "");
    loadSummary(true);
  });

  /* Ctrl/Cmd + Enter 直接导出，手不用离开键盘 */
  el.note.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      exportBundle();
    }
  });

  loadSummary(false);
})();
