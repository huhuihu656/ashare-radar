from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_monitor.config import BreakoutConfig, SupportConfig
from ashare_monitor.signals import sideways_breakout, support_retest


def bars(close: np.ndarray, volume: np.ndarray | None = None) -> pd.DataFrame:
    volume = volume if volume is not None else np.full(len(close), 100.0)
    # Close near the upper portion of each synthetic bar, as required by the
    # breakout rule.  (The default 1% symmetric bar would place close at 50%.)
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * .985,
                         "close": close, "volume": volume},
                        index=pd.date_range("2024-01-01", periods=len(close), freq="B"))


def test_support_retest_identifies_downtrend_near_prior_start() -> None:
    close = np.concatenate([np.full(65, 10.0), np.linspace(10, 13, 35), np.linspace(13, 10.3, 70)])
    row = support_retest(bars(close), SupportConfig())
    assert row is not None
    assert row["signal"] == "回踩前期起涨位"
    assert 0 <= row["distance_to_start_pct"] <= 6


def test_sideways_breakout_needs_volume_surge() -> None:
    close = np.concatenate([np.full(45, 10.0), 10 + np.sin(np.arange(20)) * .18, [10.45]])
    volume = np.concatenate([np.full(65, 100.0), [250.0]])
    row = sideways_breakout(bars(close, volume), BreakoutConfig())
    assert row is not None
    assert row["volume_ratio"] >= 1.8
