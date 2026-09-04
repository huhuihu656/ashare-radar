(() => {
  "use strict";

  const DATA_URL = "./data/latest.json";
  const state = { payload: null, signal: "all", query: "", board: "all" };
  const $ = (selector) => document.querySelector(selector);
  const number = new Intl.NumberFormat("zh-CN");
  const decimal = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

  const cleanText = (value, fallback = "—") => value === undefined || value === null || value === "" ? fallback : String(value);
  const signalKind = (signal) => signal === "回踩前期起涨位" ? "support" : signal === "横盘后放量突破" ? "breakout" : "other";
  const prettyDate = (value) => {
    if (!value) return "—";
    const compact = String(value).match(/^(\d{4})(\d{2})(\d{2})$/);
    if (compact) return `${compact[1]}.${compact[2]}.${compact[3]}`;
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
  };
  const add = (parent, tag, text, className) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  };

  function filteredSignals() {
    const signals = Array.isArray(state.payload?.signals) ? state.payload.signals : [];
    const query = state.query.trim().toLowerCase();
    return signals.filter((item) => {
      const kindMatch = state.signal === "all" || signalKind(item.signal) === state.signal;
      const boardMatch = state.board === "all" || item.board === state.board;
      const words = `${item.symbol || ""} ${item.name || ""}`.toLowerCase();
      return kindMatch && boardMatch && (!query || words.includes(query));
    }).sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
  }

  function statusText(payload) {
    if (!payload?.as_of) return "等待首次扫描";
    return Number(payload.coverage || 0) >= .75 ? "数据已更新" : "覆盖不足";
  }

  function renderOverview() {
    const payload = state.payload || {};
    const counts = payload.signal_counts || {};
    const coverage = Math.max(0, Math.min(1, Number(payload.coverage || 0)));
    $("#as-of").textContent = prettyDate(payload.as_of);
    $("#scan-time").textContent = prettyDate(payload.generated_at);
    $("#coverage").textContent = payload.as_of ? `${(coverage * 100).toFixed(1)}%` : "—";
    $("#coverage-bar").style.width = `${coverage * 100}%`;
    $("#scan-status").textContent = statusText(payload);
    $("#header-status").textContent = statusText(payload);
    $("#universe-count").textContent = payload.as_of ? number.format(Number(payload.universe_count || 0)) : "—";
    $("#support-count").textContent = payload.as_of ? number.format(Number(counts.support_retest || 0)) : "—";
    $("#breakout-count").textContent = payload.as_of ? number.format(Number(counts.sideways_breakout || 0)) : "—";
    $("#failure-count").textContent = payload.as_of ? number.format(Number(payload.failure_count || 0)) : "—";
    $("#scan-note").textContent = cleanText(payload.warning, "自动任务完成后，最新结果会显示在这里。");
    $("#all-count").textContent = number.format((payload.signals || []).length);
    $("#filter-support-count").textContent = number.format(Number(counts.support_retest || 0));
    $("#filter-breakout-count").textContent = number.format(Number(counts.sideways_breakout || 0));

    const warning = $("#data-warning");
    if (!payload.as_of) {
      warning.textContent = "首个自动扫描尚未发布。为避免误导，网站不展示任何虚构行情。";
      warning.classList.add("has-message");
    } else if (coverage < .75) {
      warning.textContent = `本次市场覆盖率为 ${(coverage * 100).toFixed(1)}%，低于正常发布门槛；请谨慎使用。`;
      warning.classList.add("has-message");
    } else {
      warning.textContent = "";
      warning.classList.remove("has-message");
    }
  }

  function renderBoards() {
    const select = $("#board");
    const current = state.board;
    const boards = [...new Set((state.payload?.signals || []).map((item) => item.board).filter(Boolean))].sort();
    select.replaceChildren();
    const allOption = new Option("全部板块", "all");
    select.add(allOption);
    boards.forEach((board) => select.add(new Option(board, board)));
    state.board = boards.includes(current) || current === "all" ? current : "all";
    select.value = state.board;
  }

  function metricLines(item) {
    if (signalKind(item.signal) === "support") {
      return [
        `距起涨位 ${cleanText(item.distance_to_start_pct)}%`,
        `前段涨幅 ${cleanText(item.prior_rally_pct)}%`,
        `MA20 / MA60 ${cleanText(item.ma20)} / ${cleanText(item.ma60)}`,
      ];
    }
    return [
      `量比 ${cleanText(item.volume_ratio)}`,
      `区间振幅 ${cleanText(item.range_pct)}%`,
      `突破位 ${cleanText(item.breakout_high)}`,
    ];
  }

  function rowFor(item) {
    const tr = document.createElement("tr");
    const stock = add(tr, "td");
    add(stock, "span", cleanText(item.name), "stock-name");
    add(stock, "span", cleanText(item.symbol), "stock-code");
    add(stock, "span", cleanText(item.board), "board-tag");

    const signalCell = add(tr, "td");
    const kind = signalKind(item.signal);
    add(signalCell, "span", cleanText(item.signal), `signal-tag signal-${kind}`);

    const priceCell = add(tr, "td");
    add(priceCell, "span", Number.isFinite(Number(item.close)) ? decimal.format(Number(item.close)) : "—", "price");

    const metrics = add(tr, "td", undefined, "metric-detail");
    metricLines(item).forEach((line) => add(metrics, "div", line));

    const scoreCell = add(tr, "td");
    const score = Math.max(0, Math.min(100, Number(item.score || 0)));
    const scoreWrap = add(scoreCell, "div", undefined, "score-wrap");
    add(scoreWrap, "span", `${score.toFixed(1)} / 100`, "score-text");
    const bar = add(scoreWrap, "span", undefined, "score-bar");
    const fill = add(bar, "i");
    fill.style.width = `${score}%`;

    add(tr, "td", cleanText(item.note), "note");
    return tr;
  }

  function renderResults() {
    const rows = $("#signal-rows");
    const items = filteredSignals();
    rows.replaceChildren(...items.map(rowFor));
    $("#empty-state").hidden = items.length > 0;
    const total = (state.payload?.signals || []).length;
    $("#result-summary").textContent = total
      ? `显示 ${items.length} / ${total} 条候选 · 已按信号分数排序`
      : "本次尚无可展示的候选信号";
  }

  function setFilterButtons() {
    document.querySelectorAll("[data-filter]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.filter === state.signal);
    });
  }

  async function loadData() {
    const refresh = $("#refresh-button");
    refresh.disabled = true;
    refresh.textContent = "正在刷新…";
    try {
      const response = await fetch(`${DATA_URL}?v=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.signals)) throw new Error("数据格式不正确");
      state.payload = payload;
      renderOverview();
      renderBoards();
      renderResults();
    } catch (error) {
      state.payload = { signals: [], warning: "无法载入最新扫描数据。请稍后刷新。" };
      renderOverview();
      renderBoards();
      renderResults();
      $("#data-warning").textContent = `数据加载失败：${error.message}`;
      $("#data-warning").classList.add("has-message");
    } finally {
      refresh.disabled = false;
      refresh.textContent = "刷新数据";
    }
  }

  function bindControls() {
    document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
      state.signal = button.dataset.filter;
      setFilterButtons();
      renderResults();
    }));
    $("#search").addEventListener("input", (event) => { state.query = event.target.value; renderResults(); });
    $("#board").addEventListener("change", (event) => { state.board = event.target.value; renderResults(); });
    $("#reset-filters").addEventListener("click", () => {
      state.signal = "all"; state.query = ""; state.board = "all";
      $("#search").value = ""; $("#board").value = "all";
      setFilterButtons(); renderResults();
    });
    $("#refresh-button").addEventListener("click", loadData);
  }

  bindControls();
  loadData();
})();
