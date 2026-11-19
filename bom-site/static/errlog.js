/* 浏览器端错误收集：页面脚本崩了只在用户屏幕上闪一下，服务端一无所知。
   把错误落一行到 runs/client.log，用户下次说「点了没反应」时就能对上。
   约束：
     - 每页最多 20 条，同一条只报一次，避免一个循环把日志刷爆。
     - 任何失败都吞掉：上报本身不能制造新的错误。 */
(function () {
  "use strict";

  var sent = 0;
  var seen = {};

  function report(kind, message, extra) {
    if (sent >= 20) return;
    var msg = String(message == null ? "" : message).slice(0, 800);
    var key = kind + "|" + msg;
    if (seen[key]) return;
    seen[key] = 1;
    sent += 1;
    try {
      fetch("/api/diag/client", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: kind,
          message: msg,
          page: location.pathname + location.search,
          extra: extra == null ? "" : String(extra),
          ua: navigator.userAgent
        }),
        keepalive: true
      }).catch(function () {});
    } catch (e) {
      /* 上报失败就算了，绝不再抛 */
    }
  }

  window.addEventListener("error", function (e) {
    var where = "";
    if (e && e.filename) where = e.filename + ":" + (e.lineno || 0) + ":" + (e.colno || 0);
    report("error", (e && e.message) || "脚本错误", where);
  });

  window.addEventListener("unhandledrejection", function (e) {
    var r = e && e.reason;
    var msg = (r && (r.stack || r.message)) || String(r);
    report("unhandledrejection", msg, "");
  });

  /* 给页面留一个手动上报口（比如「导出失败」这种被 try 住的异常） */
  window.__reportProblem = report;
})();
