from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_monitor.config import ClearUptrendConfig
from ashare_monitor.signals import uptrend_metrics


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
