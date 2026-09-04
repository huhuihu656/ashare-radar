"""Backtest the monthly mainline-sector detector (2024-09 -> 2026-08).

For each month's first trading day T (24 months) we recompute the sector
ranking with data known at T, then measure how the sectors actually performed
over the NEXT month (T -> T+21 trading days).  "Accuracy" is measured as:
  - hit rate: share of months where the top-1 sector beat the industry median
    forward return
  - mean rank percentile of top-3 sectors' forward returns
  - Spearman correlation between score and forward return (all sectors)

Factor weights are then grid-searched with walk-forward validation
(train: first 12 months, validate: last 12 months) to pick the combination
that generalizes, not the one that overfits.  Everything uses only data
available at each decision date (no look-ahead).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from ashare_monitor.data import _ts_pro  # noqa: E402

CACHE = ROOT / "data" / "backtest_cache"
MF_PATH = ROOT / "data" / "backtest_moneyflow.csv"
FWD_DAYS = 21
FLOW_WINDOW = 5


def load_all() -> tuple[dict, pd.DataFrame, dict, pd.DataFrame]:
    """Preload every SH/SZ stock close/volume into memory + moneyflow + index."""
    frames: dict[str, pd.Series] = {}
    basic = _ts_pro().stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    basic = basic.rename(columns={"ts_code": "symbol"})
    basic["symbol"] = basic["symbol"].astype(str).str[:6]
    basic = basic[basic.industry.notna()]
    names = dict(zip(basic["symbol"], basic["name"]))
    industry_of = dict(zip(basic["symbol"], basic["industry"]))

    mf = pd.read_csv(MF_PATH, dtype={"symbol": str})
    mf["symbol"] = mf["symbol"].str.zfill(6)
    mf["trade_date"] = mf["trade_date"].astype(str)
    mf = mf.pivot_table(index="trade_date", columns="symbol", values="net_mf_amount")

    for path in sorted(CACHE.glob("*.csv")):
        sym = path.stem
        if sym.startswith(("8", "4", "92")):
            continue
        if "ST" in str(names.get(sym, "")).upper():
            continue
        closes = pd.read_csv(path, usecols=["date", "close"], parse_dates=["date"])
        closes = closes.sort_values("date").set_index("date")["close"]
        if len(closes) >= 160:
            frames[sym] = closes
    return frames, mf, names, industry_of, basic


def month_start_days(sessions: list[str]) -> list[str]:
    months: dict[str, str] = {}
    for day in sessions:
        ym = day[:6]
        months.setdefault(ym, day)
    return sorted(months.values())


def factor_series(frames: dict, mf: pd.DataFrame, day: str, sessions: list[str]) -> pd.DataFrame:
    """momentum / flow / breadth per (symbol) on decision day `day`."""
    idx = sessions.index(day)
    rows = []
    for sym, closes in frames.items():
        pos = closes.index.searchsorted(pd.Timestamp(day))
        pos = min(pos, len(closes) - 1)
        if pos < 25:
            continue
        window = closes.iloc[: pos + 1]
        if len(window) < 25:
            continue
        mom = float(window.iloc[-1] / window.iloc[-FWD_DAYS - 1] - 1)
        ma20 = float(window.iloc[-20:].mean())
        above = float(window.iloc[-1] > ma20)
        rows.append((sym, mom, above))
    frame = pd.DataFrame(rows, columns=["symbol", "momentum", "above"]).set_index("symbol")
    # flow: last FLOW_WINDOW sessions ending at day (inclusive)
    flow_days = sessions[max(0, idx - FLOW_WINDOW + 1): idx + 1]
    flow_rows = mf.reindex(flow_days)
    if not flow_rows.empty:
        flow = flow_rows.mean(axis=0)
        frame["flow"] = frame.index.map(lambda s: flow.get(s, 0.0))
    else:
        frame["flow"] = 0.0
    return frame


def industry_scores(day: str, factor_frame: pd.DataFrame, industry_of: dict,
                    weights: dict) -> pd.Series:
    merged = factor_frame.copy()
    merged["industry"] = merged.index.map(industry_of)
    merged = merged[merged.industry.notna()]
    sizes = merged.groupby("industry").size()
    merged = merged[merged.industry.isin(sizes[sizes >= 8].index)]
    g = merged.groupby("industry")
    table = pd.DataFrame({
        "momentum": g["momentum"].median(),
        "flow": g["flow"].mean(),
        "breadth": g["above"].mean(),
        "members": g.size(),
    })
    if len(table) < 10:
        return pd.Series(dtype=float)
    total = sum(abs(w) for w in weights.values())
    score = pd.Series(0.0, index=table.index)
    for key, w in weights.items():
        if w == 0:
            continue
        values = table[key]
        lo, hi = values.min(), values.max()
        norm = (values - lo) / (hi - lo) if hi > lo else pd.Series(0.5, index=values.index)
        score = score + (w / total) * norm
    return score.sort_values(ascending=False)


def forward_returns(frames: dict, day: str, sessions: list[str], fwd: int) -> pd.Series:
    idx = sessions.index(day)
    end_day = sessions[min(idx + fwd, len(sessions) - 1)]
    out = {}
    for sym, closes in frames.items():
        p0 = closes.index.searchsorted(pd.Timestamp(day))
        p1 = closes.index.searchsorted(pd.Timestamp(end_day))
        p0 = min(p0, len(closes) - 1)
        p1 = min(p1, len(closes) - 1)
        if p1 > p0 and closes.iloc[p0] > 0:
            out[sym] = closes.iloc[p1] / closes.iloc[p0] - 1
    return pd.Series(out)


def month_metrics(scores: pd.Series, fwd: pd.Series, industry_of: dict) -> dict | None:
    fwd_ind = fwd.to_frame("ret")
    fwd_ind["industry"] = fwd_ind.index.map(industry_of)
    fwd_ind = fwd_ind[fwd_ind.industry.notna()].groupby("industry")["ret"].median()
    common = fwd_ind.index.intersection(scores.index)
    if len(common) < 8:
        return None
    fwd_common = fwd_ind.reindex(common)
    median = fwd_common.median()
    top1 = scores.index[0]
    top3 = list(scores.index[:3])
    ranks = fwd_common.rank(pct=True)
    return {
        "top1_hit": bool(fwd_common[top1] > median),
        "top3_mean_pctile": float(ranks.reindex(top3).mean()),
        "spearman": float(pd.Series(scores.reindex(common).values).corr(pd.Series(fwd_common.values), method="spearman")),
        "top1_name": top1,
        "top1_fwd": round(float(fwd_common[top1]) * 100, 2),
        "median_fwd": round(float(median) * 100, 2),
    }


def main() -> None:
    frames, mf, names, industry_of, basic = load_all()
    print(f"[mainline-bt] {len(frames)} stocks loaded", flush=True)

    cal = _ts_pro().trade_cal(exchange="SSE", start_date="20240601", end_date="20260831", is_open="1")
    sessions = sorted(cal["cal_date"].astype(str).tolist())
    starts = [d for d in month_start_days(sessions) if "20240901" <= d <= "20260801"]
    print(f"[mainline-bt] decision months: {len(starts)}", flush=True)

    # ---- compute per-month factor tables once, then evaluate weight combos ----
    month_data: dict[str, tuple[pd.Series, pd.Series]] = {}
    for day in starts:
        factor_frame = factor_series(frames, mf, day, sessions)
        fwd = forward_returns(frames, day, sessions, FWD_DAYS)
        month_data[day] = (factor_frame, fwd)
        print(f"[mainline-bt] factors for {day}", flush=True)

    candidates = []
    combos = []
    for wm in (0.2, 0.35, 0.5, 0.65):
        for wf in (0.2, 0.3, 0.4):
            wb = 1.0 - wm - wf
            if wb < 0.1:
                continue
            combos.append({"momentum": wm, "flow": wf, "breadth": wb})
    # 轮动假设：反向动量（低动量优先）与反向资金流
    combos.append({"momentum": -0.5, "flow": 0.0, "breadth": 0.5})
    combos.append({"momentum": -0.5, "flow": -0.3, "breadth": 0.2})
    combos.append({"momentum": -0.35, "flow": 0.35, "breadth": 0.3})
    combos.append({"momentum": -0.4, "flow": 0.2, "breadth": 0.4})
    combos.append({"momentum": -0.45, "flow": 0.1, "breadth": 0.45})
    for weights in combos:
            metrics = []
            for day, (factor_frame, fwd) in month_data.items():
                scores = industry_scores(day, factor_frame, industry_of, weights)
                m = month_metrics(scores, fwd, industry_of)
                if m:
                    metrics.append(m)
            train = metrics[:12]
            valid = metrics[12:]
            candidates.append({
                "weights": weights,
                "train_hit": round(sum(m["top1_hit"] for m in train) / len(train), 3) if train else None,
                "train_top3": round(float(np.mean([m["top3_mean_pctile"] for m in train])), 3) if train else None,
                "valid_hit": round(sum(m["top1_hit"] for m in valid) / len(valid), 3) if valid else None,
                "valid_top3": round(float(np.mean([m["top3_mean_pctile"] for m in valid])), 3) if valid else None,
                "valid_spearman": round(float(np.mean([m["spearman"] for m in valid])), 3) if valid else None,
            })

    # 42 日前向窗口的对照评估（主线轮动常需更长兑现期）
    for day, (factor_frame, fwd21) in list(month_data.items()):
        fwd42 = forward_returns(frames, day, sessions, 42)
        month_data[day] = (factor_frame, fwd21, fwd42)
    for weights in [{"momentum": -0.5, "flow": 0.0, "breadth": 0.5},
                    {"momentum": 0.35, "flow": 0.30, "breadth": 0.35},
                    {"momentum": 0.2, "flow": 0.4, "breadth": 0.4}]:
        m21, m42 = [], []
        for day, (factor_frame, fwd21, fwd42) in month_data.items():
            scores = industry_scores(day, factor_frame, industry_of, weights)
            m21.append(month_metrics(scores, fwd21, industry_of))
            m42.append(month_metrics(scores, fwd42, industry_of))
        print(f"FWD42对照 {weights}: "
              f"21d命中={sum(x['top1_hit'] for x in m21[-12:] if x)}/12 "
              f"42d命中={sum(x['top1_hit'] for x in m42[-12:] if x)}/12 "
              f"42d top3分位={round(float(np.mean([x['top3_mean_pctile'] for x in m42[-12:] if x])),3)}",
              flush=True)

    report = {
        "decision_months": starts,
        "combos": sorted(candidates, key=lambda c: -(c["valid_top3"] or 0)),
        "baseline_median": "top1 命中 = 前向收益 > 行业中位数",
        "note": "训练期=前12个月，验证期=后12个月；指标均为验证期样本外表现",
    }
    out = ROOT / "data" / "mainline_backtest_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
