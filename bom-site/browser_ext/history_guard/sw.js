/* BOM 采购台 · 淘宝历史清理
 *
 * 为什么要在浏览器里做：Chromium 打开 History 库时用独占锁，外部进程连只读
 * 都打不开（实测 "database is locked"），所以「跑外面定时删」在窗口开着时
 * 一定失败。唯一能一边跑一边删的，是浏览器自己的 chrome.history 接口 ——
 * 也就是这个扩展。
 *
 * 边界：
 *   - 只删 URL 命中 taobao.com / tmall.com / taobao.net，或标题带「淘宝 / 天猫」的；
 *   - 同一个 profile 里顺手打开的别的网页（DeepSeek、本机 8765）一条不动；
 *   - 删除失败一律吞掉：这是隐私增强，不能变成浏览故障。
 *
 * 触发时机：
 *   - 每次访问淘宝系页面后 1.2 秒（onVisited，最快，历史里几乎不留痕）；
 *   - 标签页加载完成（onUpdated，兜住「前进/后退/刷新」不触发 onVisited 的情况）；
 *   - 浏览器启动、扩展安装时各扫一次（收拾上次没删干净的）；
 *   - 每 1 分钟兜底扫一次（service worker 被回收后也能自己醒过来）。
 */

const DOMAIN_MATCH = [
  /^https?:\/\/([^/?#]*\.)?taobao\.com([/?#:]|$)/i,
  /^https?:\/\/([^/?#]*\.)?tmall\.com([/?#:]|$)/i,
  /^https?:\/\/([^/?#]*\.)?taobao\.net([/?#:]|$)/i,
];

const TITLE_MATCH = /淘宝|天猫/;

/* 搜索用的关键词：中英文各来一份，避免标题索引和 URL 索引的差异漏条目 */
const SEARCH_TERMS = ["taobao.com", "tmall.com", "淘宝", "天猫"];

let timer = null;

function isTaobao(item) {
  const url = (item && item.url) || "";
  if (DOMAIN_MATCH.some((re) => re.test(url))) return true;
  return TITLE_MATCH.test((item && item.title) || "");
}

async function sweep() {
  const seen = new Set();
  let removed = 0;
  for (const term of SEARCH_TERMS) {
    let items = [];
    try {
      items = await chrome.history.search({ text: term, maxResults: 5000, startTime: 0 });
    } catch (err) {
      continue;
    }
    for (const item of items) {
      if (!item || !item.url || seen.has(item.url) || !isTaobao(item)) continue;
      seen.add(item.url);
      try {
        await chrome.history.deleteUrl({ url: item.url });
        removed += 1;
      } catch (err) {
        /* 删不掉就算了，下一轮再来 */
      }
    }
  }
  return removed;
}

function schedule(delay) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    sweep();
  }, typeof delay === "number" ? delay : 1200);
}

chrome.runtime.onStartup.addListener(() => schedule(300));
chrome.runtime.onInstalled.addListener(() => schedule(300));

try {
  chrome.history.onVisited.addListener((item) => {
    if (isTaobao(item)) schedule(900);
  });
} catch (err) {
  /* 老版本没有这个事件也不影响下面的兜底 */
}

try {
  chrome.tabs.onUpdated.addListener((tabId, info) => {
    if (info && info.status === "complete") schedule(1200);
  });
} catch (err) {
  /* ignore */
}

try {
  chrome.alarms.create("bom-history-sweep", { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (!alarm || alarm.name === "bom-history-sweep") sweep();
  });
} catch (err) {
  /* ignore */
}

/* session 启动时先扫一遍：把上次异常退出留下的记录收掉 */
schedule(300);
