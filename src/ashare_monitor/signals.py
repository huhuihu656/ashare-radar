from __future__ import annotations

import numpy as np
import pandas as pd

from .config import BreakoutConfig, SupportConfig


def _base_row(frame: pd.DataFrame) -> dict[str, float]:
    last = frame.iloc[-1]
    return {"close": float(last.close), "volume": float(last.volume)}


def support_retest(frame: pd.DataFrame, cfg: SupportConfig) -> dict | None:
    """Find a completed prior rally whose low is being retested in a downtrend.

    Only rows up to the scan timestamp are used.  The lowest point is deliberately
    selected before its subsequent rally window, avoiding future-data leakage.
    """
    need = cfg.ma_slow + cfg.rally_max_days + 5
    if len(frame) < need:
        return None
    close = frame.close.astype(float).reset_index(drop=True)
    ma_fast = close.rolling(cfg.ma_fast).mean()
    ma_slow = close.rolling(cfg.ma_slow).mean()
    current = len(close) - 1
    candidates: list[tuple[int, int, float]] = []
    # Scan historical lows only; rally must have ended before the current decline.
    for start in range(cfg.ma_slow, current - cfg.rally_min_days):
        end = min(start + cfg.rally_max_days, current - 5)
        if end - start < cfg.rally_min_days:
            continue
        low = close.iloc[start]
        if low != close.iloc[max(0, start - 4) : start + 5].min():
            continue
        future = close.iloc[start + cfg.rally_min_days : end + 1]
        peak_pos = int(future.idxmax())
        rally = close.iloc[peak_pos] / low - 1
        if rally >= cfg.min_prior_rally_pct:
            candidates.append((start, peak_pos, float(rally)))
    if not candidates:
        return None
    start, peak, rally = candidates[-1]
    start_price = float(close.iloc[start])
    price = float(close.iloc[-1])
    distance = price / start_price - 1
    downtrend = price < ma_fast.iloc[-1] < ma_slow.iloc[-1] and ma_fast.iloc[-1] < ma_fast.iloc[-6]
    valid_band = -cfg.invalidation_below_pct <= distance <= cfg.approach_band_above_pct
    if not (downtrend and valid_band):
        return None
    # Higher score means closer support, stronger original rally, without claiming prediction.
    proximity = max(0.0, 1 - max(distance, 0) / cfg.approach_band_above_pct)
    score = round(100 * (0.65 * proximity + 0.35 * min(rally / 0.5, 1)), 1)
    return {
        **_base_row(frame), "signal": "回踩前期起涨位", "score": score,
        "start_date": str(frame.index[start].date()), "start_price": round(start_price, 3),
        "distance_to_start_pct": round(distance * 100, 2), "prior_rally_pct": round(rally * 100, 2),
        "ma20": round(float(ma_fast.iloc[-1]), 3), "ma60": round(float(ma_slow.iloc[-1]), 3),
        "note": "下跌结构中接近前期起涨低点；跌破失效位应重新评估",
    }


def sideways_breakout(frame: pd.DataFrame, cfg: BreakoutConfig) -> dict | None:
    """Volume-and-price proxy for a potential accumulation breakout."""
    required = max(cfg.consolidation_days + cfg.volume_baseline_days + 1, 50)
    if len(frame) < required:
        return None
    prior = frame.iloc[-(cfg.consolidation_days + 1) : -1].copy()
    today = frame.iloc[-1]
    range_pct = float(prior.high.max() / prior.low.min() - 1)
    tr = pd.concat([
        frame.high - frame.low,
        (frame.high - frame.close.shift()).abs(),
        (frame.low - frame.close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_pct = float(tr.iloc[-cfg.consolidation_days - 1 : -1].mean() / prior.close.mean())
    volume_base = float(frame.volume.iloc[-cfg.volume_baseline_days - 1 : -1].mean())
    volume_ratio = float(today.volume / volume_base) if volume_base else 0.0
    breakout_high = float(prior.high.iloc[-cfg.breakout_lookback_days :].max())
    daily_range = max(float(today.high - today.low), 1e-9)
    close_position = float((today.close - today.low) / daily_range)
    # Permit a close just below the prior high to catch 14:40 pre-close candidates.
    near_or_above_high = float(today.close) >= breakout_high * 0.985
    if not (range_pct <= cfg.max_range_pct and atr_pct <= cfg.max_atr_pct and
            volume_ratio >= cfg.min_volume_ratio and close_position >= cfg.min_close_position and near_or_above_high):
        return None
    score = round(100 * min(1, 0.45 * volume_ratio / cfg.min_volume_ratio + 0.35 * close_position + 0.20 * (1 - range_pct / cfg.max_range_pct)), 1)
    return {
        **_base_row(frame), "signal": "横盘后放量突破", "score": score,
        "range_pct": round(range_pct * 100, 2), "atr_pct": round(atr_pct * 100, 2),
        "volume_ratio": round(volume_ratio, 2), "breakout_high": round(breakout_high, 3),
        "close_position": round(close_position, 2),
        "note": "量价代理显示放量接近/突破区间上沿；需结合公告与真实资金流核验",
    }


def scan_frame(frame: pd.DataFrame, support_cfg: SupportConfig, breakout_cfg: BreakoutConfig) -> list[dict]:
    clean = frame.copy().sort_index()
    clean = clean[~clean.index.duplicated(keep="last")]
    clean = clean.dropna(subset=["open", "high", "low", "close", "volume"])
    out: list[dict] = []
    if support_cfg.enabled:
        row = support_retest(clean, support_cfg)
        if row:
            out.append(row)
    if breakout_cfg.enabled:
        row = sideways_breakout(clean, breakout_cfg)
        if row:
            out.append(row)
    return out
