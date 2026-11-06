from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_monitor.config import ClearUptrendConfig, RiskConfig
from ashare_monitor.signals import clear_uptrend, uptrend_metrics


def build(close: np.ndarray) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996,
         "close": close, "volume": np.full(len(close), 100.0)},
        index=pd.date_range("2024-01-01", periods=len(close), freq="B"),
    )


def uptrend(n_base: int = 140, n_rise: int = 60, base: float = 8.0,
            gain: float = 0.20, wobble: float = 0.004, seed: int = 7) -> np.ndarray:
    """先横盘建立 MA60，再规整上行；wobble 制造温和日常波动。"""
    rng = np.random.default_rng(seed)
    ramp = np.linspace(base, base * (1 + gain), n_rise)
    rise = ramp * (1 + rng.uniform(-wobble, wobble, n_rise))
    return np.concatenate([np.full(n_base, base), rise])


CFG = ClearUptrendConfig()


def test_metrics_of_clean_uptrend() -> None:
    m = uptrend_metrics(build(uptrend()), CFG, 0.10)
    assert m is not None
    assert m["px"] > m["ma20"] > m["ma60"]
    assert m["gain_60"] >= 0.10
    assert m["er"] >= 0.18
    assert m["p95"] < 0.07
    assert m["mdd"] > -0.15
    assert m["limit_ups"] <= 1


def test_metrics_returns_none_on_short_history() -> None:
    assert uptrend_metrics(build(np.full(120, 8.0)), CFG, 0.10) is None


def test_metrics_returns_none_on_nonpositive_price() -> None:
    bad = np.full(220, 0.0)
    assert uptrend_metrics(build(bad), CFG, 0.10) is None


def test_metrics_returns_none_when_window_exceeds_history() -> None:
    """min_history_days 被调得比 window+1 还小时，仍不能越界。"""
    tiny = ClearUptrendConfig(min_history_days=10)
    assert uptrend_metrics(build(np.full(50, 8.0)), tiny, 0.10) is None


RISK = RiskConfig()


def assert_rejected_by(close: np.ndarray, key: str, predicate) -> None:
    """先证明反例确实违反了目标指标，再证明检测器拒绝——防止因别的原因被拒。"""
    frame = build(close)
    m = uptrend_metrics(frame, CFG, 0.10)
    assert m is not None, "反例样本本身长度必须足够"
    assert predicate(m[key]), f"反例没有违反 {key}: {m[key]}"
    assert clear_uptrend(frame, CFG, RISK) is None


def test_contract_exposes_full_metric_set() -> None:
    """指标字典是判定层与测试层的显式契约：缺键、多键都算破坏接口。"""
    m = uptrend_metrics(build(uptrend()), CFG, 0.10)
    assert m is not None
    assert set(m) == {
        "px", "ma20", "ma60", "ma20_prev", "gain_60", "ext", "er", "p95", "mdd",
        "limit_ups", "higher_lows", "higher_highs",
    }


def test_structure_flags_use_the_last_bar() -> None:
    """三段结构必须覆盖最近 60 根（含判定当日），不能把今日漏在窗口外。"""
    close = uptrend()
    close[-21:-1] = close[-22]        # 第三段（除末根）压平到第二段水平
    close[-1] = close[-22] * 1.06     # 只有末根把第三段高点顶到第二段之上
    m = uptrend_metrics(build(close), CFG, 0.10)
    assert m is not None
    assert m["higher_highs"] is True


def test_accepts_clean_uptrend_and_exposes_fields() -> None:
    row = clear_uptrend(build(uptrend()), CFG, RISK)
    assert row is not None
    assert row["signal"] == "清晰上升趋势"
    for key in ("er", "gain_60_pct", "mdd_pct", "ext_pct", "ma20", "ma60",
                "limit_ups", "higher_lows", "higher_highs", "today_high", "today_low", "score"):
        assert key in row
    assert 0 <= row["score"] <= 100


def test_rejects_below_ma60() -> None:
    close = uptrend()
    close[-1] = close[-1] * 0.60      # 收盘砸到 MA20/MA60 之下
    m = uptrend_metrics(build(close), CFG, 0.10)
    assert m is not None
    assert m["px"] < m["ma60"], "反例没有真的跌破 MA60"
    assert clear_uptrend(build(close), CFG, RISK) is None


def test_rejects_ma20_not_rising() -> None:
    close = uptrend()
    # 末段先回调 4% 再横住，最后一根反抽回前高：价格仍在 MA20 上方，但 MA20 已不再上行。
    close[-20:-1] = close[-21] * 0.96
    close[-1] = close[-21]
    m = uptrend_metrics(build(close), CFG, 0.10)
    assert m is not None
    assert m["ma20"] <= m["ma20_prev"], "反例没有让 MA20 停止上行"
    assert clear_uptrend(build(close), CFG, RISK) is None


def test_rejects_insufficient_gain() -> None:
    assert_rejected_by(uptrend(gain=0.04), "gain_60", lambda v: v < CFG.min_gain_60)


def test_rejects_overextended() -> None:
    close = uptrend()
    close[-1] = close[-2] * 1.16      # 单日跳 16%，偏离 MA20 过大
    assert_rejected_by(close, "ext", lambda v: v > CFG.max_ext_pct)


def test_rejects_choppy_low_efficiency() -> None:
    base = uptrend()
    t = np.arange(len(base))
    close = base * (1 + 0.06 * np.sin(t / 2.0))   # 宽幅震荡但净涨幅仍在
    assert_rejected_by(close, "er", lambda v: v < CFG.min_efficiency)


def test_rejects_spiky_daily_moves() -> None:
    close = uptrend()
    close[-20::4] *= 1.09             # 每隔 4 天来一根大阳
    assert_rejected_by(close, "p95", lambda v: v >= CFG.max_daily_p95)


def test_rejects_deep_drawdown() -> None:
    close = uptrend()
    close[-35:-25] *= 0.80            # 中途深跌 20%
    assert_rejected_by(close, "mdd", lambda v: v <= -CFG.max_drawdown)


def test_rejects_multiple_limit_ups() -> None:
    close = uptrend()
    close[-10] = close[-11] * 1.10
    close[-6] = close[-7] * 1.10
    assert_rejected_by(close, "limit_ups", lambda v: v > CFG.max_limit_ups)


def test_rejects_single_gap_rise() -> None:
    """横盘后最后一根跳空拉起（近似停牌复牌），不得判为清晰上涨。"""
    close = uptrend()
    close[-60:] = close[-61]
    close[-1] = close[-61] * 1.20
    m = uptrend_metrics(build(close), CFG, 0.10)
    assert m is not None
    assert m["ext"] > CFG.max_ext_pct, "该反例应当由偏离度拦下，而不是 p95"
    assert clear_uptrend(build(close), CFG, RISK) is None


def test_limit_up_detection_respects_board_limit() -> None:
    """同样的 +10% 大阳，在 20% 涨跌幅板块不应当作涨停。"""
    close = uptrend()
    close[-18] = close[-19] * 1.10    # 落在 limitup_lookback=20 的统计窗口内
    m_main = uptrend_metrics(build(close), CFG, 0.10)
    m_gem = uptrend_metrics(build(close), CFG, 0.20)
    assert m_main is not None and m_gem is not None
    assert m_main["limit_ups"] >= 1
    assert m_gem["limit_ups"] == 0


def test_rejects_short_history() -> None:
    assert clear_uptrend(build(np.full(100, 8.0)), CFG, RISK) is None


def test_rejects_when_disabled() -> None:
    assert clear_uptrend(build(uptrend()), ClearUptrendConfig(enabled=False), RISK) is None
