# 极简趋势简报 + 财富感首屏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有前端重做为「顶栏 + 财富带 + 单列榜单 + 行内展开」的极简简报，并加入浓郁档金色财富视觉。

**Architecture:** 纯静态 HTML/CSS/JS，无构建步骤。`index.html` 只保留顶栏、财富带、榜单容器与页脚；`app.js` 负责取数、聚合、渲染、排序、行内展开与 K 线复用；`styles.css` 承载完整视觉系统。数据契约不变，走势线直接读 `klines.json` 已有日线。

**Tech Stack:** 原生 HTML5 / CSS3 / ES2020 JavaScript；数据为 `docs/data/latest.json`、`klines.json`、`archive/`。

**Spec:** `docs/superpowers/specs/2026-11-07-frontend-minimal-wealth-design.md`

## Global Constraints

- 色板必须逐字使用：`--bg:#05080f`、`--bg-2:#080d18`、`--surface:#0b1220`、`--line:rgba(230,180,80,0.14)`、`--ink:#f2f5fb`、`--muted:#93a1b8`、`--quiet:#66748c`、`--gold:#e6b450`、`--gold-bright:#ffd479`、`--gold-soft:rgba(230,180,80,0.12)`、`--up:#ff4d5e`、`--down:#2fbf8f`、`--score:#ffd479`。
- 金光透明度上限 `0.22`；金色只用于财富带、走势线、TOP1、高分，正文保持中性色。
- 数字一律 `font-variant-numeric: tabular-nums`。
- 走势线取 `klines.json` 的 `stocks[symbol].series.day`，每根为 `[ymd, open, high, low, close, volume]`（收盘价索引 **4**），取末 60 根。
- 不改动 `docs/data/*`、`scripts/*`、`src/*`；不新增后端字段。
- 必须支持 `prefers-reduced-motion: reduce`（关闭计数、错峰、描边、高度动画）。
- 页脚必须保留「研究预警，不构成投资建议」。
- 仓库文件为 CRLF；新建/重写文件后必须规范化回 CRLF。
- 验收用 Playwright 真浏览器（本会话 MCP 可用）；无前端测试框架。

## Review Focus

- **某只股票在 `klines.json` 中缺失或日线不足 2 根**：走势线必须留空而不是抛错、不能让整张表停止渲染。
- **`signals` 为空数组**：财富带不能显示 `NaN`，必须显示 `—` 并给出空状态文案。
- **归档日数据字段与最新日不一致**（例如旧归档仍含旧信号名）：表格只按字段读取，未知信号名照常显示，不依赖 `signal` 等于某个值。
- **窄屏与 reduced-motion 同时生效**：不能出现横向滚动条或停留在动画中间态。
- **展开行时切换档案/排序**：必须先收起已展开行，避免 `tbody` 重建后残留孤立面板。

---

### Task 1: 页面骨架与视觉系统

**Files:**
- Rewrite: `docs/index.html`
- Rewrite: `docs/styles.css`

**Interfaces:**
- Produces: DOM id 契约 —— `#as-of`、`#trend-count`、`#avg-gain`、`#avg-er`、`#top-score`、`#universe-count`、`#board`、`#search`、`#archive-select`、`#refresh-button`、`#back-latest`、`#signals-body`、`#signals-head`、`#data-warning`、`#empty-state`、`#footer-note`。
- Produces: CSS 变量与类 —— `.wealth`、`.wealth-metrics`、`.signals-table`、`.row-main`、`.row-detail`、`.spark`、`.badge-top1`、`.is-open`、`.is-top1`。

- [ ] **Step 1: 写 `index.html` 骨架**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>势能雷达 · 清晰上升趋势</title>
  <link rel="stylesheet" href="./styles.css?v=30" />
  <script defer src="./app.js?v=30"></script>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="./">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-name">势能雷达</span>
      <span class="brand-sub">CLEAR UPTREND</span>
    </a>
    <div class="topbar-controls">
      <label class="sr-only" for="board">板块</label>
      <select id="board" class="control">
        <option value="all">全部板块</option>
        <option value="主板">主板</option>
        <option value="创业板">创业板</option>
        <option value="科创板">科创板</option>
        <option value="北交所">北交所</option>
      </select>
      <label class="sr-only" for="search">代码或名称</label>
      <input id="search" class="control" type="search" placeholder="代码 / 名称" autocomplete="off" />
      <label class="sr-only" for="archive-select">历史档案</label>
      <select id="archive-select" class="control"><option value="">最新</option></select>
      <button id="back-latest" class="control control-btn" type="button" hidden>返回最新</button>
      <button id="refresh-button" class="control control-btn" type="button">刷新</button>
    </div>
  </header>

  <section class="wealth" aria-label="今日概览">
    <div class="wealth-left">
      <div class="wealth-date" id="as-of">—</div>
      <div class="wealth-sub">清晰上升趋势 · <span id="trend-count">—</span> 只 · 覆盖 <span id="universe-count">—</span> 只</div>
    </div>
    <div class="wealth-metrics">
      <div class="wm"><strong id="avg-gain">—</strong><span>平均 60 日涨幅</span></div>
      <div class="wm"><strong id="avg-er">—</strong><span>平均趋势效率</span></div>
      <div class="wm"><strong id="top-score">—</strong><span>最高分</span></div>
    </div>
  </section>

  <div id="data-warning" class="warning" hidden></div>

  <main class="board">
    <table class="signals-table">
      <thead id="signals-head">
        <tr>
          <th class="col-rank">#</th>
          <th class="col-name">标的</th>
          <th class="col-spark">60日走势</th>
          <th class="col-num" data-sort="er">ER</th>
          <th class="col-num" data-sort="gain_60_pct">60日涨幅</th>
          <th class="col-num" data-sort="mdd_pct">最大回撤</th>
          <th class="col-num" data-sort="ext_pct">距MA20</th>
          <th class="col-num">涨停</th>
          <th class="col-num" data-sort="score">分数</th>
        </tr>
      </thead>
      <tbody id="signals-body"></tbody>
    </table>
    <div id="empty-state" class="empty" hidden></div>
  </main>

  <footer class="footer">
    <p id="footer-note">研究预警，不构成投资建议。信号为规则化统计筛选，不代表未来收益。</p>
  </footer>
</body>
</html>
```

- [ ] **Step 2: 写 `styles.css` 视觉系统**

包含：`:root` 色板（逐字使用 Global Constraints）、`body` 深底、`.topbar` sticky 56px、`.wealth` 170px + 金色径向光晕 + 底部金色渐变发丝线、`.wealth-metrics strong` 40px/700 金色、`.signals-table` 列宽与 sticky `thead`、行高 56px、`.is-top1` 左侧 2px 金条、`.badge-top1`、`.spark` 160×36、`.row-detail` 展开面板、`.warning`、`.empty`、`.footer`、响应式断点（1100/900/700）与 `prefers-reduced-motion`。

关键片段：

```css
:root{
  --bg:#05080f; --bg-2:#080d18; --surface:#0b1220;
  --line:rgba(230,180,80,0.14); --ink:#f2f5fb; --muted:#93a1b8; --quiet:#66748c;
  --gold:#e6b450; --gold-bright:#ffd479; --gold-soft:rgba(230,180,80,0.12);
  --up:#ff4d5e; --down:#2fbf8f; --score:#ffd479;
}
body{background:var(--bg);color:var(--ink);font-variant-numeric:tabular-nums;}
.wealth{
  position:relative; background:var(--bg-2); padding:28px 32px;
  background-image:radial-gradient(120% 140% at 88% -20%,
    rgba(230,180,80,0.22) 0%, rgba(230,180,80,0.06) 38%, transparent 62%);
}
.wealth::after{
  content:""; position:absolute; left:0; right:0; bottom:0; height:1px;
  background:linear-gradient(90deg,transparent,var(--gold),transparent); opacity:.55;
}
.wealth-metrics strong{font-size:40px;font-weight:700;color:var(--gold-bright);}
.wealth-metrics .wm:first-child strong{color:var(--up);}
.signals-table tbody tr.row-main{height:56px;border-bottom:1px solid var(--line);cursor:pointer;}
.signals-table tbody tr.row-main:hover{background:rgba(230,180,80,0.048);}
.signals-table tbody tr.is-top1{border-left:2px solid var(--gold);}
.row-detail>td{background:var(--surface);border-top:1px solid var(--gold);padding:20px;}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important;}}
```

- [ ] **Step 3: 语法与静态检查**

```bash
node --check docs/app.js 2>/dev/null || echo "app.js not yet rewritten (expected)"
.venv/Scripts/python.exe -c "
import re
html=open('docs/index.html',encoding='utf-8').read()
ids=set(re.findall(r'id=\"([^\"]+)\"',html))
need={'as-of','trend-count','avg-gain','avg-er','top-score','universe-count','board','search','archive-select','refresh-button','back-latest','signals-body','signals-head','data-warning','empty-state','footer-note'}
print('missing ids:', sorted(need-ids) or 'none')
"
```

Expected: `missing ids: none`

- [ ] **Step 4: 提交**

```bash
git add docs/index.html docs/styles.css
git commit -m "feat(frontend): 极简趋势简报骨架与金色视觉系统"
```

---

### Task 2: 取数、财富带聚合与榜单渲染

**Files:**
- Rewrite: `docs/app.js`

**Interfaces:**
- Consumes: Task 1 的 DOM id。
- Produces: `state = {payload, klines, archiveDay, board, query, sortKey, sortDir, openSymbol}`；`loadData()`；`renderAll()`；`renderWealth()`；`renderRows()`；`sparklineSvg(symbol, er)`；`countUp(el, value, digits, prefix)`。

- [ ] **Step 1: 写数据层与工具函数**

```javascript
const LATEST_URL = new URLSearchParams(location.search).get("data") || "./data/latest.json";
const KLINE_URL = "./data/klines.json";
const ARCHIVE_INDEX_URL = "./data/archive/index.json";
const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  payload: null, klines: null, archiveDay: "",
  board: "all", query: "", sortKey: "score", sortDir: -1, openSymbol: null,
};

const $ = (sel) => document.querySelector(sel);
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const fmtPct = (v, digits = 1) => {
  const n = num(v);
  return n === null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
};
const fmtNum = (v, digits = 3) => { const n = num(v); return n === null ? "—" : n.toFixed(digits); };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
const prettyDate = (v) => {
  const s = String(v ?? "");
  return /^\d{8}$/.test(s) ? `${s.slice(0,4)}.${s.slice(4,6)}.${s.slice(6,8)}` : (s || "—");
};

async function fetchJson(url) {
  const res = await fetch(`${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
```

- [ ] **Step 2: 写财富带聚合与数字滚动**

```javascript
function countUp(el, target, digits, prefix = "") {
  const text = (v) => `${prefix}${v.toFixed(digits)}`;
  if (REDUCE || !Number.isFinite(target)) { el.textContent = Number.isFinite(target) ? text(target) : "—"; return; }
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 900);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = text(target * eased);
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderWealth() {
  const signals = state.payload?.signals || [];
  $("#as-of").textContent = prettyDate(state.payload?.as_of);
  $("#trend-count").textContent = signals.length || "0";
  $("#universe-count").textContent = Number(state.payload?.universe_count || 0).toLocaleString();
  const avg = (key) => {
    const vals = signals.map((s) => num(s[key])).filter((v) => v !== null);
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : NaN;
  };
  countUp($("#avg-gain"), avg("gain_60_pct"), 1, "+");
  countUp($("#avg-er"), avg("er"), 3);
  const top = signals.reduce((m, s) => Math.max(m, num(s.score) ?? 0), 0);
  countUp($("#top-score"), top, 1);
}
```

- [ ] **Step 3: 写迷你走势线**

```javascript
function sparklineSvg(symbol, er) {
  const bars = state.klines?.stocks?.[symbol]?.series?.day;
  if (!Array.isArray(bars) || bars.length < 2) return "";
  const closes = bars.slice(-60).map((b) => Number(b[4])).filter(Number.isFinite);
  if (closes.length < 2) return "";
  const w = 160, h = 36, pad = 2;
  const min = Math.min(...closes), max = Math.max(...closes);
  const span = max - min || 1;
  const stepX = (w - pad * 2) / (closes.length - 1);
  const pts = closes.map((c, i) => [
    pad + i * stepX,
    h - pad - ((c - min) / span) * (h - pad * 2),
  ]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${pts.at(-1)[0].toFixed(1)},${h} L${pts[0][0].toFixed(1)},${h} Z`;
  const bright = (num(er) ?? 0) >= 0.25;
  const stroke = bright ? "var(--gold)" : "#8a7a55";
  const gid = `g-${symbol}`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(230,180,80,${bright ? 0.28 : 0.14})"/>
      <stop offset="100%" stop-color="rgba(230,180,80,0)"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.5"
          stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}
```

- [ ] **Step 4: 写榜单渲染**

```javascript
function visibleSignals() {
  let rows = [...(state.payload?.signals || [])];
  if (state.board !== "all") rows = rows.filter((s) => s.board === state.board);
  const q = state.query.trim();
  if (q) rows = rows.filter((s) => `${s.symbol}${s.name}`.includes(q));
  rows.sort((a, b) => {
    const va = num(a[state.sortKey]) ?? -Infinity;
    const vb = num(b[state.sortKey]) ?? -Infinity;
    return (va - vb) * state.sortDir;
  });
  return rows;
}

function renderRows() {
  const body = $("#signals-body");
  // 先无条件收起已展开行：任何重渲染路径都不能留下孤立面板
  document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
  state.openSymbol = null;
  const rows = visibleSignals();
  body.replaceChildren();
  $("#empty-state").hidden = rows.length > 0;
  if (!rows.length) { $("#empty-state").textContent = "没有符合当前筛选的候选。"; return; }
  rows.forEach((item, idx) => {
    const tr = document.createElement("tr");
    tr.className = "row-main" + (idx === 0 ? " is-top1" : "");
    tr.dataset.symbol = item.symbol;
    tr.tabIndex = idx === 0 ? 0 : -1;
    tr.innerHTML = `
      <td class="col-rank">${idx + 1}</td>
      <td class="col-name">
        ${idx === 0 ? '<span class="badge-top1">TOP 1</span>' : ""}
        <span class="name">${esc(item.name)}</span>
        <span class="code">${esc(item.symbol)}</span>
        <span class="board-tag">${esc(item.board)}</span>
      </td>
      <td class="col-spark">${sparklineSvg(item.symbol, item.er)}</td>
      <td class="col-num">${fmtNum(item.er, 3)}</td>
      <td class="col-num up">${fmtPct(item.gain_60_pct)}</td>
      <td class="col-num down">${fmtPct(item.mdd_pct)}</td>
      <td class="col-num">${fmtPct(item.ext_pct)}</td>
      <td class="col-num">${num(item.limit_ups) ?? "—"}</td>
      <td class="col-num score${(num(item.score) ?? 0) >= 80 ? " high" : ""}">${fmtNum(item.score, 1)}</td>`;
    body.append(tr);
  });
}

function renderAll() {
  renderWealth();
  renderRows();
}
```

- [ ] **Step 5: 写启动加载**

```javascript
async function loadData(url = LATEST_URL) {
  $("#refresh-button").disabled = true;
  try {
    state.payload = await fetchJson(url);
    if (!state.klines) {
      state.klines = await fetchJson(KLINE_URL).catch(() => ({ stocks: {} }));
    }
    renderAll();
    $("#data-warning").hidden = true;
  } catch (err) {
    const box = $("#data-warning");
    box.hidden = false;
    box.textContent = `数据加载失败：${err.message}`;
  } finally {
    $("#refresh-button").disabled = false;
  }
}
document.addEventListener("DOMContentLoaded", () => loadData());
```

- [ ] **Step 6: 浏览器验证**

Run（Playwright）：打开 `http://127.0.0.1:8791/`（本地 `docs/` 静态服务）。
Expected：37 行；`#avg-gain`/`#avg-er`/`#top-score` 在 1.5 秒内变为非零；每行 `.spark path` 的 `d` 非空；无 console error。

- [ ] **Step 7: 提交**

```bash
git add docs/app.js
git commit -m "feat(frontend): 财富带聚合、金色走势线与榜单渲染"
```

---

### Task 3: 行内展开、K 线与买卖点

**Files:**
- Modify: `docs/app.js`

**Interfaces:**
- Consumes: `state.openSymbol`、`state.klines`。
- Produces: `toggleRow(symbol)`、`buildDetail(item)`、`renderKlineInto(container, symbol)`。

- [ ] **Step 1: 迁移 K 线渲染**

把现有 `svgEl`、`movingAverage`、`drawKline`、`klineBarsFor`、`setGranularity` 逻辑复制进新 `app.js`，并改造：

- 删除对 `#signal-dialog` 的依赖；
- `renderKlineInto(container, symbol, granularity = "day")` 接收容器 DOM，内部 `container.replaceChildren()` 后重建；
- 数据源改为 `state.klines?.stocks?.[symbol]?.series?.[granularity]`（该结构已是现成的日/周/月数组，无需再聚合）；
- 每根 bar 为 `[ymd, o, h, l, c, v]`。

```javascript
function renderKlineInto(container, symbol, granularity = "day") {
  container.replaceChildren();
  const bars = state.klines?.stocks?.[symbol]?.series?.[granularity];
  if (!Array.isArray(bars) || bars.length < 2) {
    container.textContent = "暂无 K 线数据";
    return;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "kline-svg");
  container.append(svg);
  drawKline(svg, bars);   // 复用旧 drawKline，签名调整为 (svg, bars)
}
```

- [ ] **Step 2: 写展开面板**

```javascript
function buildDetail(item) {
  const tr = document.createElement("tr");
  tr.className = "row-detail";
  tr.innerHTML = `<td colspan="9">
    <div class="detail-grid">
      <div class="detail-chart">
        <div class="granularity">
          <button type="button" data-g="day" class="is-active">日</button>
          <button type="button" data-g="week">周</button>
          <button type="button" data-g="month">月</button>
        </div>
        <div class="chart-host"></div>
      </div>
      <div class="detail-meta">
        <dl class="plan">
          <dt>买入参考价</dt><dd>${num(item.entry_price) === null ? "—" : item.entry_price}（${esc(item.entry_state || "—")}）</dd>
          <dt>止损价</dt><dd class="down">${num(item.stop_loss) === null ? "—" : item.stop_loss}</dd>
          <dt>止盈价</dt><dd class="up">${num(item.take_profit) === null ? "—" : item.take_profit}（1:${esc(item.risk_reward ?? "—")}）</dd>
          <dt>计划依据</dt><dd>${esc(item.plan_note || "—")}</dd>
        </dl>
        <dl class="metrics">
          <dt>趋势效率</dt><dd>${fmtNum(item.er, 3)}</dd>
          <dt>60日涨幅</dt><dd class="up">${fmtPct(item.gain_60_pct)}</dd>
          <dt>最大回撤</dt><dd class="down">${fmtPct(item.mdd_pct)}</dd>
          <dt>距 MA20</dt><dd>${fmtPct(item.ext_pct)}</dd>
          <dt>MA20 / MA60</dt><dd>${esc(item.ma20)} / ${esc(item.ma60)}</dd>
          <dt>近20日涨停</dt><dd>${num(item.limit_ups) ?? "—"} 次</dd>
          <dt>结构抬高</dt><dd>低点 ${item.higher_lows ? "是" : "否"} / 高点 ${item.higher_highs ? "是" : "否"}</dd>
        </dl>
        <dl class="position">
          <dt>参考仓位</dt><dd>${num(item.position_pct) ?? "—"}% · ${esc(item.position_tier || "—")}</dd>
          <dt>仓位依据</dt><dd>${esc(item.position_reason || "—")}</dd>
          <dt>主力资金</dt><dd>${num(item.net_mf_amount) === null ? "—" : `${item.net_mf_amount > 0 ? "+" : ""}${(item.net_mf_amount / 10000).toFixed(2)} 亿`}</dd>
        </dl>
      </div>
    </div>
  </td>`;
  return tr;
}

function toggleRow(symbol) {
  document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
  if (state.openSymbol === symbol) { state.openSymbol = null; return; }
  state.openSymbol = symbol;
  const main = document.querySelector(`tr.row-main[data-symbol="${symbol}"]`);
  if (!main) return;
  const detail = buildDetail((state.payload.signals || []).find((s) => s.symbol === symbol));
  main.after(detail);
  const host = detail.querySelector(".chart-host");
  renderKlineInto(host, symbol, "day");
  detail.querySelectorAll("[data-g]").forEach((btn) => {
    btn.addEventListener("click", () => {
      detail.querySelectorAll("[data-g]").forEach((b) => b.classList.toggle("is-active", b === btn));
      renderKlineInto(host, symbol, btn.dataset.g);
    });
  });
}
```

- [ ] **Step 3: 绑定点击与键盘**

```javascript
$("#signals-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr.row-main");
  if (tr) toggleRow(tr.dataset.symbol);
});
document.addEventListener("keydown", (e) => {
  const rows = [...document.querySelectorAll("tr.row-main")];
  const i = rows.indexOf(document.activeElement);
  if (e.key === "ArrowDown" && i >= 0) { e.preventDefault(); rows[Math.min(i + 1, rows.length - 1)]?.focus(); }
  if (e.key === "ArrowUp" && i > 0) { e.preventDefault(); rows[i - 1]?.focus(); }
  if (e.key === "Enter" && i >= 0) { e.preventDefault(); toggleRow(rows[i].dataset.symbol); }
  if (e.key === "Escape") { document.querySelectorAll("tr.row-detail").forEach((n) => n.remove()); state.openSymbol = null; }
});
```

- [ ] **Step 4: 浏览器验证**

Expected：点击行出现面板，含 K 线与买卖点四行；再次点击收起；`Esc` 收起；切换日/周/月重绘；console 无 error。

- [ ] **Step 5: 提交**

```bash
git add docs/app.js
git commit -m "feat(frontend): 行内展开与 K 线复用"
```

---

### Task 4: 排序、筛选、档案与刷新

**Files:**
- Modify: `docs/app.js`

- [ ] **Step 1: 排序**

```javascript
$("#signals-head").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = -1; }
  document.querySelectorAll("th[data-sort]").forEach((n) => n.classList.toggle("is-sort", n === th));
  state.openSymbol = null;
  document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
  renderRows();
});
```

- [ ] **Step 2: 搜索与板块**

```javascript
$("#search").addEventListener("input", (e) => {
  state.query = e.target.value;
  state.openSymbol = null;
  document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
  renderRows();
});
$("#board").addEventListener("change", (e) => {
  state.board = e.target.value;
  state.openSymbol = null;
  document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
  renderRows();
});
```

- [ ] **Step 3: 档案**

```javascript
async function loadArchiveIndex() {
  try {
    const idx = await fetchJson(ARCHIVE_INDEX_URL);
    const sel = $("#archive-select");
    sel.innerHTML = '<option value="">最新</option>' +
      (idx.days || []).map((d) => `<option value="${d}">${prettyDate(d)}</option>`).join("");
  } catch { /* 索引缺失时保持只有「最新」 */ }
}

$("#archive-select").addEventListener("change", async (e) => {
  const day = e.target.value;
  if (!day) { state.archiveDay = ""; $("#back-latest").hidden = true; await loadData(LATEST_URL); return; }
  state.archiveDay = day;
  $("#back-latest").hidden = false;
  await loadData(`./data/archive/${day}.json`);
});
$("#back-latest").addEventListener("click", async () => {
  state.archiveDay = "";
  $("#archive-select").value = "";
  $("#back-latest").hidden = true;
  await loadData(LATEST_URL);
});
$("#refresh-button").addEventListener("click", () => loadData(state.archiveDay ? `./data/archive/${state.archiveDay}.json` : LATEST_URL));
```

- [ ] **Step 4: 启动时加载索引**

在 `DOMContentLoaded` 中追加 `loadArchiveIndex();`。

- [ ] **Step 5: 浏览器验证**

Expected：点 ER 表头首行变为 ER 最大；搜索 `600` 只剩含 600 的代码；板块筛选生效；档案切到 20261105 后 `#as-of` 变化且「返回最新」出现；无 error。

- [ ] **Step 6: 提交**

```bash
git add docs/app.js
git commit -m "feat(frontend): 排序、筛选与历史档案"
```

---

### Task 5: 动效、响应式、清理与发布

**Files:**
- Modify: `docs/styles.css`
- Modify: `docs/app.js`
- Modify: `docs/sw.js`

- [ ] **Step 1: 行入场与 reduced-motion**

```javascript
function animateRows() {
  if (REDUCE) return;
  [...document.querySelectorAll("tr.row-main")].slice(0, 20).forEach((tr, i) => {
    tr.animate(
      [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }],
      { duration: 320, delay: i * 20, easing: "ease-out", fill: "backwards" }
    );
  });
}
```

在 `renderRows()` 末尾调用 `animateRows()`。

- [ ] **Step 2: 响应式规则**

```css
@media (max-width:1100px){ .signals-table .col-num:nth-child(7), .signals-table .col-num:nth-child(8){display:none;} }
@media (max-width:900px){ .col-spark{display:none;} .signals-table .col-num:nth-child(5){display:none;} }
@media (max-width:700px){ .wealth{flex-direction:column;gap:16px;} .topbar{flex-wrap:wrap;height:auto;padding:10px 16px;} }
```

> 用 `nth-child` 前先确认 `<thead>`/`<tbody>` 的列序与 Task 1 一致；若改用类名控制更清晰，则统一给 `th/td` 加 `.c-spark/.c-ext/.c-lim`。

- [ ] **Step 3: Service Worker**

把 `docs/sw.js` 的 `CACHE` 版本 +1，并在 `DATA` 列表中只保留 `./data/latest.json`、`./data/klines.json`、`./data/archive/index.json`。

- [ ] **Step 4: 删除废弃样式**

清掉 `styles.css` 中不再使用的 `.kpi-*`、`.filter-*`、`.swatch-*`、`.mainline-*`、`.signal-dialog*`、`.news-badge` 规则；用 grep 确认无引用。

```bash
grep -rn "kpi-\|filter-rail\|swatch-\|mainline\|signal-dialog" docs/index.html docs/app.js || echo "no refs"
```

- [ ] **Step 5: 全量浏览器验收**

Playwright：
1. 首屏 37 行、财富带数字非零、走势线 `d` 非空
2. 点击/Enter 展开、Esc 收起、↑↓ 移动焦点
3. 排序、搜索、板块、档案、返回最新
4. 控制台 0 error
5. 模拟 `prefers-reduced-motion: reduce` 后数字直接显示、无动画

- [ ] **Step 6: 提交与推送**

```bash
git add docs/index.html docs/styles.css docs/app.js docs/sw.js
git commit -m "feat(frontend): 动效、响应式与清理"
git fetch origin && git log --oneline HEAD..origin/main
git push origin main
```

> 若 `HEAD..origin/main` 非空，先 `git rebase origin/main`；不要 rebase `docs/data` 冲突提交。

---

## 验收标准

1. 首屏无需滚动即可看到财富带与至少 8 行榜单
2. 默认按分数降序，TOP1 有金色竖条与徽章
3. 每条走势线肉眼可辨方向与平滑度；ER 低的线为暗金
4. 展开面板包含 K 线（日/周/月）、买卖点、指标明细、仓位、资金流
5. 排序、搜索、板块、档案全部可用；`Esc`/`↑`/`↓` 正常
6. `prefers-reduced-motion` 下无动画残留
7. 控制台无 error；页脚保留研究预警声明
