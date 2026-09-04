"""Paid data provider (Tushare Pro) integration.

The free quote feeds remain the *historical daily bar* source so forward
adjustment (qfq) stays consistent with the cached history and the signal math
never mixes adjustment bases.  Tushare contributes what free feeds cannot:

  - real main-force money flow (`moneyflow`, needs ~2000+ points) per trading
    day, used to annotate / sanity-check "capital entry" patterns;
  - a qfq daily fallback when every free host is down (`pro_bar`).

Token resolution order: $TUSHARE_TOKEN env var -> <project root>/.tushare_token
(never committed).  Every call fails soft: if the token is missing, the points
are insufficient or the API errors, callers get None and the scanner proceeds
with free-source data.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

_TS: Any = None
_TOKEN_CANDIDATES: tuple[str, ...] = ()


def _token_path() -> Path | None:
    for candidate in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
        p = candidate / ".tushare_token"
        if p.exists():
            return p
    return None


def _resolve_token() -> str | None:
    import os

    env = os.environ.get("TUSHARE_TOKEN")
    if env:
        return env.strip()
    path = _token_path()
    if path:
        return path.read_text(encoding="utf-8").strip()
    return None


def available() -> bool:
    """True when a token exists and the API answers."""
    global _TS
    token = _resolve_token()
    if not token:
        return False
    try:
        if _TS is None:
            import tushare as ts

            ts.set_token(token)
            _TS = ts.pro_api()
        _TS.trade_cal(exchange="SSE", start_date="20260901", end_date="20260905")
        return True
    except Exception:
        return False


def latest_moneyflow(trade_date: str | None = None, max_back: int = 3) -> pd.DataFrame | None:
    """Main-force money flow for the requested (or latest available) session.

    Returns a DataFrame indexed by 6-digit symbol with
    `mf_date`, `net_mf_amount` (10k CNY, + = main-force net inflow),
    `buy_lg_amount`, `sell_lg_amount`, `buy_elg_amount`, `sell_elg_amount`
    (10k CNY).  During 14:40 scans today's moneyflow may not be published yet,
    so callers pass max_back to walk back to the most recent closed session.
    """
    if not available():
        return None
    target = trade_date or date.today().strftime("%Y%m%d")
    for back in range(max_back):
        day = (pd.Timestamp(target) - pd.Timedelta(days=back)).strftime("%Y%m%d")
        try:
            raw = _TS.moneyflow(trade_date=day)
        except Exception:
            return None
        if raw is None or raw.empty:
            continue
        frame = raw.copy()
        frame["symbol"] = frame["ts_code"].astype(str).str[:6]
        keep = ["symbol", "trade_date", "net_mf_amount", "buy_lg_amount",
                "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"]
        frame = frame[keep].rename(columns={"trade_date": "mf_date"})
        for column in ["net_mf_amount", "buy_lg_amount", "sell_lg_amount",
                       "buy_elg_amount", "sell_elg_amount"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.set_index("symbol")
    return None


def qfq_daily(symbol6: str, start: date, end: date | None = None) -> pd.DataFrame | None:
    """Forward-adjusted daily bars as a date-indexed OHLCV frame (fallback only)."""
    if not available():
        return None
    try:
        import tushare as ts

        code = f"{symbol6}.SH" if symbol6.startswith(("6", "9")) or symbol6.startswith("688") \
            else f"{symbol6}.BJ" if symbol6.startswith(("4", "8")) \
            else f"{symbol6}.SZ"
        end = end or date.today()
        raw = ts.pro_bar(ts_code=code, adj="qfq",
                         start_date=start.strftime("%Y%m%d"),
                         end_date=end.strftime("%Y%m%d"))
        if raw is None or raw.empty:
            return None
        frame = raw.rename(columns={
            "trade_date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "vol": "volume"})
        frame.date = pd.to_datetime(frame.date)
        frame = frame.set_index("date").sort_index()
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna()
    except Exception:
        return None
