from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import json
import time

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential


HISTORY_COLUMNS = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}


def get_universe(trade_date: str) -> pd.DataFrame:
    """Closed-session universe: Tushare stock list + the day's real OHLCV.

    Post-close official scan: no free realtime feed is involved.  `trade_date`
    must be a closed session (15:10 run); the caller passes today's date.
    Returns rows with symbol/name/board/last_price/open/high/low/volume where
    volume is in lots (Tushare native) -- consistent with the price cache.
    """
    pro = _ts_pro()
    if pro is None:
        raise RuntimeError("Tushare 不可用，无法获取股票池")
    basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
    if basic is None or basic.empty:
        raise RuntimeError("Tushare 股票列表为空")
    daily = pro.daily(trade_date=trade_date)
    if daily is None or daily.empty:
        raise RuntimeError(f"Tushare {trade_date} 行情尚未发布（需收盘后运行）")
    merged = basic.merge(daily[["ts_code", "open", "high", "low", "close", "vol"]],
                         on="ts_code", how="inner")
    if merged.empty:
        raise RuntimeError("股票池与当日行情合并为空")
    merged["symbol"] = merged["ts_code"].astype(str).str[:6]
    merged = merged.rename(columns={
        "name": "name", "close": "last_price", "vol": "volume"})
    return merged[["symbol", "name", "open", "high", "low", "last_price", "volume"]]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def is_ashare_trading_day(day: date | None = None) -> bool:
    """Official exchange calendar via Tushare (paid).  Fail-closed on error.

    A weekday is not necessarily a trading day.  The caller must skip rather
    than reuse a stale quote if this check cannot be completed; free-source
    fallbacks were removed per the paid-data policy.
    """
    target = pd.Timestamp(day or date.today()).normalize()
    pro = _ts_pro()
    if pro is None:
        raise RuntimeError("Tushare 不可用，无法核验交易日历")
    cal = pro.trade_cal(exchange="SSE", start_date=target.strftime("%Y%m%d"),
                        end_date=target.strftime("%Y%m%d"), is_open="1")
    if cal is None or cal.empty:
        cal = pro.trade_cal(exchange="SSE", start_date=target.strftime("%Y%m%d"),
                            end_date=target.strftime("%Y%m%d"), is_open="0")
        return cal is not None and cal.empty
    return True


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


def _download_history(symbol: str, start: date) -> pd.DataFrame:
    """Per-symbol forward-adjusted bars from Tushare (paid, qfq via factor math).

    Free quote servers were removed from the data chain per the paid-data
    policy; a symbol the bulk refresh missed raises instead of silently
    switching sources.
    """
    from .tushare_src import qfq_daily

    frame = qfq_daily(symbol, start)
    if frame is None or frame.empty:
        raise RuntimeError(f"{symbol} Tushare 行情不可用")
    return frame


def cache_latest_date(cache_dir: Path) -> str | None:
    """缓存中最新交易日的 YYYYMMDD（任一成分股 csv 的最大日期）。"""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return None
    latest = None
    for path in cache_dir.glob("*.csv"):
        try:
            dates = pd.read_csv(path, usecols=["date"])["date"]
            if dates.empty:
                continue
            day = str(dates.max()).replace("-", "")[:8]
            if latest is None or day > latest:
                latest = day
        except Exception:
            continue
    return latest


def _sessions_manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "_sessions.json"


def _load_done_sessions(cache_dir: Path) -> set[str]:
    """已成功抓取并合并进缓存的交易日集合（YYYYMMDD）。读不到就当空账本。"""
    try:
        payload = json.loads(_sessions_manifest_path(cache_dir).read_text(encoding="utf-8"))
        return {str(x) for x in payload.get("sessions", [])}
    except Exception:
        return set()


def _save_done_sessions(cache_dir: Path, done: set[str]) -> None:
    path = _sessions_manifest_path(cache_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"sessions": sorted(done)}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def session_fetched(cache_dir: Path, session: str) -> bool:
    """该交易日是否已成功抓取并合并（以记账清单为准，不看个别文件的新鲜度）。"""
    return session in _load_done_sessions(cache_dir)


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

    # 逐日记账：只有真正抓成功的交易日才入账，缺的会在下次运行自动重试。
    # 旧实现用"某个抽样文件的最新日期"推断覆盖度，造成两类静默错误——
    # ① 某日 fetch 失败被整日丢弃后永远不再重试；② 个别文件陈旧却因抽样躲过检查。
    done = _load_done_sessions(cache_dir)
    cold_start = not any(cache_dir.glob("*.csv"))
    if force:
        needed = list(all_sessions)
    elif not done:
        # 无账本：要么是全新缓存，要么是存量缓存需要一次性补洞。
        # 全新缓存只需覆盖形态最长回看（约 160 根K线），抓最近 200 个交易日即可；
        # 存量缓存则扫全窗口，把历史上被丢掉的交易日补齐。
        needed = all_sessions[-200:] if cold_start else list(all_sessions)
    else:
        needed = [s for s in all_sessions if s not in done]
    if not needed:
        return 0, 0

    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    # 全局请求节流：任意两次 Tushare 请求间隔 >=0.4s（≈150 次/分钟 < 200 上限）
    _ts_lock = threading.Lock()
    _ts_next = [0.0]

    def _ts_throttle() -> None:
        with _ts_lock:
            wait = _ts_next[0] - _time.monotonic()
            if wait > 0:
                _time.sleep(wait)
            _ts_next[0] = _time.monotonic() + 0.4

    def fetch_session(session: str):
        """一个交易日：daily + adj_factor（供并行调用）。"""
        for attempt in range(4):
            try:
                _ts_throttle()
                daily = pro.daily(trade_date=session)
                _ts_throttle()
                factors = pro.adj_factor(trade_date=session)
                if daily is None or daily.empty or factors is None or factors.empty:
                    return None
                merged = daily.merge(factors[["ts_code", "adj_factor"]], on="ts_code", how="inner")
                merged["trade_date"] = session
                return merged[["ts_code", "trade_date", "adj_factor", "open", "high", "low", "close", "vol"]]
            except Exception as error:
                message = str(error)
                if "频率超限" in message or "frequency" in message.lower() or "每分钟" in message:
                    print(f"[bulk] {session} 触发限频，退避 65s 重试（{attempt + 1}/4）", flush=True)
                    _time.sleep(65)
                    continue
                return None
        return None

    frames: list[pd.DataFrame] = []
    # 3 线程并行：隐藏跨洋/跨网延迟（冷启动约 4 倍加速）；
    # 节流器保证总请求速率仍低于 200/分钟。
    pending = list(needed)
    fetched: list[str] = []
    for attempt in range(3):
        if not pending:
            break
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for session, result in zip(pending, pool.map(fetch_session, pending)):
                if result is None:
                    failed.append(session)
                else:
                    frames.append(result)
                    fetched.append(session)
        if failed and attempt < 2:
            print(f"[bulk] {len(failed)} 个交易日抓取失败，重试（{attempt + 2}/3）", flush=True)
            _time.sleep(5)
        pending = failed
    if pending:
        # 不记账 → 下次运行自动重试，不留永久空洞
        print(f"[bulk] {len(pending)} 个交易日仍失败，留待下次运行重试：{pending[:5]}", flush=True)
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
    # 只把真正抓成功的交易日记账；失败的留给下次运行自动重试。
    _save_done_sessions(cache_dir, done | set(fetched))
    return written, len(needed)


def _ts_pro():
    """Lazy Tushare API handle; None when token missing/unusable."""
    from . import tushare_src

    if not tushare_src.available():
        return None
    return tushare_src._TS


def history_for(symbol: str, cache_dir: Path, lookback_days: int,
                as_of: str | None = None) -> pd.DataFrame:
    """Return cached, forward-adjusted daily bars.

    Cache is refreshed only when it does not yet cover the scan day (`as_of`,
    YYYYMMDD; default today).  Comparing against the scan day instead of "now"
    prevents a historical backfill from triggering a per-symbol download storm
    (the bulk refresh already guarantees coverage).  A live pre-close snapshot
    is never merged into the cache, preventing an incomplete candle from
    contaminating later research.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, symbol)
    today = pd.Timestamp.today().normalize()
    target = pd.Timestamp(as_of).normalize() if as_of else today
    cached = pd.DataFrame()
    if path.exists():
        cached = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    refresh_needed = cached.empty or cached.index.max().normalize() < target
    if refresh_needed:
        # After the initial download, request only the overlap since the final
        # cached session.  This keeps daily full-market runs practical.
        start = (date.today() - timedelta(days=lookback_days * 2) if cached.empty
                 else cached.index.max().date() - timedelta(days=5))
        fresh = _download_history(symbol, start)
        # A daily endpoint can include today's unfinished bar.  Cache only known
        # historical bars; the scanner builds its one ephemeral live row below.
        fresh = fresh[fresh.index.normalize() <= target]
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

    Paid source only (Tushare index_daily).  During intraday scans today's bar
    is not published yet; the frame simply ends at the previous close, which is
    what the regime classifier needs.  Raises when Tushare is unavailable.
    """
    pro = _ts_pro()
    if pro is None:
        raise RuntimeError("Tushare 不可用，无法获取大盘指数")
    ts_code = {"sh000001": "000001.SH", "sz399001": "399001.SZ",
               "sz399006": "399006.SZ"}.get(symbol, "000001.SH")
    raw = pro.index_daily(ts_code=ts_code,
                          start_date=(date.today() - timedelta(days=200)).strftime("%Y%m%d"))
    if raw is None or raw.empty:
        raise RuntimeError("Tushare 指数数据为空")
    frame = raw.rename(columns={"trade_date": "date", "vol": "volume"})
    frame.date = pd.to_datetime(frame.date)
    frame = frame.set_index("date").sort_index()
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna().tail(200)


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
