(() => {
  "use strict";

  /* 数据源可通过 URL 参数覆盖（例如测试时 ?data=/preview/latest.json）
     默认使用 GitHub Pages 上由扫描器推送的最新快照。 */
  const DATA_URL = new URLSearchParams(location.search).get("data") || "./data/latest.json";
  const STALE_HOURS = 72; // 距 generated_at 超过该时长提示可能过期
  const STALE_MS = STALE_HOURS * 3600 * 1000;

  const state = { payload: null, loaded: false, signal: "all", query: "", board: "all" };
  const $ = (selector) => document.querySelector(selector);

  const number = new Intl.NumberFormat("zh-CN");
  const decimal = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const dateTime = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" });

  /* ---------- 基础工具 ---------- */

  const cleanText = (value, fallback = "—") =>
    value === undefined || value === null || value === "" ? fallback : String(value);

  const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : null);

  const SIGNAL_KINDS = {
    "回踩前期起涨位": "support",
    "横盘后放量突破": "breakout",
    "箱体突破红肥绿瘦": "box",
    "阳包阴反包启动": "engulfing",
    "涨停跳空缺口共振": "limitup",
    "龙回头二次启动": "dragon",
    "均线多头散发": "ma",
    "低位仙人指路": "shadow",
  };
  const signalKind = (signal) => SIGNAL_KINDS[signal] || "other";

  const parseTime = (value) => {
    if (!value) return null;
    const compact = String(value).match(/^(\d{4})(\d{2})(\d{2})$/);
    if (compact) return new Date(+compact[1], +compact[2] - 1, +compact[3]);
    const dashed = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (dashed) return new Date(+dashed[1], +dashed[2] - 1, +dashed[3]);
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? null : parsed;
  };

  const prettyDate = (value) => {
    if (value === null || value === undefined || value === "") return "—";
    const compact = String(value).match(/^(\d{4})(\d{2})(\d{2})$/);
    if (compact) return `${compact[1]}.${compact[2]}.${compact[3]}`;
    const dashed = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (dashed) return `${dashed[1]}.${dashed[2]}.${dashed[3]}`;
    const parsed = parseTime(value);
    return parsed ? dateTime.format(parsed) : String(value);
  };

  const ageOf = (value) => {
    const parsed = parseTime(value);
    return parsed ? Date.now() - parsed.valueOf() : null;
  };

  const relLabel = (ms) => {
    if (ms === null || ms === undefined) return "—";
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  };

  /* A 股习惯：正差价 / 涨幅 → 红（is-up），负差价 → 绿（is-down） */
  const signClass = (value) => {
    const n = num(value);
    if (n === null) return "";
    return n >= 0 ? "is-up" : "is-down";
  };

  const signedPct = (value) => {
    const n = num(value);
    if (n === null) return "—";
    return `${n > 0 ? "+" : ""}${decimal.format(n)}%`;
  };

  const pct = (value) => {
    const n = num(value);
    return n === null ? "—" : `${decimal.format(n)}%`;
  };

  const compactVolume = (value) => {
    const n = num(value);
    if (n === null) return "—";
    const abs = Math.abs(n);
    if (abs >= 1e8) return `${decimal.format(n / 1e8)} 亿`;
    if (abs >= 1e4) return `${decimal.format(n / 1e4)} 万`;
    return number.format(n);
  };

  const add = (parent, tag, text, className) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  };

  /* ---------- 数据状态 ---------- */

  const dataState = (payload) => {
    if (!payload?.as_of) return "pending";
    const age = ageOf(payload.generated_at);
    if (age !== null && age > STALE_MS) return "stale";
    if (Number(payload.coverage || 0) < 0.75) return "lowcoverage";
    return "ok";
  };

  const STATUS_TEXT = {
    pending: "等待首次扫描",
    lowcoverage: "覆盖不足",
    stale: "数据可能过期",
    ok: "数据已更新",
    failed: "数据异常",
    loading: "正在加载数据…",
  };

  function setStatus(stateKey) {
    const pillClasses = { ok: "is-ok", pending: "is-pending", stale: "is-stale", lowcoverage: "is-stale", failed: "is-error", loading: "is-pending" };
    const ledClasses = { ok: "is-ok", pending: "is-pending", stale: "is-stale", lowcoverage: "is-stale", failed: "is-error", loading: "is-pending" };
    const pill = $("#scan-status");
    const led = $("#header-led");
    const text = $("#header-status");
    pill.className = `pill ${pillClasses[stateKey] || "is-pending"}`;
    pill.textContent = STATUS_TEXT[stateKey] || STATUS_TEXT.pending;
    if (led) led.className = `status-led ${ledClasses[stateKey] || "is-pending"}`;
    text.textContent = STATUS_TEXT[stateKey] || STATUS_TEXT.pending;
  }

  /* ---------- 概览区 ---------- */

  function renderOverview() {
    const payload = state.payload || {};
    const counts = payload.signal_counts || {};
    const coverage = Math.max(0, Math.min(1, Number(payload.coverage || 0)));
    const hasData = Boolean(payload.as_of);

    $("#as-of").textContent = prettyDate(payload.as_of);
    $("#scan-time").textContent = prettyDate(payload.generated_at);
    $("#published-at").textContent = prettyDate(payload.published_at);
    $("#coverage").textContent = hasData ? `${(coverage * 100).toFixed(1)}%` : "—";
    $("#coverage-bar").style.width = `${coverage * 100}%`;
    $("#scan-note").textContent = cleanText(payload.warning, "自动任务完成后，最新结果会显示在这里。");

    $("#universe-count").textContent = hasData ? number.format(Number(payload.universe_count || 0)) : "—";
    $("#support-count").textContent = hasData ? number.format(Number(counts.support_retest || 0)) : "—";
    $("#breakout-count").textContent = hasData ? number.format(Number(counts.sideways_breakout || 0)) : "—";
    $("#failure-count").textContent = hasData ? number.format(Number(payload.failure_count || 0)) : "—";
    ["#universe-count", "#support-count", "#breakout-count", "#failure-count"].forEach((sel) => {
      if (!hasData) $(sel).dataset.empty = "true";
      else delete $(sel).dataset.empty;
    });

    $("#header-freshness").textContent = hasData
      ? `更新于 ${relLabel(ageOf(payload.generated_at))}`
      : "—";

    $("#all-count").textContent = number.format((payload.signals || []).length);
    $("#filter-support-count").textContent = number.format(Number(counts.support_retest || 0));
    $("#filter-breakout-count").textContent = number.format(Number(counts.sideways_breakout || 0));
    renderExtraChips(counts);

    const warning = $("#data-warning");
    const setBanner = (kind, message) => {
      warning.className = `data-warning has-message ${kind}`;
      warning.textContent = message;
    };
    if (!payload.as_of && state.loaded) {
      setBanner("w-info", "首个自动扫描尚未发布。为避免误导，页面不展示任何虚构行情。");
    } else if (hasData && coverage < 0.75) {
      setBanner("w-warn", `本次市场覆盖率为 ${(coverage * 100).toFixed(1)}%，低于正常发布门槛；请谨慎使用。`);
    } else if (dataState(payload) === "stale") {
      setBanner("w-warn", `数据生成时间距现在已超过 ${STALE_HOURS} 小时（${relLabel(ageOf(payload.generated_at))}），可能已经过期；请核对扫描任务状态。`);
    } else if (hasData) {
      warning.className = "data-warning";
      warning.textContent = "";
    }
  }

  /* ---------- 高胜率形态筛选 chips ---------- */

  const EXTRA_KINDS = [
    ["box", "箱体突破红肥绿瘦", "box_breakout"],
    ["engulfing", "阳包阴反包启动", "bullish_engulfing"],
    ["limitup", "涨停跳空缺口共振", "limitup_gap"],
    ["dragon", "龙回头二次启动", "dragon_pullback"],
    ["ma", "均线多头散发", "ma_divergence"],
    ["shadow", "低位仙人指路", "low_shadow"],
  ];

  function renderExtraChips(counts) {
    const container = $("#extra-filter-chips");
    if (!container) return;
    container.replaceChildren();
    EXTRA_KINDS.forEach(([kind, label, key]) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "filter-button";
      chip.dataset.filter = kind;
      const count = Number(counts[key] || 0);
      if (count > 0) chip.classList.add("has-hits");
      add(chip, "span", label);
      add(chip, "span", number.format(count));
      chip.addEventListener("click", () => {
        state.signal = kind;
        setFilterButtons();
        renderResults();
      });
      container.append(chip);
    });
  }

  /* ---------- 板块筛选 ---------- */

  function renderBoards() {
    const select = $("#board");
    const current = state.board;
    const boards = [...new Set((state.payload?.signals || []).map((item) => item.board).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
    select.replaceChildren();
    select.add(new Option("全部板块", "all"));
    boards.forEach((board) => select.add(new Option(board, board)));
    state.board = boards.includes(current) || current === "all" ? current : "all";
    select.value = state.board;
  }

  /* ---------- 结果列表 ---------- */

  function filteredSignals() {
    const signals = Array.isArray(state.payload?.signals) ? state.payload.signals : [];
    const query = state.query.trim().toLowerCase();
    return signals
      .filter((item) => {
        const kindMatch = state.signal === "all" || signalKind(item.signal) === state.signal;
        const boardMatch = state.board === "all" || item.board === state.board;
        const haystack = `${item.symbol || ""} ${item.name || ""}`.toLowerCase();
        return kindMatch && boardMatch && (!query || haystack.includes(query));
      })
      .sort((a, b) => Number(b.score || 0) - Number(a.score || 0) || String(a.symbol || "").localeCompare(String(b.symbol || "")));
  }

  function metricLines(item) {
    if (signalKind(item.signal) === "support") {
      return [
        { text: `距起涨位 ${signedPct(item.distance_to_start_pct)}`, cls: signClass(item.distance_to_start_pct) },
        { text: `前段涨幅 ${signedPct(item.prior_rally_pct)}`, cls: signClass(item.prior_rally_pct) },
        { text: `MA20 / MA60 ${cleanText(item.ma20)} / ${cleanText(item.ma60)}`, cls: "" },
      ];
    }
    const vr = num(item.volume_ratio);
    const metricMap = {
      box: [
        { text: `量比 ${cleanText(item.volume_ratio)}`, cls: vr !== null && vr >= 2 ? "is-up" : "" },
        { text: `箱体上沿 ${cleanText(item.box_high)}`, cls: "" },
        { text: `红绿量比 ${cleanText(item.red_green_vol_ratio)}`, cls: "" },
      ],
      engulfing: [
        { text: `反包量比 ${cleanText(item.engulf_vol_ratio)}`, cls: "" },
        { text: `回调占比 ${cleanText(item.pullback_ratio)}`, cls: "" },
        { text: `前两日涨幅 ${signedPct(item.prior2_gain_pct)}`, cls: signClass(item.prior2_gain_pct) },
      ],
      limitup: [
        { text: `涨停日 ${prettyDate(item.limit_date)}`, cls: "" },
        { text: `缺口幅度 ${signedPct(item.gap_size_pct)}`, cls: signClass(item.gap_size_pct) },
        { text: `回调缩量比 ${cleanText(item.pullback_vol_ratio)}`, cls: "" },
      ],
      dragon: [
        { text: `量比 ${cleanText(item.second_vol_ratio)}`, cls: vr !== null && vr >= 2.3 ? "is-up" : "" },
        { text: `首波涨幅 ${signedPct(item.wave_gain_pct)}`, cls: signClass(item.wave_gain_pct) },
        { text: `前高 ${cleanText(item.prior_high)}`, cls: "" },
      ],
      ma: [
        { text: `量比 ${cleanText(item.volume_ratio)}`, cls: vr !== null && vr >= 2 ? "is-up" : "" },
        { text: `MA20/60 ${cleanText(item.ma20)}/${cleanText(item.ma60)}`, cls: "" },
        { text: `突破位 ${cleanText(item.breakout_high)}`, cls: "" },
      ],
      shadow: [
        { text: `上影倍数 ${cleanText(item.shadow_ratio)}`, cls: "" },
        { text: `覆盖量比 ${cleanText(item.cover_vol_ratio)}`, cls: "" },
        { text: `60日涨幅 ${signedPct(item.prior_gain_60d_pct)}`, cls: signClass(item.prior_gain_60d_pct) },
      ],
    };
    const kind = signalKind(item.signal);
    if (metricMap[kind]) return metricMap[kind];
    return [
      { text: `量比 ${cleanText(item.volume_ratio)}`, cls: vr !== null && vr >= 1.8 ? "is-up" : "" },
      { text: `区间振幅 ${pct(item.range_pct)}`, cls: "" },
      { text: `突破位 ${cleanText(item.breakout_high)}`, cls: "" },
    ];
  }

  function rowFor(item) {
    const tr = document.createElement("tr");

    const stockCell = add(tr, "td");
    add(stockCell, "span", cleanText(item.name), "stock-name");
    add(stockCell, "span", cleanText(item.symbol), "stock-code");
    add(stockCell, "span", cleanText(item.board), "board-tag");

    const signalCell = add(tr, "td");
    add(signalCell, "span", cleanText(item.signal), `signal-tag signal-${signalKind(item.signal)}`);

    const priceCell = add(tr, "td", undefined, "td-num");
    add(priceCell, "span", num(item.close) !== null ? decimal.format(num(item.close)) : "—", "price");

    const metricsCell = add(tr, "td");
    metricLines(item).forEach((line) => add(metricsCell, "div", line.text, `metric-line ${line.cls}`.trim()));

    const scoreCell = add(tr, "td");
    const rawScore = Math.max(0, Math.min(100, Number(item.score || 0)));
    const scoreWrap = add(scoreCell, "div", undefined, "score-cell");
    const top = add(scoreWrap, "div", undefined, "score-top");
    add(top, "span", rawScore.toFixed(1), "score-num");
    add(top, "span", "/ 100", "score-den");
    const bar = add(scoreWrap, "span", undefined, "score-bar");
    const fill = add(bar, "i");
    fill.style.width = `${rawScore}%`;

    add(tr, "td", cleanText(item.note), "note");

    const actionCell = add(tr, "td");
    const detailButton = add(actionCell, "button", "详情", "detail-btn");
    detailButton.type = "button";
    detailButton.setAttribute("aria-haspopup", "dialog");
    detailButton.setAttribute("aria-label", `查看 ${cleanText(item.name)} ${cleanText(item.symbol)} 的完整字段`);

    tr.addEventListener("click", () => openDialog(item));
    return tr;
  }

  /* ---------- 空状态 ---------- */

  /* 以下 SVG 均为内置常量（不包含任何外部数据），以 innerHTML 注入空状态视觉区 */
  const EMPTY = {
    waiting: {
      cls: "v-waiting",
      title: "等待首次扫描",
      desc: "首个自动扫描尚未发布。为避免误导，页面不展示任何虚构行情。",
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.3" aria-hidden="true"><circle cx="12" cy="12" r="10" opacity=".45"></circle><circle cx="12" cy="12" r="5.5" opacity=".6"></circle><path d="M12 12 L19 5.4" stroke-linecap="round"></path><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"></circle></svg>',
    },
    noresult: {
      cls: "",
      title: "本次扫描无候选",
      desc: "扫描任务已完成，但本次交易日没有标的进入候选池；可等下一次收盘前扫描自动更新。",
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.3" aria-hidden="true"><rect x="4" y="4.5" width="16" height="15" rx="2" opacity=".6"></rect><path d="M4 12.5 h4.2 l1.4 2.6 h4.8 l1.4 -2.6 H20" opacity=".9"></path></svg>',
    },
    filtered: {
      cls: "v-filtered",
      title: "没有符合当前条件的候选",
      desc: "试试调整信号类型 / 板块筛选，或改用其他代码、名称关键词；也可直接重置筛选。",
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.4" aria-hidden="true"><path d="M4 5 h16 l-6.1 7.3 V19 l-3.8 -2.2 v-4.5 Z"></path></svg>',
    },
    failed: {
      cls: "v-failed",
      title: "数据加载失败",
      desc: "无法从数据源取得最新扫描结果。请稍后点击「刷新数据」重试。",
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.4" aria-hidden="true"><path d="M12 4 L21 19.5 H3 Z" opacity=".7"></path><path d="M12 10 v4.6" stroke-linecap="round"></path><circle cx="12" cy="17.3" r=".9" fill="currentColor" stroke="none"></circle></svg>',
    },
  };

  function setEmptyState(kind) {
    const container = $("#empty-state");
    const template = EMPTY[kind];
    if (!template) {
      container.hidden = true;
      return;
    }
    $("#empty-visual").className = `empty-visual ${template.cls}`.trim();
    $("#empty-visual").innerHTML = template.icon;
    $("#empty-title").textContent = template.title;
    $("#empty-desc").textContent = template.desc;
    container.hidden = false;
  }

  /* ---------- 渲染结果 ---------- */

  function renderResults() {
    const rows = $("#signal-rows");
    const items = filteredSignals();
    rows.replaceChildren(...items.map(rowFor));

    const payload = state.payload || {};
    const total = Array.isArray(payload.signals) ? payload.signals.length : 0;

    setEmptyState(
      !state.loaded ? null : items.length ? null : !payload.as_of ? "waiting" : total ? "filtered" : "noresult"
    );

    $("#result-summary").textContent = total
      ? `显示 ${items.length} / ${total} 条候选 · 按信号分数降序`
      : "本次尚无可展示的候选信号";
  }

  function setFilterButtons() {
    document.querySelectorAll("[data-filter]").forEach((button) => {
      const active = button.dataset.filter === state.signal;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  /* ---------- 详情对话框 ---------- */

  function dialogRows(item) {
    const kind = signalKind(item.signal);
    const rows = [
      { dt: "标的价格", dd: num(item.close) !== null ? decimal.format(num(item.close)) : "—" },
      { dt: "成交量", dd: compactVolume(item.volume) },
      {
        dt: "信号分数",
        dd: `${Math.max(0, Math.min(100, Number(item.score || 0))).toFixed(1)} / 100`,
        meter: Math.max(0, Math.min(100, Number(item.score || 0))) / 100,
      },
      { dt: "扫描时间", dd: prettyDate(item.scan_time) },
    ];
    if (kind === "support") {
      rows.push(
        { dt: "起涨日", dd: prettyDate(item.start_date) },
        { dt: "起涨价", dd: num(item.start_price) !== null ? decimal.format(num(item.start_price)) : "—" },
        { dt: "距起涨位", dd: signedPct(item.distance_to_start_pct), cls: signClass(item.distance_to_start_pct) },
        { dt: "前段涨幅", dd: signedPct(item.prior_rally_pct), cls: signClass(item.prior_rally_pct) },
        { dt: "MA20 / MA60", dd: `${cleanText(item.ma20)} / ${cleanText(item.ma60)}` },
        { dt: "区间振幅", dd: pct(item.range_pct) },
      );
    } else if (kind === "breakout") {
      const pos = num(item.close_position);
      rows.push(
        { dt: "量比", dd: cleanText(item.volume_ratio) },
        { dt: "区间振幅", dd: pct(item.range_pct) },
        { dt: "ATR 振幅", dd: pct(item.atr_pct) },
        { dt: "突破位", dd: cleanText(item.breakout_high) },
        {
          dt: "收盘位置",
          dd: pos !== null ? `${(Math.max(0, Math.min(1, pos)) * 100).toFixed(1)}%` : "—",
          meter: pos === null ? 0 : Math.max(0, Math.min(1, pos)),
          cls: pos !== null && pos >= 0.8 ? "is-up" : "",
        },
      );
    } else if (kind === "box") {
      rows.push(
        { dt: "箱体上沿", dd: cleanText(item.box_high) },
        { dt: "箱体振幅", dd: pct(item.range_pct) },
        { dt: "末端收敛比", dd: cleanText(item.converge_ratio) },
        { dt: "红绿量比", dd: cleanText(item.red_green_vol_ratio) },
        { dt: "突破量比", dd: cleanText(item.volume_ratio) },
      );
    } else if (kind === "engulfing") {
      rows.push(
        { dt: "回调占比", dd: cleanText(item.pullback_ratio) },
        { dt: "前两日涨幅", dd: signedPct(item.prior2_gain_pct), cls: signClass(item.prior2_gain_pct) },
        { dt: "反包量比", dd: cleanText(item.engulf_vol_ratio) },
        { dt: "量比", dd: cleanText(item.volume_ratio) },
      );
    } else if (kind === "limitup") {
      rows.push(
        { dt: "涨停日", dd: prettyDate(item.limit_date) },
        { dt: "缺口幅度", dd: signedPct(item.gap_size_pct), cls: signClass(item.gap_size_pct) },
        { dt: "距涨停天数", dd: cleanText(item.days_since_limit) },
        { dt: "回调缩量比", dd: cleanText(item.pullback_vol_ratio) },
      );
    } else if (kind === "dragon") {
      rows.push(
        { dt: "首波涨幅", dd: signedPct(item.wave_gain_pct), cls: signClass(item.wave_gain_pct) },
        { dt: "回调幅度", dd: signedPct(item.pullback_pct), cls: signClass(item.pullback_pct) },
        { dt: "回调缩量比", dd: cleanText(item.pullback_vol_ratio) },
        { dt: "二次启动量比", dd: cleanText(item.second_vol_ratio) },
        { dt: "前高", dd: cleanText(item.prior_high) },
      );
    } else if (kind === "ma") {
      rows.push(
        { dt: "MA20 / MA60", dd: `${cleanText(item.ma20)} / ${cleanText(item.ma60)}` },
        { dt: "均线乖离", dd: pct(item.ma_gap_pct) },
        { dt: "量比", dd: cleanText(item.volume_ratio) },
        { dt: "突破位", dd: cleanText(item.breakout_high) },
      );
    } else if (kind === "shadow") {
      rows.push(
        { dt: "上影倍数", dd: cleanText(item.shadow_ratio) },
        { dt: "上影日量比", dd: cleanText(item.shadow_vol_ratio) },
        { dt: "覆盖量比", dd: cleanText(item.cover_vol_ratio) },
        { dt: "60日涨幅", dd: signedPct(item.prior_gain_60d_pct), cls: signClass(item.prior_gain_60d_pct) },
      );
    }
    return rows;
  }

  function openDialog(item) {
    $("#dialog-title").textContent = `${cleanText(item.name)}（${cleanText(item.symbol)}）`;
    const tags = $("#dialog-tags");
    tags.replaceChildren();
    add(tags, "span", cleanText(item.board), "board-tag");
    add(tags, "span", cleanText(item.signal), `signal-tag signal-${signalKind(item.signal)}`);

    const dl = $("#dialog-meta");
    dl.replaceChildren();
    dialogRows(item).forEach((row) => {
      const wrap = add(dl, "div", undefined, "dm-row");
      add(wrap, "dt", row.dt);
      const ddNode = add(wrap, "dd", undefined, row.cls || undefined);
      if (row.meter !== undefined) {
        const meter = add(ddNode, "span", undefined, "dm-meter");
        add(meter, "span", row.dd, "dm-val");
        const bar = add(meter, "span", undefined, "dm-bar");
        const fill = add(bar, "i");
        fill.style.width = `${row.meter * 100}%`;
      } else {
        ddNode.textContent = row.dd;
      }
    });

    $("#dialog-note").textContent = cleanText(item.note, "本次无附加提示。");
    $("#signal-dialog").showModal();
  }

  function bindDialog() {
    const dialog = $("#signal-dialog");
    $("#dialog-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  /* ---------- 数据加载 ---------- */

  async function loadData() {
    const refresh = $("#refresh-button");
    const firstLoad = !state.loaded;
    refresh.disabled = true;
    refresh.textContent = "正在刷新…";
    if (firstLoad) setStatus("loading");
    try {
      const response = await fetch(`${DATA_URL}${DATA_URL.includes("?") ? "&" : "?"}v=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.signals)) {
        throw new Error("数据格式不符合约定（缺少 signals 数组）");
      }
      state.payload = payload;
      state.loaded = true;
      renderOverview();
      setStatus(dataState(payload));
      renderBoards();
      renderResults();
    } catch (error) {
      const hadData = Boolean(state.payload?.as_of);
      if (!state.loaded) state.payload = { signals: [] };
      setStatus("failed");
      renderOverview();
      renderBoards();
      renderResults();
      if (!hadData) setEmptyState("failed"); /* 首次加载即失败：显示专用的失败空状态 */
      $("#data-warning").className = "data-warning has-message w-error";
      $("#data-warning").textContent = state.payload?.as_of
        ? `本次刷新失败：${error.message}。当前仍显示上次加载的数据。`
        : `数据加载失败：${error.message}。请稍后点击「刷新数据」重试。`;
    } finally {
      refresh.disabled = false;
      refresh.textContent = "刷新数据";
    }
  }

  /* ---------- 控件绑定 ---------- */

  function bindControls() {
    document.querySelectorAll("[data-filter]").forEach((button) =>
      button.addEventListener("click", () => {
        state.signal = button.dataset.filter;
        setFilterButtons();
        renderResults();
      })
    );
    $("#search").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderResults();
    });
    $("#board").addEventListener("change", (event) => {
      state.board = event.target.value;
      renderResults();
    });
    $("#reset-filters").addEventListener("click", () => {
      state.signal = "all";
      state.query = "";
      state.board = "all";
      $("#search").value = "";
      $("#board").value = "all";
      setFilterButtons();
      renderResults();
    });
    $("#refresh-button").addEventListener("click", loadData);
    bindDialog();
  }

  bindControls();
  renderOverview();
  setFilterButtons();
  loadData();
})();
