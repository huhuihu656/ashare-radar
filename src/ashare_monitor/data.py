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
    """Official exchange calendar via Tushare (paid), Sina as fallback.

    A weekday is not necessarily a trading day.  The caller must skip rather
    than reuse a stale quote if this check cannot be completed.
    """
    target = pd.Timestamp(day or date.today()).normalize()
    pro = _ts_pro()
    if pro is not None:
        try:
            cal = pro.trade_cal(exchange="SSE", start_date=target.strftime("%Y%m%d"),
                                end_date=target.strftime("%Y%m%d"), is_open="1")
            if cal is not None and not cal.empty:
                return True
            cal = pro.trade_cal(exchange="SSE", start_date=target.strftime("%Y%m%d"),
                                end_date=target.strftime("%Y%m%d"), is_open="0")
            return cal is not None and cal.empty
        except Exception:
            pass
    import akshare as ak
    calendar = ak.tool_trade_date_hist_sina()
    if "trade_date" not in calendar.columns:
        raise RuntimeError("交易日历字段变化：缺少 trade_date")
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


_dl_lock = __import__("threading").Lock()
_dl_next = 0.0


def _throttle_download() -> None:
    """Space out quote-server requests so the shared IP is not throttled."""
    global _dl_next
    with _dl_lock:
        now = time.monotonic()
        wait = _dl_next - now
        if wait > 0:
            time.sleep(wait)
        _dl_next = time.monotonic() + 0.12


def _tencent_symbol(symbol: str) -> str:
    """Tencent kline API wants exchange-prefixed codes (sh/sz/bj)."""
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    return "bj" + symbol


def _standardize(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize a raw OHLCV frame into the cache contract (date-indexed)."""
    frame = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    frame.date = pd.to_datetime(frame.date)
    frame = frame.set_index("date").sort_index()
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def _download_history(symbol: str, start: date) -> pd.DataFrame:
    """Daily forward-adjusted bars, Tencent first, Sina daily as fallback.

    Free quote servers (Eastmoney, Tencent) throttle or blacklist an IP under
    full-market load; Sina is a different host family and returns the full
    history in one request.  All payloads are public market data.
    """
    import requests

    _throttle_download()
    prefixed = _tencent_symbol(symbol)
    params = {
        "param": f"{prefixed},day,{start.strftime('%Y-%m-%d')},{date.today().strftime('%Y-%m-%d')},320,qfq",
    }
    tencent_error = None
    for host in ("https://ifzq.gtimg.cn", "http://web.ifzq.gtimg.cn"):
        try:
            response = requests.get(host + "/appstock/app/fqkline/get", params=params, timeout=15)
            response.raise_for_status()
            payload = response.json().get("data", {}).get(prefixed, {})
            bars = payload.get("qfqday") or payload.get("day") or []
            if not bars:
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            # Tencent bar order: date, open, close, high, low, volume.  Some
            # symbols carry a 7th amount field; keep the six standard columns.
            frame = pd.DataFrame([bar[:6] for bar in bars],
                                 columns=["date", "open", "close", "high", "low", "volume"])
            return _standardize(frame)
        except Exception as error:
            tencent_error = error
    # Tencent hosts unavailable -> Sina daily (full history, forward-adjusted).
    import akshare as ak

    try:
        raw = ak.stock_zh_a_daily(symbol=prefixed, adjust="qfq")
        return _standardize(raw)
    except Exception as error:
        raise RuntimeError(f"{symbol} 行情源全部失败（腾讯: {tencent_error}；新浪: {error}）")


def refresh_history_cache_bulk(cache_dir: Path, lookback_days: int = 320, force: bool = False) -> tuple[int, int]:
    """Refresh the whole price cache from Tushare, one session per call pair.

    Paid-source bulk refresh: each `daily` + `adj_factor` call returns the whole
    market for one session (~0.5s together), so even a full 320-session rebuild
    takes a few minutes instead of per-stock downloads.  Forward adjustment is
    computed manually -- close * factor / factor(latest for the symbol) -- which
    we verified equals pro_bar(adj="qfq") exactly (diff 0.0000).  After the
    first rebuild, routine refreshes only fetch sessions newer than the newest
    cached bar, so daily runs are nearly instant.

    Cache format stays the existing one: CSV with a `date` column, then
    open/high/low/close/volume.  Returns (symbols_written, sessions_fetched).
    """
    pro = _ts_pro()
    if pro is None:
        return 0, 0
    cache_dir.mkdir(parents=True, exist_ok=True)

    cal = pro.trade_cal(
        exchange="SSE",
        start_date=(date.today() - timedelta(days=lookback_days * 2 + 30)).strftime("%Y%m%d"),
        end_date=date.today().strftime("%Y%m%d"),
        is_open="1",
    )
    if cal is None or cal.empty:
        return 0, 0
    all_sessions = sorted(cal["cal_date"].astype(str).tolist())

    if force or not any(cache_dir.iterdir()):
        needed = all_sessions
    else:
        newest = date(2000, 1, 1)
        try:
            sample = next(cache_dir.glob("*.csv"))
            head = pd.read_csv(sample, parse_dates=["date"], nrows=5)
            if not head.empty:
                newest = max(head.date.max().date(), newest)
        except Exception:
            newest = date(2000, 1, 1)
        needed = [s for s in all_sessions if s > newest.strftime("%Y%m%d")]
        needed = sorted(set(needed + all_sessions[-3:]))
    if not needed:
        return 0, 0

    frames: list[pd.DataFrame] = []
    for session in needed:
        try:
            daily = pro.daily(trade_date=session)
            factors = pro.adj_factor(trade_date=session)
        except Exception:
            continue
        if daily is None or daily.empty or factors is None or factors.empty:
            continue
        merged = daily.merge(factors[["ts_code", "adj_factor"]], on="ts_code", how="inner")
        merged["trade_date"] = session
        frames.append(merged[["ts_code", "trade_date", "adj_factor", "open", "high", "low", "close", "vol"]])
    if not frames:
        return 0, 0
    all_raw = pd.concat(frames, ignore_index=True)
    all_raw["symbol"] = all_raw["ts_code"].astype(str).str[:6]
    latest_factor = all_raw.sort_values(["symbol", "trade_date"]).groupby("symbol")["adj_factor"].last()

    written = 0
    for symbol, group in all_raw.groupby("symbol"):
        base = float(latest_factor[symbol])
        if base <= 0:
            continue
        bars = pd.DataFrame({
            "date": pd.to_datetime(group["trade_date"]),
            "open": pd.to_numeric(group["open"], errors="coerce"),
            "high": pd.to_numeric(group["high"], errors="coerce"),
            "low": pd.to_numeric(group["low"], errors="coerce"),
            "close": pd.to_numeric(group["close"], errors="coerce") * group["adj_factor"] / base,
            "volume": pd.to_numeric(group["vol"], errors="coerce"),
        }).dropna()
        if bars.empty:
            continue
        path = cache_dir / f"{symbol}.csv"
        if path.exists():
            try:
                existing = pd.read_csv(path, parse_dates=["date"])
                key = existing.date.dt.strftime("%Y%m%d")
                bars_key = bars.date.dt.strftime("%Y%m%d")
                existing = existing[~key.isin(bars_key)]
                bars = pd.concat([existing, bars], ignore_index=True)
            except Exception:
                pass
        bars = bars.sort_values("date")
        bars.to_csv(path, index=False, columns=["date", "open", "high", "low", "close", "volume"])
        written += 1
    return written, len(needed)


def _ts_pro():
    """Lazy Tushare API handle; None when token missing/unusable."""
    from . import tushare_src

    if not tushare_src.available():
        return None
    return tushare_src._TS


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


def index_frame(symbol: str = "sh000001") -> pd.DataFrame:
    """Shanghai Composite daily bars for the market-regime check.

    Paid source first (Tushare index_daily); free feeds (Tencent/Sina) remain
    as fallbacks.  One request per scan; failures return an empty frame so the
    scan continues without a market tag instead of aborting.
    """
    pro = _ts_pro()
    if pro is not None:
        try:
            ts_code = {"sh000001": "000001.SH", "sz399001": "399001.SZ",
                       "sz399006": "399006.SZ"}.get(symbol, "000001.SH")
            raw = pro.index_daily(ts_code=ts_code,
                                  start_date=(date.today() - timedelta(days=200)).strftime("%Y%m%d"))
            if raw is not None and not raw.empty:
                frame = raw.rename(columns={"trade_date": "date", "vol": "volume"})
                frame.date = pd.to_datetime(frame.date)
                frame = frame.set_index("date").sort_index()
                for column in ["open", "high", "low", "close", "volume"]:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                return frame.dropna().tail(200)
        except Exception:
            pass
    import requests

    try:
        params = {"param": f"{symbol},day,{date.today() - timedelta(days=180):%Y-%m-%d},{date.today():%Y-%m-%d},150,qfq"}
        for host in ("https://ifzq.gtimg.cn", "http://web.ifzq.gtimg.cn"):
            try:
                response = requests.get(host + "/appstock/app/fqkline/get", params=params, timeout=15)
                response.raise_for_status()
                payload = response.json().get("data", {}).get(symbol, {})
                bars = payload.get("qfqday") or payload.get("day") or []
                if bars:
                    frame = pd.DataFrame([bar[:6] for bar in bars],
                                         columns=["date", "open", "close", "high", "low", "volume"])
                    frame.date = pd.to_datetime(frame.date)
                    frame = frame.set_index("date").sort_index()
                    for column in ["open", "high", "low", "close", "volume"]:
                        frame[column] = pd.to_numeric(frame[column], errors="coerce")
                    return frame.dropna()
            except Exception:
                continue
    except Exception:
        pass
    try:
        import akshare as ak

        raw = ak.stock_zh_index_daily(symbol=symbol)
        frame = raw.rename(columns={
            "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume"})
        frame.date = pd.to_datetime(frame.date)
        frame = frame.set_index("date").sort_index()
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna().tail(200)
    except Exception:
        return pd.DataFrame()


def market_regime(frame: pd.DataFrame, ma_days: int = 60) -> dict:
    """Classify the broad market as 偏强/中性/偏弱 from the index frame.

    偏弱 = 收盘价与20日线均在60日线下方（单边下跌），此时个股形态成功率
    普遍降低；用于审计记录与可选的信号过滤。
    """
    if frame.empty or len(frame) < ma_days + 20:
        return {"state": "未知", "close": None, "ma20": None, "ma60": None}
    close = frame.close.astype(float)
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(ma_days).mean().iloc[-1])
    price = float(close.iloc[-1])
    if price < ma20 < ma60:
        state = "偏弱"
    elif price > ma20 > ma60:
        state = "偏强"
    else:
        state = "中性"
    return {"state": state, "close": round(price, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2)}


def polite_pause() -> None:
    """Small spacing between public-data requests; tune workers conservatively."""
    time.sleep(0.03)
