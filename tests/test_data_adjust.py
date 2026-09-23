"""前复权必须作用于整根 K 线。

回归测试：批量缓存路径曾只把 `close` 乘以因子、open/high/low 留原始价，导致
close 与 high/low 落在两个价格基准上，任何拿 low/high 去比由复权收盘价算出的
水平位的信号都会静默失真（实测曾有 185 万根 K 线的 close 落在同日 [low, high] 之外）。
"""
import pandas as pd
import pytest

from ashare_monitor.data import adjust_bars_forward

TOL = 1e-9


def _raw() -> pd.DataFrame:
    """3 个交易日，中间一次除权把 adj_factor 从 1.0 抬到 1.5。"""
    return pd.DataFrame({
        "ts_code": ["000001"] * 3,
        "trade_date": ["20260918", "20260921", "20260922"],
        "adj_factor": [1.0, 1.0, 1.5],
        "open": [10.0, 10.2, 10.4],
        "high": [10.5, 10.6, 10.8],
        "low": [9.8, 9.9, 10.1],
        "close": [10.2, 10.4, 10.5],
        "vol": [100.0, 110.0, 120.0],
    })


def test_all_four_price_columns_share_one_basis() -> None:
    raw = _raw()
    scale = raw["adj_factor"] / 1.5          # 每行因子不同，必须逐行缩放
    bars = adjust_bars_forward(raw, base=1.5)
    for col in ("open", "high", "low", "close"):
        assert list(bars[col]) == pytest.approx(list(raw[col] * scale), abs=TOL), col


def test_adjusted_close_stays_inside_adjusted_day_range() -> None:
    """这正是那个 bug 违反的不变式：复权后 close 必须落在同日复权后的 [low, high] 内。"""
    bars = adjust_bars_forward(_raw(), base=1.5)
    assert (bars.close <= bars.high + TOL).all()
    assert (bars.close >= bars.low - TOL).all()


def test_volume_is_left_unadjusted() -> None:
    bars = adjust_bars_forward(_raw(), base=1.5)
    assert list(bars.volume) == [100.0, 110.0, 120.0]


def test_latest_bar_scales_by_one() -> None:
    """base 取该股最新因子，所以最新一根的价格保持原始数值（前复权锚点）。"""
    raw = _raw()
    bars = adjust_bars_forward(raw, base=1.5)
    assert bars.close.iloc[-1] == pytest.approx(float(raw.close.iloc[-1]), abs=TOL)


def test_high_low_ordering_survives_scaling() -> None:
    bars = adjust_bars_forward(_raw(), base=1.5)
    assert (bars.high >= bars.low).all()
    assert (bars.high >= bars.open).all()
    assert (bars.high >= bars.close).all()
    assert (bars.low <= bars.open).all()
    assert (bars.low <= bars.close).all()
