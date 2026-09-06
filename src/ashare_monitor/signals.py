from __future__ import annotations

import numpy as np
import pandas as pd

from .config import (
    BoxBreakoutConfig,
    BreakMa20Config,
    BreakoutConfig,
    DragonConfig,
    EngulfingConfig,
    LimitUpGapConfig,
    MaDivergenceConfig,
    OversoldReversalConfig,
    RiskConfig,
    ShadowTestConfig,
    SupportConfig,
)


def _base_row(frame: pd.DataFrame) -> dict[str, float]:
    last = frame.iloc[-1]
    return {"close": float(last.close), "volume": float(last.volume)}


# ---------------------------------------------------------------------------
# Shared risk guards (位置 / 量能 / 趋势初期)
# ---------------------------------------------------------------------------

def position_ok(frame: pd.DataFrame, risk: RiskConfig) -> bool:
    """6个月内（约120个交易日）涨幅不得超过上限；高位形态胜率骤降。"""
    if not risk.enabled:
        return True
    look = min(risk.position_lookback_days, len(frame) - 2)
    if look < 30:
        return True
    base = float(frame.close.iloc[-look - 1])
    if base <= 0:
        return False
    return float(frame.close.iloc[-1]) / base - 1 <= risk.max_position_gain_pct


def low_zone_ok(frame: pd.DataFrame, risk: RiskConfig) -> bool:
    """价格处于观察窗口的相对低位（位置分位 <= low_zone_pct）。"""
    window = frame.iloc[-(risk.position_lookback_days + 1):]
    lo = float(window.low.min())
    hi = float(window.high.max())
    if hi <= lo:
        return False
    return (float(frame.close.iloc[-1]) - lo) / (hi - lo) <= risk.low_zone_pct


def volume_valid(today_volume: float, frame: pd.DataFrame, risk: RiskConfig) -> bool:
    """通用量能门槛：当日量比 >= 1.5 且不低于前5日均量的1.5倍之一。"""
    if not risk.enabled:
        return True
    base = float(frame.volume.iloc[-21:-1].mean()) if len(frame) >= 21 else float(frame.volume.iloc[:-1].mean())
    if base <= 0:
        return False
    return today_volume / base >= risk.min_volume_ratio


def _vol_ma(frame: pd.DataFrame, days: int) -> float:
    return float(frame.volume.iloc[-days:].mean())


def _gain_over(frame: pd.DataFrame, days: int) -> float:
    """Close change over the last `days` bars (before today)."""
    if len(frame) < days + 2:
        return 0.0
    base = float(frame.close.iloc[-days - 2])
    prev = float(frame.close.iloc[-2])
    return prev / base - 1 if base > 0 else 0.0


# ---------------------------------------------------------------------------
# 一、启动信号类
# ---------------------------------------------------------------------------

def box_breakout_bullish(frame: pd.DataFrame, cfg: BoxBreakoutConfig, risk: RiskConfig) -> dict | None:
    """箱体突破 + 红肥绿瘦：低位箱体末端收敛，放量大阳线刺穿箱体上沿。

    主力吸筹完成的量价结构：箱体内阳线放量（红肥）、阴线缩量（绿瘦），
    突破 K 线需放量至 5 日均量 2 倍以上。
    """
    need = cfg.box_days + 25
    if len(frame) < need:
        return None
    box = frame.iloc[-(cfg.box_days + 1):-1]
    box_high = float(box.high.max())
    box_low = float(box.low.min())
    range_pct = box_high / box_low - 1 if box_low > 0 else 1e9
    if not (0.03 <= range_pct <= cfg.box_max_range_pct):
        return None
    # 箱体末端波动收敛
    tail = box.iloc[-cfg.converge_days:]
    tail_range = float(tail.high.max() / tail.low.min() - 1) if tail.low.min() > 0 else 1e9
    converge = tail_range / range_pct if range_pct > 0 else 1e9
    if converge > cfg.converge_ratio:
        return None
    # 红肥绿瘦：箱体内阳线均量明显大于阴线均量
    bulls = box[box.close > box.open]
    bears = box[box.close < box.open]
    if len(bulls) < 5 or len(bears) < 3:
        return None
    red_green = float(bulls.volume.mean() / bears.volume.mean()) if bears.volume.mean() > 0 else 0.0
    if red_green < cfg.red_green_vol_ratio:
        return None
    # 突破当日：放量大阳线刺穿箱体上沿，且处于相对低位
    today = frame.iloc[-1]
    if not (float(today.close) > box_high and float(today.close) > float(today.open)):
        return None
    prior_vol_ma = float(frame.volume.iloc[-cfg.vol_ma_days - 1:-1].mean())
    vol_ratio = float(today.volume) / prior_vol_ma if prior_vol_ma > 0 else 0.0
    if vol_ratio < cfg.min_breakout_vol_ratio:
        return None
    if not (position_ok(frame, risk) and low_zone_ok(frame, risk)):
        return None
    score = round(100 * min(1, 0.40 * vol_ratio / cfg.min_breakout_vol_ratio + 0.25 * (1 - converge) +
                            0.20 * red_green / cfg.red_green_vol_ratio + 0.15 * (1 - range_pct / cfg.box_max_range_pct)), 1)
    return {
        **_base_row(frame), "signal": "箱体突破红肥绿瘦", "score": score,
        "box_high": round(box_high, 3), "box_low": round(box_low, 3), "range_pct": round(range_pct * 100, 2),
        "converge_ratio": round(converge, 2), "red_green_vol_ratio": round(red_green, 2),
        "volume_ratio": round(vol_ratio, 2),
        "note": "低位箱体末端收敛后放量突破上沿；突破后需回踩不破箱体上沿确认",
    }


def bullish_engulfing(frame: pd.DataFrame, cfg: EngulfingConfig, risk: RiskConfig) -> dict | None:
    """阳包阴反包启动：上升初期缩量阴线后，次日放量阳线突破阴线最高点。

    洗盘结束信号：回调幅度不得超过前两日涨幅的30%，反包阳线须放量。
    """
    if len(frame) < 12:
        return None
    prev = frame.iloc[-2]
    today = frame.iloc[-1]
    if not (prev.close < prev.open and today.close > today.open):
        return None
    # 前两日涨幅金额与相对涨幅（阴线前的两日累计）
    prior_top = float(frame.close.iloc[-3])
    gain_amount = prior_top - float(frame.close.iloc[-5])
    if gain_amount <= 0:
        return None
    prior_gain = gain_amount / float(frame.close.iloc[-5])
    # 阴线回调金额 / 前两日涨幅金额
    pullback = (prior_top - float(prev.close)) / gain_amount
    if pullback > cfg.max_pullback_ratio:
        return None
    # 阴线缩量（明显小于近5日均量）
    if float(prev.volume) >= _vol_ma(frame.iloc[:-1], cfg.vol_ma_days) * cfg.pullback_vol_ratio:
        return None
    # 反包阳线放量突破阴线最高点（而非仅覆盖实体）
    if float(today.close) < float(prev.high):
        return None
    engulf_vol_ratio = float(today.volume / prev.volume) if prev.volume > 0 else 0.0
    if engulf_vol_ratio < cfg.engulf_min_vol_ratio or not volume_valid(float(today.volume), frame, risk):
        return None
    # 位置：上升趋势初期（60日涨幅受限）且不在高位
    if _gain_over(frame, 60) > risk.early_trend_max_gain_pct or not position_ok(frame, risk):
        return None
    score = round(100 * min(1, 0.40 * min(engulf_vol_ratio / 2.0, 1) + 0.30 * (1 - pullback / cfg.max_pullback_ratio) +
                            0.30 * min(prior_gain / 0.15, 1)), 1)
    return {
        **_base_row(frame), "signal": "阳包阴反包启动", "score": score,
        "pullback_ratio": round(pullback, 2), "prior2_gain_pct": round(prior_gain * 100, 2),
        "yin_high": round(float(prev.high), 3), "yin_low": round(float(prev.low), 3),
        "engulf_vol_ratio": round(engulf_vol_ratio, 2), "volume_ratio": round(volume_valid(float(today.volume), frame, risk) * 1, 2),
        "note": "上升初期缩量洗盘后放量反包并突破阴线高点；需维持放量确认",
    }


def limitup_gap(frame: pd.DataFrame, cfg: LimitUpGapConfig, risk: RiskConfig, limit_pct: float = 0.10) -> dict | None:
    """涨停 + 跳空缺口共振：20日内涨停且伴随向上实体缺口，3日未回补。

    涨停需为首次突破关键压力位（非跟风），封单金额免费行情源无法核验，
    以「封板收盘 + 突破前60日高点」近似。
    """
    need = cfg.pressure_lookback_days + cfg.lookback_days + 5
    if len(frame) < need:
        return None
    closes = frame.close.astype(float)
    opens = frame.open.astype(float)
    highs = frame.high.astype(float)
    lows = frame.low.astype(float)
    vols = frame.volume.astype(float)
    limit_price = lambda prev_close: round(prev_close * (1 + limit_pct), 2)
    found = None
    for i in range(len(frame) - cfg.gap_hold_days - 1, max(len(frame) - cfg.lookback_days - 1, 1), -1):
        prev_close = float(closes.iloc[i - 1])
        is_limit = float(closes.iloc[i]) >= limit_price(prev_close) - cfg.limit_tolerance_pct and float(closes.iloc[i]) == float(highs.iloc[i])
        gap = float(opens.iloc[i]) > float(highs.iloc[i - 1])
        if not (is_limit and gap):
            continue
        # 首次突破关键压力位：涨停日收盘高于此前60日最高收盘（不含当日）
        pressure = float(closes.iloc[max(0, i - cfg.pressure_lookback_days):i].max())
        if float(closes.iloc[i]) <= pressure:
            continue
        # 缺口3日内未回补
        gap_bottom = float(highs.iloc[i - 1])
        hold = i + cfg.gap_hold_days < len(frame) and float(lows.iloc[i + 1:i + 1 + cfg.gap_hold_days].min()) > gap_bottom
        if not hold:
            continue
        found = (i, gap_bottom, float(closes.iloc[i]), float(vols.iloc[i]))
        break
    if found is None:
        return None
    i, gap_bottom, limit_close, limit_vol = found
    # 回调缩量至涨停日50%以下（涨停后至当前的最小量）
    after_vol = float(vols.iloc[i + 1:].min())
    pullback_vol_ratio = after_vol / limit_vol if limit_vol > 0 else 1e9
    if pullback_vol_ratio > cfg.pullback_vol_ratio:
        return None
    if not position_ok(frame, risk):
        return None
    gap_pct = (float(opens.iloc[i]) / float(highs.iloc[i - 1]) - 1) * 100
    days_since = len(frame) - 1 - i
    score = round(100 * min(1, 0.45 * (1 - pullback_vol_ratio / cfg.pullback_vol_ratio) +
                            0.30 * min(gap_pct / 3.0, 1) + 0.25 * min(10 / max(days_since, 1), 1)), 1)
    return {
        **_base_row(frame), "signal": "涨停跳空缺口共振", "score": score,
        "limit_date": str(frame.index[i].date()), "gap_size_pct": round(gap_pct, 2),
        "gap_bottom": round(gap_bottom, 3), "limit_close": round(limit_close, 3),
        "days_since_limit": int(days_since), "pullback_vol_ratio": round(pullback_vol_ratio, 2),
        "note": "涨停突破压力位+实体缺口3日未回补，回调缩量；封单金额与主力性质需另行核验",
    }


# ---------------------------------------------------------------------------
# 二、趋势延续类
# ---------------------------------------------------------------------------

def dragon_pullback(frame: pd.DataFrame, cfg: DragonConfig, risk: RiskConfig) -> dict | None:
    """龙回头二次启动：强势首波后缩量回调至20/30日线附近，二次放量突破前高。"""
    need = cfg.wave_lookback_days + 30
    if len(frame) < need:
        return None
    window = frame.iloc[-(cfg.wave_lookback_days + 1):-1]  # 首波与回调区间（不含今日）
    highs = window.high.astype(float)
    peak_pos = int(np.argmax(highs.values))  # positional index into window
    peak = float(highs.iloc[peak_pos])
    pre_peak = window.iloc[: peak_pos + 1]
    if len(pre_peak) < 5:
        return None
    start_pos = int(np.argmin(pre_peak.low.astype(float).values))
    start_low = float(pre_peak.low.iloc[start_pos])
    wave_gain = peak / start_low - 1 if start_low > 0 else 0.0
    if wave_gain < cfg.min_first_wave_pct:
        return None
    post_peak = window.iloc[peak_pos + 1:]
    if len(post_peak) < 3:
        return None
    pullback_low = float(post_peak.low.min())
    pullback_pct = (peak - pullback_low) / peak if peak > 0 else 1e9
    if pullback_pct > cfg.max_pullback_pct:
        return None
    # 回调缩量至首波均量30%以下
    wave_vol = float(window.iloc[start_pos : peak_pos + 1].volume.mean())
    pullback_vol = float(post_peak.volume.mean())
    if wave_vol <= 0 or pullback_vol / wave_vol > cfg.pullback_vol_ratio:
        return None
    # 回调至20/30日线附近
    close = frame.close.astype(float)
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma30 = float(close.rolling(30).mean().iloc[-1])
    near_ma = abs(pullback_low - ma20) / ma20 <= cfg.pullback_ma_tolerance or abs(pullback_low - ma30) / ma30 <= cfg.pullback_ma_tolerance
    if not near_ma:
        return None
    # 今日二次启动：放量（量比>=2.3）并站稳前高
    today = frame.iloc[-1]
    vol_base = float(frame.volume.astype(float).rolling(cfg.vol_ma_days).mean().iloc[-2]) if len(frame) > cfg.vol_ma_days else 0.0
    vol_ratio = float(today.volume) / vol_base if vol_base > 0 else 0.0
    if vol_ratio < cfg.min_second_vol_ratio:
        return None
    if float(today.close) < peak * cfg.prior_high_tolerance:
        return None
    if not position_ok(frame, risk):
        return None
    score = round(100 * min(1, 0.40 * min(vol_ratio / 3.0, 1) + 0.25 * min(wave_gain / 0.5, 1) +
                            0.20 * (1 - pullback_pct / cfg.max_pullback_pct) + 0.15 * (1 - pullback_vol / wave_vol)), 1)
    return {
        **_base_row(frame), "signal": "龙回头二次启动", "score": score,
        "wave_gain_pct": round(wave_gain * 100, 2), "pullback_pct": round(pullback_pct * 100, 2),
        "pullback_vol_ratio": round(pullback_vol / wave_vol, 2), "second_vol_ratio": round(vol_ratio, 2),
        "prior_high": round(peak, 3), "pullback_low": round(pullback_low, 3),
        "note": "首波后缩量回调至关键均线，二次放量站稳前高；龙头股续强概率较高",
    }


def ma_divergence_breakout(frame: pd.DataFrame, cfg: MaDivergenceConfig, risk: RiskConfig) -> dict | None:
    """均线多头发散 + 量能突破：20日线金叉60日线后多头排列，量能站上50日均量线。"""
    need = cfg.ma_slow + cfg.cross_lookback_days + 15
    if len(frame) < need:
        return None
    close = frame.close.astype(float)
    ma_fast = close.rolling(cfg.ma_fast).mean()
    ma_slow = close.rolling(cfg.ma_slow).mean()
    if not (ma_fast.iloc[-1] > ma_slow.iloc[-1]):
        return None
    # 金叉发生在最近 cross_lookback_days 内
    recent = ma_fast.iloc[-(cfg.cross_lookback_days + 1):]
    crossed = bool(((recent.iloc[:-1] <= ma_slow.iloc[-(cfg.cross_lookback_days + 1):-1]) &
                    (recent.iloc[1:].values > ma_slow.iloc[-cfg.cross_lookback_days:].values)).any())
    if not crossed:
        return None
    # 至少7日维持多头发散
    gap = (ma_fast - ma_slow).iloc[-cfg.divergence_hold_days:]
    if (gap <= 0).any() or gap.iloc[-1] <= gap.iloc[0]:
        return None
    # 回调不破20日线
    recent_lows = frame.low.astype(float).iloc[-cfg.divergence_hold_days:]
    if float(recent_lows.min()) < float(ma_fast.iloc[-1]) * (1 - cfg.pullback_ma_tolerance):
        return None
    # 量能站上50日均量线 + 突破关键压力位（前20日高点）时量比>=2
    today = frame.iloc[-1]
    vol_ma50 = float(frame.volume.rolling(cfg.vol_ma_days).mean().iloc[-1])
    if float(today.volume) < vol_ma50:
        return None
    pressure = float(frame.high.astype(float).iloc[-(cfg.breakout_lookback_days + 1):-1].max())
    if float(today.close) < pressure:
        return None
    vol_ratio = float(today.volume) / float(frame.volume.iloc[-(cfg.breakout_lookback_days + 1):-1].mean())
    if vol_ratio < cfg.min_breakout_vol_ratio:
        return None
    if not position_ok(frame, risk):
        return None
    ma_gap_pct = float((ma_fast.iloc[-1] / ma_slow.iloc[-1] - 1) * 100)
    score = round(100 * min(1, 0.40 * min(vol_ratio / 3.0, 1) + 0.30 * min(ma_gap_pct / 5.0, 1) +
                            0.30 * min(len(gap[gap > 0]) / cfg.divergence_hold_days, 1)), 1)
    return {
        **_base_row(frame), "signal": "均线多头发散", "score": score,
        "ma20": round(float(ma_fast.iloc[-1]), 3), "ma60": round(float(ma_slow.iloc[-1]), 3),
        "ma_gap_pct": round(ma_gap_pct, 2), "volume_ratio": round(vol_ratio, 2),
        "breakout_high": round(pressure, 3),
        "note": "均线多头排列且量能突破压力位；需防无量假突破，大盘弱势时谨慎",
    }


def low_shadow_test(frame: pd.DataFrame, cfg: ShadowTestConfig, risk: RiskConfig) -> dict | None:
    """低位仙人指路：低位长上影试盘后，次日阳线覆盖上影线高点。

    必须出现在前期涨幅<=30%的低位区域；高位形态多为出货信号。上影低点
    是否在后续3日守住属于未来数据，由之后的扫描日复核。
    """
    if len(frame) < 40:
        return None
    prev = frame.iloc[-2]
    today = frame.iloc[-1]
    body = abs(float(prev.close - prev.open))
    upper_shadow = float(prev.high) - max(float(prev.open), float(prev.close))
    if body <= 0 or upper_shadow / body < cfg.min_shadow_ratio:
        return None
    # 位置：前期（60日）涨幅<=30%且处于相对低位
    if _gain_over(frame, 60) > cfg.max_prior_gain_pct or not low_zone_ok(frame, risk):
        return None
    # 上影日放量但未破位（低点守住近5日支撑）
    shadow_vol_ratio = float(prev.volume) / _vol_ma(frame.iloc[:-1], cfg.vol_ma_days)
    if shadow_vol_ratio < cfg.shadow_vol_ratio:
        return None
    support = float(frame.low.astype(float).iloc[-(cfg.support_days + 1):-1].min())
    if float(prev.low) < support:
        return None
    # 次日阳线覆盖上影线最高点，且放量
    if not (float(today.close) > float(today.open) and float(today.close) >= float(prev.high)):
        return None
    cover_vol_ratio = float(today.volume / prev.volume) if prev.volume > 0 else 0.0
    if cover_vol_ratio < 1.0 or not volume_valid(float(today.volume), frame, risk):
        return None
    if not position_ok(frame, risk):
        return None
    score = round(100 * min(1, 0.40 * min(upper_shadow / body / 4.0, 1) + 0.30 * min(cover_vol_ratio / 2.0, 1) +
                            0.30 * (1 - max(_gain_over(frame, 60), 0) / cfg.max_prior_gain_pct)), 1)
    return {
        **_base_row(frame), "signal": "低位仙人指路", "score": score,
        "shadow_ratio": round(upper_shadow / body, 2), "shadow_vol_ratio": round(shadow_vol_ratio, 2),
        "shadow_high": round(float(prev.high), 3), "shadow_low": round(float(prev.low), 3),
        "cover_vol_ratio": round(cover_vol_ratio, 2), "prior_gain_60d_pct": round(_gain_over(frame, 60) * 100, 2),
        "note": "低位放量长上影试盘后阳线覆盖上影高点；需后续3日不破上影低点确认",
    }


# ---------------------------------------------------------------------------
# 原有两类信号（保持不变）
# ---------------------------------------------------------------------------

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
    # 向量化局部低点预筛（O(n²)→候选点稀疏），减少 10-50 倍循环量
    arr = close.to_numpy()
    roll_min = pd.Series(arr).rolling(9, center=True, min_periods=5).min().to_numpy()
    # 是否局部低点（±4 日最小）
    is_trough = (arr == roll_min) & np.isfinite(arr)
    trough_idx = np.where(is_trough[: current - cfg.rally_min_days])[0]
    trough_idx = trough_idx[trough_idx >= cfg.ma_slow]
    candidates: list[tuple[int, int, float]] = []
    # 仅扫描当前下跌段之前的最后一段上涨（后进先出，取最近的有效候选）
    for start in reversed(list(trough_idx)):
        end = min(start + cfg.rally_max_days, current - 5)
        if end - start < cfg.rally_min_days:
            continue
        low = arr[start]
        future = arr[start + cfg.rally_min_days : end + 1]
        peak_pos = int(np.argmax(future))
        rally = future[peak_pos] / low - 1
        if rally >= cfg.min_prior_rally_pct:
            candidates.append((start, peak_pos, float(rally)))
        if len(candidates) >= 8:
            break
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


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0)


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    return dif, dea, (dif - dea) * 2


def oversold_reversal(frame: pd.DataFrame, cfg: OversoldReversalConfig, risk: RiskConfig) -> dict | None:
    """超跌反转：缩量企稳（地量+止跌企稳）或 多指标共振（下轨/RSI/长下影/MACD背离）。

    抄底一族，逆势；必须低位 + 抛压衰竭证据，仓位要小、止损要严。
    """
    if not cfg.enabled or len(frame) < 60:
        return None
    close = frame.close.astype(float)
    high = frame.high.astype(float)
    low = frame.low.astype(float)
    open_ = frame.open.astype(float)
    vol = frame.volume.astype(float)
    if not (position_ok(frame, risk) and low_zone_ok(frame, risk)):
        return None
    ma20 = close.rolling(cfg.ma_period).mean()
    today = frame.iloc[-1]
    c = float(close.iloc[-1]); o = float(open_.iloc[-1]); l = float(low.iloc[-1])
    below_ma = c < ma20.iloc[-1]
    # --- A 缩量企稳 ---
    vol5 = float(vol.iloc[-5:].mean())
    vol20 = float(vol.iloc[-20:].mean())
    vol_contraction = vol20 > 0 and vol5 < vol20 * cfg.volume_contraction_ratio
    vol_rank = float((vol.iloc[-60:] <= vol5).mean())       # vol5 在近60日的分位（越小越低）
    vol_ground = vol_rank <= cfg.volume_quantile
    prior_low = float(low.iloc[-cfg.stab_days - 1])
    no_new_low = l >= prior_low * 0.995
    mild_stab = no_new_low and c >= o                        # 未跌破近期低点且未放量大阴
    A = vol_contraction and vol_ground and mild_stab
    # --- B 多指标共振 ---
    rsi = float(_rsi(close, cfg.rsi_period).iloc[-1])
    bb_sd = float(close.iloc[-cfg.bb_period:].std())
    lower_band = float(ma20.iloc[-1]) - cfg.bb_std * bb_sd
    touch_lower = c <= lower_band
    rsi_oversold = rsi < cfg.rsi_threshold
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    long_shadow = body > 0 and lower_shadow >= body * cfg.shadow_ratio
    dif, _dea, _hist = _macd(close)
    price_low20 = float(low.iloc[-20:].min())
    dif_min20 = float(dif.iloc[-20:].min())
    macd_div = (c <= price_low20 * 1.01) and dif.iloc[-1] > dif_min20
    n_res = sum(1 for x in (touch_lower, rsi_oversold, long_shadow, macd_div) if x)
    B = n_res >= cfg.min_resonance and (long_shadow or macd_div) and below_ma
    if not (A or B):
        return None
    which = "缩量企稳" if A else "多指标共振"
    if A and B:
        score = 100.0
    elif A:
        score = round(100 * (0.50 * min(vol20 / max(vol5, 1e-9) * 0.5, 1.0) + 0.30 * (1 - vol_rank) + 0.20), 1)
    else:
        score = round(100 * (0.40 * (n_res / 4.0) + 0.30 * max(0.0, min(1.0, (cfg.rsi_threshold - rsi) / cfg.rsi_threshold)) +
                             0.30 * (1.0 if macd_div else 0.5)), 1)
    return {
        **_base_row(frame), "signal": "超跌反转", "score": score,
        "sub_signal": which, "rsi": round(rsi, 1), "touch_lower": bool(touch_lower),
        "long_shadow": bool(long_shadow), "macd_div": bool(macd_div),
        "vol_contraction": bool(vol_contraction), "volume_ground": bool(vol_ground),
        "near_low": round(prior_low, 3),
        "note": f"超跌反转（{which}）：逆势探底、抛压衰竭；需严格止损、轻仓。",
    }


def break_ma20(frame: pd.DataFrame, cfg: BreakMa20Config, risk: RiskConfig) -> dict | None:
    """恰好突破20日线：前N日收盘<MA20、今日放量站上MA20，且非高位（右侧转折）。"""
    if not cfg.enabled or len(frame) < 30:
        return None
    close = frame.close.astype(float)
    vol = frame.volume.astype(float)
    ma20 = close.rolling(cfg.ma_period).mean()
    before = frame.iloc[-(cfg.below_days + 1):-1]
    ma_before = ma20.iloc[-(cfg.below_days + 1):-1]
    below_before = len(before) == cfg.below_days and bool((before.close < ma_before).all())
    c = float(close.iloc[-1])
    broke = below_before and c > float(ma20.iloc[-1])
    vol_ma5 = float(vol.iloc[-cfg.vol_ma_days - 1:-1].mean())
    vol_ok = vol_ma5 > 0 and float(vol.iloc[-1]) >= vol_ma5 * cfg.min_vol_ratio
    gain60 = c / float(close.iloc[-61]) - 1 if len(frame) > 61 else 0.0
    not_high = gain60 <= cfg.max_gain_60_pct
    if not (broke and vol_ok and not_high and position_ok(frame, risk)):
        return None
    score = round(100 * min(1.0, 0.45 * min(float(vol.iloc[-1]) / vol_ma5 / 2.0, 1.0) +
                            0.30 * min(max(0.0, (c - float(ma20.iloc[-1])) / float(ma20.iloc[-1])) / 0.03, 1.0) +
                            0.25 * max(0.0, 1.0 - gain60 / cfg.max_gain_60_pct)), 1)
    return {
        **_base_row(frame), "signal": "恰好突破20日线", "score": score,
        "ma20": round(float(ma20.iloc[-1]), 3),
        "broke_above_pct": round((c / float(ma20.iloc[-1]) - 1) * 100, 2),
        "prior_gain_60d_pct": round(gain60 * 100, 2),
        "note": "放量站上20日线（右侧转折）；需站稳MA20、防假突破冲高回落",
    }


def position_strategy(row: dict, market_env: str = "未知") -> dict:
    """Research-reference position sizing for a signal row.

    Transparent tiered model, never investment advice:

      base = score tier          (>=80: 30%, >=60: 20%, >=40: 10%, else 5%)
      x market environment       (偏强 1.0 / 中性 0.7 / 偏弱·未知 0.5)
      x main-force money flow    (净流入 1.2 / 净流出 0.8 / 无数据 1.0)

    Final value snaps to a 5% grid and caps at 30% (single-name risk cap).
    Every factor that moved the number is listed in position_reason so the
    derivation is auditable from the CSV alone.
    """
    score = float(row.get("score") or 0)
    if score >= 80:
        base = 30.0
    elif score >= 60:
        base = 20.0
    elif score >= 40:
        base = 10.0
    else:
        base = 5.0
    factor = 1.0
    reasons: list[str] = []
    if market_env == "偏强":
        pass
    elif market_env == "中性":
        factor *= 0.7
        reasons.append("大盘中性")
    elif market_env == "偏弱":
        factor *= 0.5
        reasons.append("大盘偏弱")
    else:
        factor *= 0.5
        reasons.append("大盘未知")
    net = row.get("net_mf_amount")
    if net is not None:
        if net > 0:
            factor *= 1.2
            reasons.append("主力净流入")
        else:
            factor *= 0.8
            reasons.append("主力净流出")
    pct = min(30.0, round(base * factor / 5) * 5)
    if pct >= 25:
        tier = "重点观察"
    elif pct >= 15:
        tier = "标准观察"
    elif pct >= 10:
        tier = "轻仓跟踪"
    else:
        tier = "仅跟踪"
    return {
        "position_pct": pct,
        "position_tier": tier,
        "position_reason": "、".join(reasons) or "满分结构",
    }


def entry_exit_plan(row: dict) -> dict:
    """Mechanical entry / stop / target plan derived from pattern anatomy.

    Each pattern's invalidation level IS its stop (the level that falsifies the
    thesis), its confirmation level is the entry, and the target comes from a
    1:2 risk-reward or the classic measured move (box height projection).
    Round-trip levels are research references, never certainty.
    """
    kind = row.get("signal")
    close = float(row.get("close") or 0)

    def _r3(entry: float, stop: float) -> tuple[float, float]:
        risk = entry - stop
        return stop, (entry + 3 * risk) if risk > 0 else stop

    entry = stop = target = None
    note = ""
    if kind == "回踩前期起涨位":
        start = row.get("start_price")
        if start:
            entry, stop, target = round(start * 1.01, 2), round(start * 0.97, 2), None
            note = "起涨位上方1%确认承接买入；跌破起涨位3%止损"
    elif kind == "横盘后放量突破":
        bh = row.get("breakout_high")
        if bh:
            entry, stop, target = round(bh * 1.005, 2), round(bh * 0.97, 2), None
            note = "突破位上沿确认买入；跌回突破位下方3%止损"
    elif kind == "箱体突破红肥绿瘦":
        bh, bl = row.get("box_high"), row.get("box_low")
        if bh and bl:
            entry, stop = round(bh * 1.005, 2), round(bh * 0.97, 2)
            measured = round(bh + (bh - bl), 2)  # 箱体高度投影（经典量度目标）
            target = max(measured, round(entry + 3 * (entry - stop), 2))
            note = "箱体上沿确认买入；跌回上沿下方3%止损；目标=箱体高度投影与1:3的较远者"
    elif kind == "阳包阴反包启动":
        yh, yl = row.get("yin_high"), row.get("yin_low")
        if yh and yl:
            entry, stop, target = round(yh * 1.005, 2), round(yl * 0.99, 2), None
            note = "突破阴线高点买入；跌破阴线低点（洗盘结构破坏）止损"
    elif kind == "涨停跳空缺口共振":
        gb = row.get("gap_bottom")
        if gb:
            entry, stop, target = round(gb * 1.005, 2), round(gb * 0.99, 2), None
            note = "回踩缺口上沿不补时买入；缺口回补即形态失效止损"
    elif kind == "龙回头二次启动":
        ph, pl = row.get("prior_high"), row.get("pullback_low")
        if ph and pl:
            entry, stop, target = round(ph * 1.005, 2), round(pl * 0.99, 2), None
            note = "放量站稳前高买入；跌破回调低点（二次启动失败）止损"
    elif kind == "均线多头发散":
        bh, ma = row.get("breakout_high"), row.get("ma20")
        if bh and ma:
            entry, stop, target = round(bh * 1.005, 2), round(ma * 0.97, 2), None
            note = "突破压力位买入；跌破MA20（多头结构破坏）止损"
    elif kind == "低位仙人指路":
        sh, sl = row.get("shadow_high"), row.get("shadow_low")
        if sh and sl:
            entry, stop, target = round(sh * 1.005, 2), round(sl * 0.99, 2), None
            note = "覆盖上影高点买入；跌破上影低点（试盘失败）止损"
    elif kind == "超跌反转":
        near = row.get("near_low")
        if near:
            entry, stop = round(c, 2), round(near * 0.985, 2)
            if entry > stop:
                target = round(entry + 3 * (entry - stop), 2)   # 1:3
                note = "企稳/反转确认后买入；跌破企稳低点（抛压未衰竭）止损"
    elif kind == "恰好突破20日线":
        ma20v = row.get("ma20")
        if ma20v:
            entry, stop = round(c, 2), round(ma20v * 0.97, 2)
            if entry > stop:
                target = round(entry + 3 * (entry - stop), 2)   # 1:3
                note = "放量站稳20日线买入；跌破MA20（假突破）止损"
    if entry is None or stop is None or entry <= stop:
        return {}
    if target is None:
        risk = entry - stop
        target = round(entry + 3 * risk, 2)     # 1:3 风险收益比
    rr = round((target - entry) / (entry - stop), 2) if entry > stop else 0
    state = "已触发" if close >= entry else "待确认"
    return {
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "risk_reward": rr,
        "entry_state": state,
        "plan_note": note,
    }


def scan_frame(
    frame: pd.DataFrame,
    support_cfg: SupportConfig,
    breakout_cfg: BreakoutConfig,
    risk: RiskConfig,
    box_cfg: BoxBreakoutConfig,
    engulf_cfg: EngulfingConfig,
    limitup_cfg: LimitUpGapConfig,
    dragon_cfg: DragonConfig,
    ma_cfg: MaDivergenceConfig,
    shadow_cfg: ShadowTestConfig,
    oversold_cfg: OversoldReversalConfig,
    break_ma20_cfg: BreakMa20Config,
    limit_pct: float = 0.10,
) -> list[dict]:
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
    if box_cfg.enabled:
        row = box_breakout_bullish(clean, box_cfg, risk)
        if row:
            out.append(row)
    if engulf_cfg.enabled:
        row = bullish_engulfing(clean, engulf_cfg, risk)
        if row:
            out.append(row)
    if limitup_cfg.enabled:
        row = limitup_gap(clean, limitup_cfg, risk, limit_pct)
        if row:
            out.append(row)
    if dragon_cfg.enabled:
        row = dragon_pullback(clean, dragon_cfg, risk)
        if row:
            out.append(row)
    if ma_cfg.enabled:
        row = ma_divergence_breakout(clean, ma_cfg, risk)
        if row:
            out.append(row)
    if shadow_cfg.enabled:
        row = low_shadow_test(clean, shadow_cfg, risk)
        if row:
            out.append(row)
    if oversold_cfg.enabled:
        row = oversold_reversal(clean, oversold_cfg, risk)
        if row:
            out.append(row)
    if break_ma20_cfg.enabled:
        row = break_ma20(clean, break_ma20_cfg, risk)
        if row:
            out.append(row)
    return out
