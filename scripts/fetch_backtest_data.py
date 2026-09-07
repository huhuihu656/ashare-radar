"""Fetch two years of history + moneyflow for the backtest.

Writes into data/backtest_cache/ (separate from the live 320-day cache) and
data/backtest_moneyflow.csv.  Reuses the live bulk refresh (Tushare daily +
adj_factor, forward-adjusted) and the moneyflow API.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ashare_monitor.data import _ts_pro  # noqa: E402
from ashare_monitor.data import refresh_history_cache_bulk  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    cache_dir = root / "data" / "backtest_cache"
    pro = _ts_pro()
    if pro is None:
        print("Tushare unavailable; aborting.")
        raise SystemExit(2)

    # 1) Price history: 620 trading days (~2.5 years) so the 2-year window has
    #    a full 150-bar warmup for every pattern.
    written, sessions = refresh_history_cache_bulk(cache_dir, lookback_days=620, force=True)
    print(f"[history] {written} symbols / {sessions} sessions", flush=True)

    # 2) Moneyflow: from 2024-08-01 (T-1 lag for signals starting 2024-09).
    cal = pro.trade_cal(exchange="SSE", start_date="20240801",
                        end_date=date.today().strftime("%Y%m%d"), is_open="1")
    days = sorted(cal["cal_date"].astype(str).tolist())
    out = root / "data" / "backtest_moneyflow.csv"
    frames = []
    got = 0
    for day in days:
        try:
            raw = pro.moneyflow(trade_date=day)
        except Exception as error:
            print(f"[moneyflow] {day} failed: {error}", flush=True)
            continue
        if raw is None or raw.empty:
            continue
        raw = raw.copy()
        raw["symbol"] = raw["ts_code"].astype(str).str[:6]
        frames.append(raw[["symbol", "trade_date", "net_mf_amount"]])
        got += 1
        if got % 60 == 0:
            print(f"[moneyflow] {got}/{len(days)}", flush=True)
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined.to_csv(out, index=False)
        print(f"[moneyflow] wrote {out} ({len(combined)} rows)", flush=True)


if __name__ == "__main__":
    main()
