(() => {
  "use strict";

  const LATEST_URL = new URLSearchParams(location.search).get("data") || "./data/latest.json";
  const KLINE_URL = "./data/klines.json";
  const ARCHIVE_INDEX_URL = "./data/archive/index.json";
  const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const KLINE_PALETTE = {
    up: "#ff4d5e",
    down: "#2fbf8f",
    ma20: "#e6b450",
    ma60: "#6fa8a2",
    grid: "rgba(230, 180, 80, 0.10)",
    quiet: "#66748c",
    muted: "#93a1b8",
  };

  const state = {
    payload: null,
    klines: null,
    archiveDay: "",
    board: "all",
    query: "",
    sortKey: "score",
    sortDir: -1,
    openSymbol: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtPct = (v, digits = 1) => {
    const n = num(v);
    return n === null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
  };
  const fmtNum = (v, digits = 3) => {
    const n = num(v);
    return n === null ? "—" : n.toFixed(digits);
  };
  const prettyDate = (v) => {
    const s = String(v ?? "");
    return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}.${s.slice(4, 6)}.${s.slice(6, 8)}` : s || "—";
  };

  async function fetchJson(url) {
    const joiner = url.includes("?") ? "&" : "?";
    const res = await fetch(`${url}${joiner}v=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  /* ---------------- 财富带 ---------------- */

  function countUp(el, target, digits, prefix = "", suffix = "") {
    if (!Number.isFinite(target)) {
      el.textContent = "—";
      return;
    }
    const text = (v) => `${prefix}${v.toFixed(digits)}${suffix}`;
    if (REDUCE) {
      el.textContent = text(target);
      return;
    }
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
    $("#trend-count").textContent = String(signals.length);
    $("#universe-count").textContent = Number(state.payload?.universe_count || 0).toLocaleString("zh-CN");

    const avg = (key) => {
      const vals = signals.map((s) => num(s[key])).filter((v) => v !== null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : NaN;
    };
    countUp($("#avg-gain"), avg("gain_60_pct"), 1, "+", "%");
    const gains = signals.map((s) => num(s.gain_60_pct)).filter((v) => v !== null);
    countUp($("#max-gain"), gains.length ? Math.max(...gains) : NaN, 1, "+", "%");
    countUp($("#avg-mdd"), avg("mdd_pct"), 1, "", "%");
  }

  /* ---------------- 迷你走势线 ---------------- */

  function sparklineSvg(symbol, er) {
    const bars = state.klines?.stocks?.[symbol]?.series?.day;
    if (!Array.isArray(bars) || bars.length < 2) return '<span class="spark-missing">—</span>';
    const closes = bars
      .slice(-60)
      .map((b) => Number(b[4]))
      .filter(Number.isFinite);
    if (closes.length < 2) return '<span class="spark-missing">—</span>';

    const w = 160;
    const h = 36;
    const pad = 2;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const span = max - min || 1;
    const stepX = (w - pad * 2) / (closes.length - 1);
    const pts = closes.map((c, i) => [
      pad + i * stepX,
      h - pad - ((c - min) / span) * (h - pad * 2),
    ]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${h} L${pts[0][0].toFixed(1)},${h} Z`;

    const bright = (num(er) ?? 0) >= 0.25;
    const stroke = bright ? "var(--gold)" : "#8a7a55";
    const gid = `g-${esc(symbol)}`;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true" focusable="false">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="rgba(230,180,80,${bright ? 0.28 : 0.14})"/>
        <stop offset="100%" stop-color="rgba(230,180,80,0)"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#${gid})"/>
      <path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.5"
            stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
  }

  /* ---------------- 榜单 ---------------- */

  function visibleSignals() {
    let rows = [...(state.payload?.signals || [])];
    if (state.board !== "all") rows = rows.filter((s) => s.board === state.board);
    const q = state.query.trim();
    if (q) rows = rows.filter((s) => `${s.symbol}${s.name}`.includes(q));
    rows.sort((a, b) => {
      const va = num(a[state.sortKey]);
      const vb = num(b[state.sortKey]);
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return (va - vb) * state.sortDir;
    });
    return rows;
  }

  function rowHtml(item, idx) {
    return `
      <td class="col-rank">${idx + 1}</td>
      <td class="col-name">
        ${idx === 0 ? '<span class="badge-top1">TOP 1</span>' : ""}
        <span class="name">${esc(item.name)}</span>
        <span class="code">${esc(item.symbol)}</span>
        <span class="board-tag">${esc(item.board)}</span>
      </td>
      <td class="col-spark c-spark">${sparklineSvg(item.symbol, item.er)}</td>
      <td class="col-num price">${fmtNum(item.close, 2)}</td>
      <td class="col-num c-gain up">${fmtPct(item.gain_60_pct)}</td>
      <td class="col-num down">${fmtPct(item.mdd_pct)}</td>
      <td class="col-num c-ext">${fmtPct(item.ext_pct)}</td>
      <td class="col-num c-limit">${num(item.limit_ups) === null ? "—" : esc(item.limit_ups)}</td>`;
  }

  function renderRows() {
    const body = $("#signals-body");
    // 任何重渲染路径都先收起已展开行，避免残留孤立面板
    document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
    state.openSymbol = null;

    const rows = visibleSignals();
    body.replaceChildren();
    const empty = $("#empty-state");
    empty.hidden = rows.length > 0;
    if (!rows.length) {
      empty.textContent = state.payload ? "没有符合当前筛选的候选。" : "正在加载…";
      return;
    }

    const frag = document.createDocumentFragment();
    rows.forEach((item, idx) => {
      const tr = document.createElement("tr");
      tr.className = `row-main${idx === 0 ? " is-top1" : ""}`;
      tr.dataset.symbol = String(item.symbol);
      tr.tabIndex = idx === 0 ? 0 : -1;
      tr.setAttribute("role", "button");
      tr.setAttribute("aria-expanded", "false");
      tr.innerHTML = rowHtml(item, idx);
      frag.append(tr);
    });
    body.append(frag);
    animateRows();
    syncSortHeaders();
  }

  function animateRows() {
    if (REDUCE) return;
    [...document.querySelectorAll("tr.row-main")].slice(0, 20).forEach((tr, i) => {
      tr.animate(
        [
          { opacity: 0, transform: "translateY(8px)" },
          { opacity: 1, transform: "none" },
        ],
        { duration: 320, delay: i * 20, easing: "ease-out", fill: "backwards" }
      );
    });
  }

  function syncSortHeaders() {
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      const active = th.dataset.sort === state.sortKey;
      th.classList.toggle("is-sort", active);
      if (active) th.dataset.dir = state.sortDir === -1 ? "desc" : "asc";
      else delete th.dataset.dir;
    });
  }

  function renderAll() {
    renderWealth();
    renderRows();
  }

  /* ---------------- K 线 ---------------- */

  const svgEl = (tag, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    return node;
  };

  const toBars = (arr) =>
    (arr || []).map((a) => ({
      ymd: a[0],
      open: Number(a[1]),
      high: Number(a[2]),
      low: Number(a[3]),
      close: Number(a[4]),
      volume: Math.max(0, Number(a[5]) || 0),
    }));

  function movingAverage(bars, days) {
    return bars.map((_, i) => {
      if (i < days - 1) return null;
      let sum = 0;
      for (let j = i - days + 1; j <= i; j += 1) sum += bars[j].close;
      return sum / days;
    });
  }

  function renderKlineInto(host, symbol, granularity = "day") {
    host.replaceChildren();
    const bars = toBars(state.klines?.stocks?.[symbol]?.series?.[granularity]);
    if (bars.length < 2) {
      const note = document.createElement("div");
      note.className = "chart-note";
      note.textContent = "暂无 K 线数据";
      host.append(note);
      return;
    }
    const width = Math.max(420, Math.floor(host.getBoundingClientRect().width || 640));
    const priceH = 300;
    const volH = 64;
    const gap = 14;
    const padTop = 16;
    const padRight = 64;
    const padBottom = 22;
    const totalH = padTop + priceH + gap + volH + padBottom;
    const chartW = width - padRight;

    const svg = svgEl("svg", {
      class: "kline-svg",
      viewBox: `0 0 ${width} ${totalH}`,
      preserveAspectRatio: "none",
    });

    const step = chartW / bars.length;
    const bodyW = Math.max(1, step * 0.62);
    let lo = Math.min(...bars.map((b) => b.low));
    let hi = Math.max(...bars.map((b) => b.high));
    const range = hi - lo || 1;
    lo -= range * 0.04;
    hi += range * 0.04;
    const y = (p) => padTop + ((hi - p) / (hi - lo)) * priceH;
    const x = (i) => i * step + step / 2;

    for (let g = 0; g <= 4; g += 1) {
      const gy = padTop + (priceH * g) / 4;
      svg.append(svgEl("line", { x1: 0, x2: chartW, y1: gy, y2: gy, stroke: KLINE_PALETTE.grid, "stroke-width": 1 }));
      const label = svgEl("text", { x: chartW + 6, y: gy + 4, "font-size": 10, fill: KLINE_PALETTE.quiet });
      label.textContent = (hi - (g / 4) * (hi - lo)).toFixed(2);
      svg.append(label);
    }

    const maxVol = Math.max(...bars.map((b) => b.volume), 1);
    bars.forEach((b, i) => {
      const vh = (b.volume / maxVol) * volH;
      const color = b.close >= b.open ? KLINE_PALETTE.up : KLINE_PALETTE.down;
      svg.append(svgEl("rect", {
        x: x(i) - bodyW / 2,
        y: padTop + priceH + gap + (volH - vh),
        width: bodyW,
        height: Math.max(vh, 0.5),
        fill: color,
        opacity: 0.5,
      }));
    });

    bars.forEach((b, i) => {
      const up = b.close >= b.open;
      const color = up ? KLINE_PALETTE.up : KLINE_PALETTE.down;
      svg.append(svgEl("line", { x1: x(i), x2: x(i), y1: y(b.high), y2: y(b.low), stroke: color, "stroke-width": 1 }));
      const top = y(Math.max(b.open, b.close));
      const h = Math.max(Math.abs(y(b.open) - y(b.close)), 1);
      svg.append(svgEl("rect", {
        x: x(i) - bodyW / 2,
        y: top,
        width: bodyW,
        height: h,
        stroke: color,
        "stroke-width": 1,
        fill: up ? "#05080f" : color,
      }));
    });

    const drawMA = (values, color, label) => {
      const pts = values
        .map((v, i) => (v === null ? null : `${x(i)},${y(v)}`))
        .filter(Boolean);
      if (pts.length < 2) return;
      svg.append(svgEl("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 1.4 }));
      const last = values[values.length - 1];
      if (last !== null) {
        const text = svgEl("text", { x: chartW + 6, y: y(last) + 3, "font-size": 10, fill: color });
        text.textContent = `${label} ${last.toFixed(2)}`;
        svg.append(text);
      }
    };
    const ma20 = movingAverage(bars, 20);
    const ma60 = movingAverage(bars, 60);
    drawMA(ma60, KLINE_PALETTE.ma60, "MA60");
    drawMA(ma20, KLINE_PALETTE.ma20, "MA20");

    const tooltip = document.createElement("div");
    tooltip.className = "ct-tooltip";
    tooltip.hidden = true;
    const cross = svgEl("line", {
      y1: padTop,
      y2: padTop + priceH + gap + volH,
      stroke: KLINE_PALETTE.muted,
      "stroke-width": 1,
      opacity: 0,
    });
    svg.append(cross);

    svg.addEventListener("mousemove", (event) => {
      const rect = svg.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * width;
      const py = ((event.clientY - rect.top) / rect.height) * totalH;
      if (px < 0 || px > chartW || py < padTop || py > padTop + priceH + gap + volH) {
        tooltip.hidden = true;
        cross.setAttribute("opacity", 0);
        return;
      }
      const i = Math.min(bars.length - 1, Math.max(0, Math.floor(px / step)));
      const b = bars[i];
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("opacity", 0.5);
      const rows = [
        ["日期", String(b.ymd)],
        ["开", b.open.toFixed(2)],
        ["高", b.high.toFixed(2)],
        ["低", b.low.toFixed(2)],
        ["收", b.close.toFixed(2)],
        ["MA20", ma20[i] === null ? "—" : ma20[i].toFixed(2)],
        ["MA60", ma60[i] === null ? "—" : ma60[i].toFixed(2)],
      ];
      tooltip.replaceChildren();
      rows.forEach(([k, v]) => {
        const row = document.createElement("div");
        row.className = "ct-row";
        const kk = document.createElement("span");
        kk.className = "ct-k";
        kk.textContent = k;
        const vv = document.createElement("span");
        vv.textContent = v;
        row.append(kk, vv);
        tooltip.append(row);
      });
      tooltip.hidden = false;
      const tw = tooltip.offsetWidth || 120;
      const left = Math.min(Math.max(4, px + 14), Math.max(4, host.clientWidth - tw - 8));
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${Math.max(4, py - 40)}px`;
    });
    svg.addEventListener("mouseleave", () => {
      tooltip.hidden = true;
      cross.setAttribute("opacity", 0);
    });

    host.append(svg, tooltip);
  }

  /* ---------------- 展开面板 ---------------- */

  function detailHtml(item) {
    const mf = num(item.net_mf_amount);
    return `
      <div class="detail-inner">
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
            <h4>买卖点</h4>
            <dl class="plan">
              <dt>买入参考价</dt><dd>${num(item.entry_price) === null ? "—" : esc(item.entry_price)}（${esc(item.entry_state || "—")}）</dd>
              <dt>止损价</dt><dd>${num(item.stop_loss) === null ? "—" : esc(item.stop_loss)}</dd>
              <dt>止盈价</dt><dd>${num(item.take_profit) === null ? "—" : esc(item.take_profit)}（1:${esc(item.risk_reward ?? "—")}）</dd>
              <dt>计划依据</dt><dd>${esc(item.plan_note || "—")}</dd>
            </dl>
            <h4>指标</h4>
            <dl>
              <dt>趋势效率</dt><dd>${fmtNum(item.er, 3)}</dd>
              <dt>60日涨幅</dt><dd class="up">${fmtPct(item.gain_60_pct)}</dd>
              <dt>最大回撤</dt><dd class="down">${fmtPct(item.mdd_pct)}</dd>
              <dt>距 MA20</dt><dd>${fmtPct(item.ext_pct)}</dd>
              <dt>MA20 / MA60</dt><dd>${esc(item.ma20)} / ${esc(item.ma60)}</dd>
              <dt>近20日涨停</dt><dd>${num(item.limit_ups) === null ? "—" : esc(item.limit_ups)} 次</dd>
              <dt>结构抬高</dt><dd>低点 ${item.higher_lows ? "是" : "否"} / 高点 ${item.higher_highs ? "是" : "否"}</dd>
            </dl>
            <h4>仓位与资金</h4>
            <dl>
              <dt>参考仓位</dt><dd>${num(item.position_pct) === null ? "—" : esc(item.position_pct)}% · ${esc(item.position_tier || "—")}</dd>
              <dt>仓位依据</dt><dd>${esc(item.position_reason || "—")}</dd>
              <dt>主力资金</dt><dd>${mf === null ? "—" : `${mf > 0 ? "+" : ""}${(mf / 10000).toFixed(2)} 亿`}</dd>
            </dl>
          </div>
        </div>
      </div>`;
  }

  function toggleRow(symbol) {
    const previous = document.querySelector("tr.row-detail");
    const previousMain = document.querySelector("tr.row-main.is-open");
    if (previous) previous.remove();
    if (previousMain) {
      previousMain.classList.remove("is-open");
      previousMain.setAttribute("aria-expanded", "false");
    }
    if (state.openSymbol === symbol) {
      state.openSymbol = null;
      return;
    }

    const main = document.querySelector(`tr.row-main[data-symbol="${symbol}"]`);
    const item = (state.payload?.signals || []).find((s) => String(s.symbol) === symbol);
    if (!main || !item) return;

    const tr = document.createElement("tr");
    tr.className = "row-detail";
    const td = document.createElement("td");
    td.colSpan = 8;
    tr.append(td);
    td.innerHTML = detailHtml(item);
    main.after(tr);
    main.classList.add("is-open");
    main.setAttribute("aria-expanded", "true");
    state.openSymbol = symbol;

    const host = tr.querySelector(".chart-host");
    renderKlineInto(host, symbol, "day");
    tr.querySelectorAll("[data-g]").forEach((btn) => {
      btn.addEventListener("click", () => {
        tr.querySelectorAll("[data-g]").forEach((b) => b.classList.toggle("is-active", b === btn));
        renderKlineInto(host, symbol, btn.dataset.g);
      });
    });
  }

  /* ---------------- 事件 ---------------- */

  function bindEvents() {
    $("#signals-body").addEventListener("click", (e) => {
      const tr = e.target.closest("tr.row-main");
      if (tr) toggleRow(tr.dataset.symbol);
    });

    document.addEventListener("keydown", (e) => {
      const rows = [...document.querySelectorAll("tr.row-main")];
      const i = rows.indexOf(document.activeElement);
      if (e.key === "ArrowDown" && i >= 0) {
        e.preventDefault();
        const next = rows[Math.min(i + 1, rows.length - 1)];
        if (next) {
          rows.forEach((r) => (r.tabIndex = -1));
          next.tabIndex = 0;
          next.focus();
        }
      } else if (e.key === "ArrowUp" && i > 0) {
        e.preventDefault();
        rows.forEach((r) => (r.tabIndex = -1));
        rows[i - 1].tabIndex = 0;
        rows[i - 1].focus();
      } else if ((e.key === "Enter" || e.key === " ") && i >= 0) {
        e.preventDefault();
        toggleRow(rows[i].dataset.symbol);
      } else if (e.key === "Escape") {
        document.querySelectorAll("tr.row-detail").forEach((n) => n.remove());
        document.querySelectorAll("tr.row-main.is-open").forEach((n) => {
          n.classList.remove("is-open");
          n.setAttribute("aria-expanded", "false");
        });
        state.openSymbol = null;
      }
    });

    $("#signals-head").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (!th) return;
      const key = th.dataset.sort;
      if (state.sortKey === key) state.sortDir *= -1;
      else {
        state.sortKey = key;
        state.sortDir = -1;
      }
      renderRows();
    });

    $("#search").addEventListener("input", (e) => {
      state.query = e.target.value;
      renderRows();
    });
    $("#board").addEventListener("change", (e) => {
      state.board = e.target.value;
      renderRows();
    });
    $("#refresh-button").addEventListener("click", () => {
      loadData(state.archiveDay ? `./data/archive/${state.archiveDay}.json` : LATEST_URL);
    });

    $("#archive-select").addEventListener("change", async (e) => {
      const day = e.target.value;
      if (!day) {
        state.archiveDay = "";
        $("#back-latest").hidden = true;
        await loadData(LATEST_URL);
        return;
      }
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
  }

  /* ---------------- 数据 ---------------- */

  async function loadArchiveIndex() {
    try {
      const idx = await fetchJson(ARCHIVE_INDEX_URL);
      const sel = $("#archive-select");
      const days = Array.isArray(idx.days) ? idx.days : [];
      sel.innerHTML =
        '<option value="">最新</option>' +
        days.map((d) => `<option value="${esc(d)}">${esc(prettyDate(d))}</option>`).join("");
    } catch {
      /* 索引缺失时保持只有「最新」 */
    }
  }

  async function loadData(url = LATEST_URL) {
    const btn = $("#refresh-button");
    btn.disabled = true;
    btn.textContent = "加载中…";
    try {
      state.payload = await fetchJson(url);
      if (!Array.isArray(state.payload?.signals)) {
        throw new Error("数据格式不符合约定（缺少 signals 数组）");
      }
      if (!state.klines) {
        state.klines = await fetchJson(KLINE_URL).catch(() => ({ stocks: {} }));
      }
      const warn = $("#data-warning");
      warn.hidden = true;
      warn.textContent = "";
      renderAll();
    } catch (err) {
      const warn = $("#data-warning");
      warn.hidden = false;
      warn.textContent = `数据加载失败：${err.message}`;
      if (!state.payload) {
        state.payload = { signals: [] };
        renderAll();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "刷新";
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    loadArchiveIndex();
    loadData();
  });
})();
