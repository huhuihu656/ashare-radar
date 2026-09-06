"""Two-year backtest of the full signal system with the 1:3 plan.

Simulation rules (A-share T+1, all fees included):
  - Signals computed on completed day T bars (same 8 patterns + risk guards +
    position model as the live scanner; moneyflow lagged to the session known
    at T's close).
  - Entry at T+1 open.  Skipped when T+1 open gaps >5% above the entry
    reference (no chasing) or gaps below the stop (pattern invalidated).
  - Position size = model position_pct of current equity, 100-share lots;
    0% positions are skipped.
  - Exits start T+2 (T+1 rule): stop or target checked against the day's
    low/high (stop priority when both hit); 40-session time stop at close.
  - Costs: commission 0.025% (min 5 CNY) both sides, sell +0.05% stamp.
    Initial capital 10,000 CNY.

Disclosed simplifications:
  - Completed daily bars (post-close mode of the live system since 2026-09-04).
  - Universe/ST filters use today's listing & names (survivorship bias
    present); delisted names are absent.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ashare_monitor.config import load  # noqa: E402
from ashare_monitor.signals import entry_exit_plan, position_strategy, scan_frame  # noqa: E402

START = "20240902"
END_SIGNAL = "20260902"
END_DATA = "20260904"
INITIAL_CAPITAL = 10_000.0
COMMISSION = 0.00025
COMMISSION_MIN = 5.0
STAMP = 0.0005
CHASE_CAP = 0.05
TIME_STOP = 40


def limit_pct_for(symbol: str) -> float:
    if symbol.startswith("688") or symbol.startswith("300") or symbol.startswith("301"):
        return 0.20
    if symbol.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def main() -> None:
    cfg = load(ROOT / "config.yaml")
    cache_dir = ROOT / "data" / "backtest_cache"
    mf_path = ROOT / "data" / "backtest_moneyflow.csv"

    pro = _ts_pro()
    if pro is None:
        print("Tushare unavailable")
        raise SystemExit(2)

    # ---- calendars ----
    cal = pro.trade_cal(exchange="SSE", start_date="20240101",
                        end_date=END_DATA, is_open="1")
    sessions = sorted(cal["cal_date"].astype(str).tolist())
    sidx = {d: i for i, d in enumerate(sessions)}

    # ---- index (for market env) ----
    idx_raw = pro.index_daily(ts_code="000001.SH", start_date="20231201", end_date=END_DATA)
    idx_raw = idx_raw.rename(columns={"trade_date": "date"})
    idx_raw["date"] = pd.to_datetime(idx_raw["date"])
    idx = idx_raw.set_index("date").sort_index()[["close"]]

    # ---- market env per session (precomputed once) ----
    env_by_day: dict[str, str] = {}
    for day in sessions:
        env_by_day[day] = market_env_at(day, sessions, idx)

    # ---- moneyflow ----
    mf = {}
    if mf_path.exists():
        mf_frame = pd.read_csv(mf_path, dtype={"symbol": str})
        for _, r in mf_frame.iterrows():
            mf[(r["trade_date"], r["symbol"].zfill(6))] = float(r["net_mf_amount"])

    # ---- symbols ----
    basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
    names = dict(zip(basic["ts_code"].astype(str).str[:6], basic["name"]))

    # ---- preload all price frames ----
    files = sorted(cache_dir.glob("*.csv"))
    frames: dict[str, pd.DataFrame] = {}
    for f in files:
        sym = f.stem
        if "ST" in str(names.get(sym, "")).upper():
            continue
        if sym.startswith(("8", "4", "92")):   # 沪深两市回测，排除北交所
            continue
        frame = pd.read_csv(f, parse_dates=["date"]).sort_values("date").set_index("date")
        if len(frame) >= 155:
            frames[sym] = frame
    print(f"[bt] loaded {len(frames)} symbols / {len(sessions)} sessions", flush=True)

    # ---- signal scan (parallel over symbols) ----
    from concurrent.futures import ThreadPoolExecutor

    window = [d for d in sessions if START <= d <= END_SIGNAL]
    candidates: list[dict] = []
    items = list(frames.items())
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(evaluate_symbol, sym, frame, window, sidx,
                               sessions, mf, env_by_day, cfg)
                   for sym, frame in items]
        for done, future in enumerate(futures, 1):
            candidates.extend(future.result())
            if done % 800 == 0:
                print(f"[bt] scanned {done}/{len(items)}: {len(candidates)} candidates", flush=True)
    print(f"[bt] total candidates {len(candidates)}", flush=True)

    report = simulate(candidates, sessions, sidx, frames)
    out = ROOT / "data" / "backtest_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def _ts_pro():
    from ashare_monitor.data import _ts_pro as _p

    return _p()


def market_env_at(day: str, sessions: list[str], idx: pd.DataFrame) -> str:
    i = sessions.index(day)
    lo = max(0, i - 119)
    close = idx["close"]
    w = close.iloc[lo: i + 1]
    ma20 = float(w.rolling(20).mean().iloc[-1])
    ma60 = float(w.rolling(60).mean().iloc[-1])
    price = float(w.iloc[-1])
    if price < ma20 < ma60:
        return "偏弱"
    if price > ma20 > ma60:
        return "偏强"
    return "中性"


def evaluate_symbol(symbol: str, frame: pd.DataFrame, window: list[str],
                    sidx: dict, sessions: list[str], mf: dict,
                    env_by_day: dict[str, str], cfg,
                    last_hit_seed: dict[str, str] | None = None) -> list[dict]:
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    open_ = frame["open"].astype(float)
    vol = frame["volume"].astype(float)
    date_str = frame.index.strftime("%Y%m%d")
    pos = {d: i for i, d in enumerate(date_str)}
    n = len(frame)

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol_ma20 = vol.rolling(20).mean()
    vol_ma50 = vol.rolling(50).mean()
    support_pre = (close < ma20) & (ma20 < ma60) & (ma20 < ma20.shift(6))
    vol_surge = vol > vol_ma20 * 1.5
    rmax40 = high.rolling(40).max().shift(1)
    rmin40 = low.rolling(40).min().shift(1)
    box_pre = (rmax40 / rmin40 - 1 <= 0.25) & (close > rmax40) & vol_surge
    prev_red = close.shift(1) < open_.shift(1)
    today_green = close > open_
    engulf_pre = prev_red & today_green & (close >= high.shift(1))
    rmax20 = high.rolling(20).max().shift(1)
    ma_pre = (ma20 > ma60) & (vol > vol_ma50) & (close >= rmax20)
    body = (open_.shift(1) - close.shift(1)).abs()
    upper = high.shift(1) - np.maximum(open_.shift(1), close.shift(1))
    shadow_pre = (body > 0) & (upper / body >= 2.0) & today_green & (close >= high.shift(1))

    pct = limit_pct_for(symbol)
    limit_price = (close.shift(1) * (1 + pct)).round(2)
    is_limit = (close >= limit_price - 0.003) & (close == high)
    limit20 = is_limit.rolling(20, min_periods=1).max().fillna(0).astype(bool)
    swing60 = (high.rolling(60).max() / low.rolling(60).min() - 1) >= 0.20

    mask = (support_pre | box_pre | engulf_pre | limit20 | swing60 | ma_pre | shadow_pre).to_numpy()
    hits: list[dict] = []
    # 增量扫描（signal_track.py）用 last_hit_seed 把「同信号同股 15 日剪枝」状态
    # 从已有跟踪表接续，保证与全量扫描口径一致。
    last_hit_day: dict[str, str] = dict(last_hit_seed) if last_hit_seed else {}  # signal -> day of last accepted hit
    for day in window:
        i = pos.get(day)
        if i is None or i < 155 or not mask[i]:
            continue
        env = env_by_day[day]
        net = mf.get((day, symbol))
        if net is None:
            j = sidx[day]
            for back in (1, 2):
                prev_day = sessions[j - back]
                if prev_day >= sessions[0]:
                    net = mf.get((prev_day, symbol))
                    if net is not None:
                        break
        slice_ = frame.iloc[max(0, i - 320): i + 1]
        rows = scan_frame(slice_, cfg.support_retest, cfg.breakout, cfg.risk,
                          cfg.box_breakout, cfg.bullish_engulfing, cfg.limitup_gap,
                          cfg.dragon_pullback, cfg.ma_divergence, cfg.low_shadow,
                          cfg.oversold_reversal, cfg.break_ma20,
                          limit_pct=pct)
        # 同一信号同股 15 个交易日内只取首个（贴近实盘持仓节奏，且剪枝）
        fresh_rows = []
        for r in rows:
            prev = last_hit_day.get(r["signal"])
            spacing_ok = True
            if prev is not None:
                day_i = sidx[day]
                prev_i = sidx.get(prev)
                if prev_i is not None and day_i - prev_i < 15:
                    spacing_ok = False
            if spacing_ok:
                fresh_rows.append(r)
        rows = fresh_rows
        for row in rows:
            row = dict(row)
            row.update({"symbol": symbol, "signal_day": day, "market_env": env})
            if net is not None and pd.notna(net):
                row["net_mf_amount"] = round(net, 1)
            row.update(position_strategy(row, env))
            plan = entry_exit_plan(row)
            if not plan:
                continue
            row.update(plan)
            pct_pos = float(row.get("position_pct") or 0)
            if pct_pos <= 0:
                continue
            row["position_pct"] = pct_pos
            hits.append(row)
            if pct_pos > 5:
                last_hit_day[row["signal"]] = day
    return hits


def simulate(candidates: list[dict], sessions: list[str], sidx: dict,
             frames: dict[str, pd.DataFrame]) -> dict:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_day[c["signal_day"]].append(c)

    cash = INITIAL_CAPITAL
    holdings: list[dict] = []
    trades: list[dict] = []
    equity: list[tuple[str, float]] = []

    def bar_of(sym: str, day: str):
        f = frames.get(sym)
        if f is None:
            return None
        ts = pd.Timestamp(day)
        if ts in f.index:
            return f.loc[ts]
        return None

    def total_value(day: str) -> float:
        value = cash
        for h in holdings:
            b = bar_of(h["symbol"], day)
            value += h["shares"] * (float(b.close) if b is not None else h["entry_price"])
        return value

    for day in sessions:
        # ---- exits first (T+1: position entered at day D can exit from D+1) ----
        still: list[dict] = []
        for h in holdings:
            if day <= h["entry_day"]:
                still.append(h)
                continue
            held = sidx[day] - sidx[h["entry_day"]]
            b = bar_of(h["symbol"], day)
            exit_price = None
            reason = None
            if b is not None:
                if float(b.low) <= h["stop_loss"]:
                    exit_price = h["stop_loss"]; reason = "止损"
                elif float(b.high) >= h["take_profit"]:
                    exit_price = h["take_profit"]; reason = "止盈"
                elif held >= TIME_STOP:
                    exit_price = float(b.close); reason = "超时"
            if exit_price is None:
                still.append(h)
                continue
            proceeds = h["shares"] * exit_price
            fee = max(COMMISSION_MIN, proceeds * COMMISSION) + proceeds * STAMP
            cash += proceeds - fee
            net = (proceeds - fee) - (h["cost"] + h["buy_fee"])
            trades.append({
                "symbol": h["symbol"], "signal": h["signal"],
                "entry_day": h["entry_day"], "exit_day": day,
                "entry_price": round(h["entry_price"], 3), "exit_price": round(exit_price, 3),
                "reason": reason, "shares": h["shares"],
                "position_pct": h["position_pct"], "net_pnl": round(net, 2),
                "win": net > 0,
            })
        holdings = still

        # ---- entries at T+1 open ----
        for c in sorted(by_day.get(day, []), key=lambda x: -x["score"]):
            f = frames.get(c["symbol"])
            if f is None:
                continue
            after = f.index[f.index.strftime("%Y%m%d") > day]
            if len(after) == 0:
                continue
            entry_day = after[0].strftime("%Y%m%d")
            if entry_day > END_DATA:
                continue
            ob = f.loc[after[0]]
            op = float(ob.open)
            if op <= 0 or op > c["entry_price"] * (1 + CHASE_CAP) or op < c["stop_loss"]:
                continue
            equity_now = total_value(day)
            budget = equity_now * c["position_pct"] / 100.0
            shares = int(budget / op / 100) * 100
            if shares < 100:
                continue
            cost = shares * op
            buy_fee = max(COMMISSION_MIN, cost * COMMISSION)
            if cash < cost + buy_fee:
                continue
            cash -= cost + buy_fee
            holdings.append({
                "symbol": c["symbol"], "signal": c["signal"],
                "entry_day": entry_day, "entry_price": op,
                "stop_loss": c["stop_loss"], "take_profit": c["take_profit"],
                "shares": shares, "cost": cost, "buy_fee": buy_fee,
                "position_pct": c["position_pct"],
            })

        equity.append((day, round(total_value(day), 2)))

    curve = pd.Series([v for _, v in equity], index=pd.to_datetime([d for d, _ in equity]))
    final_equity = float(curve.iloc[-1]) if len(curve) else INITIAL_CAPITAL
    total_return = final_equity / INITIAL_CAPITAL - 1
    drawdown = float((curve / curve.cummax() - 1).min()) if len(curve) else 0.0
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    net_win_rate = wins / n if n else 0
    win_pnls = [t["net_pnl"] for t in trades if t["win"]]
    loss_pnls = [t["net_pnl"] for t in trades if not t["win"]]
    avg_win = float(np.mean(win_pnls)) if win_pnls else 0
    avg_loss = float(np.mean(loss_pnls)) if loss_pnls else 0

    by_signal: dict[str, dict] = {}
    by_reason: dict[str, dict] = {}
    for t in trades:
        for bucket, key in ((by_signal, t["signal"]), (by_reason, t["reason"])):
            b = bucket.setdefault(key, {"trades": 0, "wins": 0, "net": 0.0})
            b["trades"] += 1
            b["wins"] += 1 if t["win"] else 0
            b["net"] += t["net_pnl"]

    by_year: dict[str, dict] = {}
    for t in trades:
        y = t["exit_day"][:4]
        b = by_year.setdefault(y, {"trades": 0, "wins": 0, "net": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t["win"] else 0
        b["net"] += t["net_pnl"]

    days = len(sessions[sessions.index(START):])
    years = days / 244
    return {
        "initial_capital": INITIAL_CAPITAL,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_pct": round(((final_equity / INITIAL_CAPITAL) ** (1 / max(years, 0.01)) - 1) * 100, 2),
        "max_drawdown_pct": round(drawdown * 100, 2),
        "trades": n,
        "win_rate_pct": round(net_win_rate * 100, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_rr_realized": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None,
        "open_positions_at_end": len(holdings),
        "by_signal": {k: {**v, "win_rate_pct": round(v["wins"] / v["trades"] * 100, 2), "net_pnl": round(v["net"], 2)} for k, v in by_signal.items()},
        "by_exit_reason": {k: {**v, "win_rate_pct": round(v["wins"] / v["trades"] * 100, 2), "net_pnl": round(v["net"], 2)} for k, v in by_reason.items()},
        "by_year": {k: {**v, "win_rate_pct": round(v["wins"] / v["trades"] * 100, 2), "net_pnl": round(v["net"], 2)} for k, v in by_year.items()},
        "window": f"{START} -> {END_DATA}",
    }


if __name__ == "__main__":
    main()
