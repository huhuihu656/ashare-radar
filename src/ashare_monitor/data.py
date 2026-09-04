from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import time

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential


HISTORY_COLUMNS = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def get_universe() -> pd.DataFrame:
    """Live A-share universe via AkShare.

    Eastmoney spot covers main board, ChiNext, STAR and Beijing stocks, but its
    clist endpoint can throttle/drop direct connections; fall back to the Sina
    spot feed, which covers the same boards.  The caller records the exact
    successful coverage in run.json.
    """
    import akshare as ak
    try:
        raw = ak.stock_zh_a_spot_em()
        source = "eastmoney"
    except Exception:
        raw = ak.stock_zh_a_spot()
        source = "sina"
    required = {"代码", "名称", "最新价"}
    missing = required - set(raw.columns)
    if missing:
        raise RuntimeError(f"行情字段变化，缺少: {missing}")
    raw = raw.rename(columns={"代码": "symbol", "名称": "name", "最新价": "last_price"})
    if source == "sina":
        # Sina codes carry an exchange prefix (sh600000 / bj920000); keep the
        # bare 6-digit symbol so history download and board inference agree.
        raw["symbol"] = raw["symbol"].astype(str).str[-6:]
    return raw


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def is_ashare_trading_day(day: date | None = None) -> bool:
    """Check the official exchange-session calendar exposed by AkShare/Sina.

    A weekday is not necessarily a trading day.  The caller must skip rather
    than reuse a stale quote if this check cannot be completed.
    """
    import akshare as ak
    calendar = ak.tool_trade_date_hist_sina()
    if "trade_date" not in calendar.columns:
        raise RuntimeError("交易日历字段变化：缺少 trade_date")
    target = pd.Timestamp(day or date.today()).normalize()
    sessions = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.normalize()
    return bool(sessions.eq(target).any())


def infer_board(symbol: str) -> str:
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith(("688", "689")):
        return "科创板"
    if symbol.startswith(("8", "4")):
        return "北交所"
    return "主板"


def filter_universe(universe: pd.DataFrame, exclude_st: bool, allowed_boards: tuple[str, ...]) -> pd.DataFrame:
    result = universe.copy()
    result["symbol"] = result.symbol.astype(str).str.zfill(6)
    result["board"] = result.symbol.map(infer_board)
    result = result[result.board.isin(allowed_boards)]
    if exclude_st:
        result = result[~result.name.astype(str).str.upper().str.contains("ST", na=False)]
    # Eliminate placeholders/suspended rows for which a temporary daily bar cannot be built.
    result["last_price"] = pd.to_numeric(result.last_price, errors="coerce")
    return result[result.last_price.gt(0)]


def _cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol}.csv"


def _tencent_symbol(symbol: str) -> str:
    """Tencent kline API wants exchange-prefixed codes (sh/sz/bj)."""
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    return "bj" + symbol


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _download_history(symbol: str, start: date) -> pd.DataFrame:
    """Daily forward-adjusted bars from Tencent (single request, qfq built in).

    Eastmoney can throttle or blacklist an IP mid-run; Tencent's fqkline
    endpoint is fast and returns the same shape (date, open, close, high, low,
    volume -- note the bar order differs from standard OHLC).
    """
    import requests

    prefixed = _tencent_symbol(symbol)
    url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{prefixed},day,{start.strftime('%Y-%m-%d')},{date.today().strftime('%Y-%m-%d')},320,qfq",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json().get("data", {}).get(prefixed, {})
    bars = payload.get("qfqday") or payload.get("day") or []
    if not bars:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    # Tencent bar order: date, open, close, high, low, volume.  Some symbols
    # carry a 7th amount field; keep only the six standard columns.
    frame = pd.DataFrame([bar[:6] for bar in bars],
                         columns=["date", "open", "close", "high", "low", "volume"])
    frame.date = pd.to_datetime(frame.date)
    frame = frame.set_index("date").sort_index()
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna()


def history_for(symbol: str, cache_dir: Path, lookback_days: int) -> pd.DataFrame:
    """Return cached, forward-adjusted daily bars.

    Cache is intentionally refreshed if its most recent date is older than today.
    It never merges a live pre-close snapshot into the cache, preventing an
    incomplete candle from contaminating later research.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, symbol)
    today = pd.Timestamp.today().normalize()
    cached = pd.DataFrame()
    if path.exists():
        cached = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    refresh_needed = cached.empty or cached.index.max().normalize() < today
    if refresh_needed:
        # After the initial download, request only the overlap since the final
        # cached session.  This keeps daily full-market runs practical.
        start = (date.today() - timedelta(days=lookback_days * 2) if cached.empty
                 else cached.index.max().date() - timedelta(days=5))
        fresh = _download_history(symbol, start)
        # A daily endpoint can include today's unfinished bar.  Cache only known
        # historical bars; the scanner builds its one ephemeral live row below.
        fresh = fresh[fresh.index.normalize() < today]
        if not fresh.empty:
            cached = pd.concat([cached, fresh])
            cached = cached[~cached.index.duplicated(keep="last")].sort_index().tail(lookback_days)
            cached.to_csv(path, index_label="date")
    return cached.tail(lookback_days)


def append_live_bar(history: pd.DataFrame, quote: pd.Series) -> pd.DataFrame:
    """Append a temporary today bar based on the quote; do not persist it."""
    today = pd.Timestamp.today().normalize()
    if history.empty:
        return history
    price = float(quote.last_price)
    # AkShare spot fields may vary; using price as high/low is conservative if
    # intraday OHLC is unavailable, and marks the signal as proxy quality.
    def numeric(field: str, fallback: float) -> float:
        value = pd.to_numeric(quote.get(field), errors="coerce")
        return float(value) if pd.notna(value) and value > 0 else fallback
    live = pd.DataFrame({
        "open": [numeric("今开", price)], "high": [numeric("最高", price)],
        "low": [numeric("最低", price)], "close": [price],
        "volume": [numeric("成交量", 0.0)],
    }, index=pd.DatetimeIndex([today], name="date"))
    if live.volume.iloc[0] <= 0:
        # Do not manufacture a volume breakout if the live quote lacks volume.
        live.loc[:, "volume"] = history.volume.tail(20).mean()
    return pd.concat([history[history.index.normalize() < today], live]).tail(len(history) + 1)


def polite_pause() -> None:
    """Small spacing between public-data requests; tune workers conservatively."""
    time.sleep(0.03)
