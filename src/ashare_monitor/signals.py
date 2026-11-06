from __future__ import annotations

import numpy as np
import pandas as pd

from .config import ClearUptrendConfig, RiskConfig


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

def uptrend_metrics(frame: pd.DataFrame, cfg: ClearUptrendConfig,
                    limit_pct: float) -> dict | None:
    """计算「清晰上升趋势」判定所需的全部原始指标，不做任何阈值判断。

    拆成独立函数是为了让测试能逐项断言「这条反例到底违反了哪个指标」，
    避免反例因为别的原因被拒而让测试变成空转。
    """
    if len(frame) < cfg.min_history_days:
        return None
    close = frame.close.astype(float)
    c = close.to_numpy()
    w = cfg.window
    if len(c) < w + 1:
        return None
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    if not (np.isfinite(ma20.iloc[-1]) and np.isfinite(ma60.iloc[-1])):
        return None
    px = float(c[-1])
    m20 = float(ma20.iloc[-1])
    m60 = float(ma60.iloc[-1])
    if not (px > 0 and m20 > 0 and m60 > 0):
        return None
    seg = c[-(w + 1):]
    if seg[0] <= 0:
        return None
    path = float(np.abs(np.diff(seg)).sum())
    ret = np.diff(seg) / seg[:-1]
    run_max = np.maximum.accumulate(seg)
    block = max(1, w // 3)
    blocks = [seg[1 + i * block:1 + (i + 1) * block] for i in range(3)]
    lows = [float(b.min()) for b in blocks]
    highs = [float(b.max()) for b in blocks]
    return {
        "px": px,
        "ma20": m20,
        "ma60": m60,
        "ma20_prev": float(ma20.iloc[-1 - cfg.ma20_slope_days]),
        "gain_60": float(seg[-1] / seg[0] - 1),
        "ext": float(px / m20 - 1),
        "er": float(abs(seg[-1] - seg[0]) / path) if path > 0 else 0.0,
        "p95": float(np.percentile(np.abs(ret), 95)) if ret.size else 1.0,
        "mdd": float(np.min(seg / run_max - 1)),
        "limit_ups": int((ret[-cfg.limitup_lookback:] >= limit_pct - cfg.limit_tolerance).sum()),
        "higher_lows": all(lows[i + 1] > lows[i] for i in range(len(lows) - 1)),
        "higher_highs": all(highs[i + 1] > highs[i] for i in range(len(highs) - 1)),
    }

def clear_uptrend(frame: pd.DataFrame, cfg: ClearUptrendConfig, risk: RiskConfig,
                  limit_pct: float = 0.10) -> dict | None:
    """清晰上升趋势：趋势结构 + K 线规整度 + 非打板情绪，三者同时成立。"""
    if not cfg.enabled:
        return None
    m = uptrend_metrics(frame, cfg, limit_pct)
    if m is None:
        return None
    # 趋势
    if not (m["px"] > m["ma20"] > m["ma60"]):
        return None
    if m["ma20"] <= m["ma20_prev"]:
        return None
    if m["gain_60"] < cfg.min_gain_60:
        return None
    if m["ext"] > cfg.max_ext_pct:
        return None
    # K 线老实
    if m["er"] < cfg.min_efficiency:
        return None
    if m["p95"] >= cfg.max_daily_p95:
        return None
    if m["mdd"] <= -cfg.max_drawdown:
        return None
    # 非情绪驱动
    if m["limit_ups"] > cfg.max_limit_ups:
        return None
    if cfg.require_higher_swings and not (m["higher_lows"] and m["higher_highs"]):
        return None
    if not position_ok(frame, risk):
        return None
    score = round(100 * min(1.0,
                            0.35 * min(m["er"] / 0.40, 1.0) +
                            0.25 * min(m["gain_60"] / 0.40, 1.0) +
                            0.20 * min(-m["mdd"] / 0.20, 1.0) +
                            0.20 * (1 - m["limit_ups"] / max(1, cfg.max_limit_ups + 1))), 1)
    last = frame.iloc[-1]
    return {
        **_base_row(frame), "signal": "清晰上升趋势", "score": score,
        "er": round(m["er"], 3),
        "gain_60_pct": round(m["gain_60"] * 100, 2),
        "mdd_pct": round(m["mdd"] * 100, 2),
        "ext_pct": round(m["ext"] * 100, 2),
        "ma20": round(m["ma20"], 3), "ma60": round(m["ma60"], 3),
        "limit_ups": m["limit_ups"],
        "higher_lows": bool(m["higher_lows"]), "higher_highs": bool(m["higher_highs"]),
        "today_high": round(float(last.high), 3), "today_low": round(float(last.low), 3),
        "note": "价格沿上行的MA20/MA60规整抬升，近20日无连续打板；收盘跌破MA20即趋势结构破坏",
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

    entry = stop = target = None
    note = ""
    if kind == "清晰上升趋势":
        m20 = row.get("ma20")
        if m20:
            entry, stop = round(close, 2), round(m20 * 0.97, 2)
            if entry > stop:
                target = round(entry + 3 * (entry - stop), 2)
                note = "趋势确认后买入；收盘跌破 MA20（趋势结构破坏）止损"
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

def scan_frame(frame: pd.DataFrame, uptrend_cfg: ClearUptrendConfig, risk: RiskConfig,
               limit_pct: float = 0.10) -> list[dict]:
    """对单只股票的日线帧跑信号判定，返回 0 或 1 条信号。"""
    clean = frame.copy().sort_index()
    clean = clean[~clean.index.duplicated(keep="last")]
    clean = clean.dropna(subset=["open", "high", "low", "close", "volume"])
    if not uptrend_cfg.enabled:
        return []
    row = clear_uptrend(clean, uptrend_cfg, risk, limit_pct)
    return [row] if row else []
