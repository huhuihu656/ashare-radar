from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_monitor.config import (
    BoxBreakoutConfig,
    DragonConfig,
    EngulfingConfig,
    LimitUpGapConfig,
    MaDivergenceConfig,
    RiskConfig,
    ShadowTestConfig,
)
from ashare_monitor.signals import (
    box_breakout_bullish,
    bullish_engulfing,
    dragon_pullback,
    limitup_gap,
    low_shadow_test,
    ma_divergence_breakout,
)


def bars(close: np.ndarray, volume: np.ndarray | None = None) -> pd.DataFrame:
    volume = volume if volume is not None else np.full(len(close), 100.0)
    # Close near the upper portion of each synthetic bar, as required by the
    # breakout rule.  (The default 1% symmetric bar would place close at 50%.)
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * .985,
                         "close": close, "volume": volume},
                        index=pd.date_range("2024-01-01", periods=len(close), freq="B"))


def ohlc(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
         volume: np.ndarray | None = None) -> pd.DataFrame:
    volume = volume if volume is not None else np.full(len(close), 100.0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                        index=pd.date_range("2024-01-01", periods=len(close), freq="B"))


RISK = RiskConfig()


# ---------------------------------------------------------------------------
# 箱体突破 + 红肥绿瘦
# ---------------------------------------------------------------------------

def test_box_breakout_detects_low_box_with_volume_structure() -> None:
    # 高位平台之后进入低位箱体：位置分位落在低位；箱体内红肥绿瘦、
    # 末端收敛，最后放量大阳线刺穿上沿。
    n = 101
    close = np.full(n, 12.0)
    open_ = np.full(n, 12.0)
    vol = np.full(n, 100.0)
    for i in range(60, 100):   # 40日箱体：前35日振幅0.2，末5日收敛
        amp = 0.2 if i < 95 else 0.03
        up = (i % 2 == 0)
        close[i] = 10.1 + amp / 2 if up else 10.1 - amp / 2
        open_[i] = 10.1 - amp / 2 if up else 10.1 + amp / 2
        vol[i] = 160.0 if up else 60.0
    close[-1] = 10.65          # 突破上沿（箱体高点≈10.1*1.005≈10.15）
    open_[-1] = 10.12
    vol[-1] = 280.0            # 突破前5日均量100，量比2.8
    frame = ohlc(open_, close * 1.005, close * 0.985, close, vol)
    row = box_breakout_bullish(frame, BoxBreakoutConfig(), RISK)
    assert row is not None
    assert row["signal"] == "箱体突破红肥绿瘦"
    assert row["volume_ratio"] >= 2.0
    assert 0 <= row["score"] <= 100


def test_box_breakout_rejects_high_position() -> None:
    # 同一结构但处于120日窗口顶部：位置分位守卫拦截。
    rise = np.linspace(10, 30, 60)
    n = len(rise) + 41
    close = np.concatenate([rise, np.full(40, 30.0), [30.5]])
    vol = np.full(n, 100.0)
    vol[-1] = 260.0
    row = box_breakout_bullish(bars(close, vol), BoxBreakoutConfig(), RISK)
    assert row is None


# ---------------------------------------------------------------------------
# 阳包阴反包启动
# ---------------------------------------------------------------------------

def test_engulfing_detects_washout_reversal() -> None:
    n = 30
    close = np.full(n, 10.0)
    open_ = np.full(n, 10.0)
    high = np.full(n, 10.2)
    low = np.full(n, 9.8)
    vol = np.full(n, 100.0)
    # 前两日上涨（+5%），随后缩量阴线，再放量阳线覆盖阴线最高点。
    close[-5], open_[-5] = 10.0, 9.9
    close[-4], open_[-4] = 10.2, 10.0
    close[-3], open_[-3] = 10.5, 10.2   # 前两日累计 +0.5
    close[-2], open_[-2] = 10.42, 10.5  # 缩量阴线（回调0.08）
    vol[-2] = 60.0
    close[-1], open_[-1] = 10.55, 10.35  # 放量阳线，收盘突破阴线高点
    high[-2], high[-1] = 10.52, 10.6
    low[-2], low[-1] = 10.4, 10.3
    vol[-1] = 220.0
    frame = ohlc(open_, high, low, close, vol)
    row = bullish_engulfing(frame, EngulfingConfig(), RISK)
    assert row is not None
    assert row["signal"] == "阳包阴反包启动"
    assert row["pullback_ratio"] <= 0.30


def test_engulfing_rejects_heavy_volume_red_day() -> None:
    n = 30
    close = np.full(n, 10.0)
    open_ = np.full(n, 10.0)
    high = np.full(n, 10.2)
    low = np.full(n, 9.8)
    vol = np.full(n, 100.0)
    close[-3], open_[-3] = 10.5, 10.2
    close[-2], open_[-2] = 10.42, 10.5
    vol[-2] = 200.0          # 阴线放量（出货嫌疑），不满足缩量
    close[-1], open_[-1] = 10.55, 10.35
    high[-2], high[-1] = 10.52, 10.6
    low[-2], low[-1] = 10.4, 10.3
    vol[-1] = 220.0
    row = bullish_engulfing(ohlc(open_, high, low, close, vol), EngulfingConfig(), RISK)
    assert row is None


# ---------------------------------------------------------------------------
# 涨停 + 跳空缺口共振
# ---------------------------------------------------------------------------

def _limit_frame(filled: bool) -> pd.DataFrame:
    n = 100
    close = np.array(np.linspace(8, 10, 60).tolist() + [10.0] * 40)
    open_ = close * 0.995
    high = close * 1.005
    low = close * 0.985
    vol = np.full(n, 100.0)
    limit_day = 80                 # 距今日19个交易日，落在20日窗口内
    close[limit_day] = 11.05       # 相对前收10.0 涨停（主板10%）
    high[limit_day] = 11.05        # 封板收盘
    open_[limit_day] = 10.15       # 跳空高开（前日高点10.06）
    high[limit_day - 1] = 10.06
    low[limit_day - 1] = 9.95
    if filled:
        low[81] = 9.9              # 缺口3日内回补
    else:
        low[81], low[82], low[83] = 11.0, 11.1, 11.2
    close[84:] = 11.5              # 涨停后温和走高
    vol[limit_day] = 500.0
    vol[81:] = 150.0               # 回调缩量 < 50% 涨停日
    return ohlc(open_, high, low, close, vol)


def test_limitup_gap_detects_breakout_limit_with_unfilled_gap() -> None:
    row = limitup_gap(_limit_frame(filled=False), LimitUpGapConfig(), RISK, limit_pct=0.10)
    assert row is not None
    assert row["signal"] == "涨停跳空缺口共振"
    assert row["days_since_limit"] >= 3


def test_limitup_gap_rejects_filled_gap() -> None:
    row = limitup_gap(_limit_frame(filled=True), LimitUpGapConfig(), RISK, limit_pct=0.10)
    assert row is None


# ---------------------------------------------------------------------------
# 龙回头二次启动
# ---------------------------------------------------------------------------

def _dragon_frame(deep: bool) -> pd.DataFrame:
    n = 120
    close = np.full(n, 10.0)
    open_ = np.full(n, 10.0)
    high = np.full(n, 10.2)
    low = np.full(n, 9.8)
    vol = np.full(n, 100.0)
    # 首波：20日 10 → 15（+50%），均量300
    wave = np.linspace(10, 15, 20)
    close[60:80] = wave
    high[60:80] = wave * 1.005
    low[60:80] = wave * 0.985
    open_[60:80] = wave * 0.99
    vol[60:80] = 300.0
    # 回调：15 → 13.2（-12%），缩量至 80
    close[80:100] = np.linspace(15, 13.2, 20)
    high[80:100] = close[80:100] * 1.005
    low[80:100] = close[80:100] * 0.985
    open_[80:100] = close[80:100] * 0.995
    vol[80:100] = 80.0
    if deep:
        close[80:100] = np.linspace(15, 9.0, 20)   # 深度回调（结构破坏）
        low[80:100] = close[80:100] * 0.985
        high[80:100] = close[80:100] * 1.005
    # 整理与二次启动
    close[100:119] = 13.5
    high[100:119] = 13.5 * 1.005
    low[100:119] = 13.5 * 0.985
    open_[100:119] = 13.5
    vol[100:119] = 60.0
    close[-1] = 15.15
    open_[-1] = 14.6
    high[-1] = 15.25
    low[-1] = 14.55
    vol[-1] = 700.0     # 量比 ≈ 2.3+
    return ohlc(open_, high, low, close, vol)


def test_dragon_pullback_detects_second_launch() -> None:
    row = dragon_pullback(_dragon_frame(deep=False), DragonConfig(), RISK)
    assert row is not None
    assert row["signal"] == "龙回头二次启动"
    assert row["second_vol_ratio"] >= 2.3


def test_dragon_pullback_rejects_deep_pullback() -> None:
    row = dragon_pullback(_dragon_frame(deep=True), DragonConfig(), RISK)
    assert row is None


# ---------------------------------------------------------------------------
# 均线多头发散 + 量能突破
# ---------------------------------------------------------------------------

def test_ma_divergence_detects_breakout() -> None:
    n = 141
    # 前段下跌筑底，后段稳步上行形成金叉与多头发散
    close = np.concatenate([np.linspace(20, 10, 70), np.linspace(10, 16, 70), [16.8]])
    open_ = close * 0.995
    high = np.append((close * 1.01)[:-1], 16.9)
    low = close * 0.985
    vol = np.full(n, 100.0)
    vol[-30:] = 130.0     # 量能站上50日均量线
    vol[-1] = 280.0       # 突破日量比≥2
    frame = ohlc(open_, high, low, close, vol)
    row = ma_divergence_breakout(frame, MaDivergenceConfig(), RISK)
    assert row is not None
    assert row["signal"] == "均线多头发散"
    assert row["volume_ratio"] >= 2.0


def test_ma_divergence_rejects_without_cross() -> None:
    n = 141
    close = np.linspace(20, 8, n)     # 单边下跌，无金叉
    open_ = close * 0.995
    high = close * 1.01
    low = close * 0.985
    vol = np.full(n, 100.0)
    frame = ohlc(open_, high, low, close, vol)
    assert ma_divergence_breakout(frame, MaDivergenceConfig(), RISK) is None


# ---------------------------------------------------------------------------
# 低位仙人指路
# ---------------------------------------------------------------------------

def test_low_shadow_detects_probe_then_cover() -> None:
    n = 98
    # 高位平台（12.0）之后回落至10.0附近 → 低位；长上影试盘+次日覆盖
    close = np.concatenate([np.full(60, 12.0), np.full(38, 10.0)])
    open_ = close.copy()
    high = close * 1.005
    low = close * 0.985
    vol = np.full(n, 100.0)
    open_[-2], close[-2] = 10.0, 10.05
    high[-2], low[-2] = 10.5, 9.98
    vol[-2] = 150.0        # 放量但未破位
    open_[-1], close[-1] = 10.1, 10.55
    high[-1], low[-1] = 10.6, 10.05
    vol[-1] = 200.0
    frame = ohlc(open_, high, low, close, vol)
    row = low_shadow_test(frame, ShadowTestConfig(), RISK)
    assert row is not None
    assert row["signal"] == "低位仙人指路"
    assert row["shadow_ratio"] >= 2.0


def test_low_shadow_rejects_high_position() -> None:
    n = 60
    # 前期大涨（60日涨幅>30%），高位长上影 → 出货嫌疑
    close = np.concatenate([np.full(20, 10.0), np.linspace(10, 20, 40)])
    open_ = close * 0.995
    high = close * 1.005
    low = close * 0.985
    vol = np.full(n, 100.0)
    open_[-2], close[-2] = 20.0, 20.1
    high[-2], low[-2] = 21.0, 19.95
    vol[-2] = 150.0
    open_[-1], close[-1] = 20.2, 21.1
    high[-1], low[-1] = 21.2, 20.1
    vol[-1] = 200.0
    frame = ohlc(open_, high, low, close, vol)
    assert low_shadow_test(frame, ShadowTestConfig(), RISK) is None


# ---------------------------------------------------------------------------
# 参考仓位策略
# ---------------------------------------------------------------------------

def test_position_strategy_score_tiers_and_strong_market() -> None:
    from ashare_monitor.signals import position_strategy
    row = position_strategy({"score": 85}, "偏强")
    assert row["position_pct"] == 30.0
    assert row["position_tier"] == "重点观察"


def test_position_strategy_weak_market_outflow_slashes() -> None:
    from ashare_monitor.signals import position_strategy
    row = position_strategy({"score": 55, "net_mf_amount": -100}, "偏弱")
    assert row["position_pct"] <= 10
    assert "大盘偏弱" in row["position_reason"]
    assert "主力净流出" in row["position_reason"]


def test_position_strategy_inflow_boost_capped_at_30() -> None:
    from ashare_monitor.signals import position_strategy
    row = position_strategy({"score": 90, "net_mf_amount": 1000}, "偏强")
    assert row["position_pct"] == 30.0  # 单票上限 30%
    assert "主力净流入" in row["position_reason"]


# ---------------------------------------------------------------------------
# 买卖点计划
# ---------------------------------------------------------------------------

def test_plan_support_entry_stop_r3() -> None:
    from ashare_monitor.signals import entry_exit_plan
    plan = entry_exit_plan({"signal": "回踩前期起涨位", "close": 15.0, "start_price": 14.55})
    assert plan["entry_price"] > plan["stop_loss"]
    assert plan["take_profit"] > plan["entry_price"]
    assert plan["risk_reward"] == 3.0
    assert plan["entry_state"] in ("已触发", "待确认")


def test_plan_box_measured_move_target() -> None:
    from ashare_monitor.signals import entry_exit_plan
    plan = entry_exit_plan({"signal": "箱体突破红肥绿瘦", "close": 64.2,
                            "box_high": 62.5, "box_low": 58.0})
    # 箱体量度目标 67.0 vs 1:3 目标 62.81 + 3*(62.81-60.62)=69.38 → 取较远者
    assert plan["take_profit"] == 69.38
    assert plan["stop_loss"] < plan["entry_price"]


def test_plan_unknown_signal_returns_empty() -> None:
    from ashare_monitor.signals import entry_exit_plan
    assert entry_exit_plan({"signal": "未知信号", "close": 10}) == {}


# ---------------------------------------------------------------------------
# 月度主线板块：确定性评分
# ---------------------------------------------------------------------------

def test_minmax_normalizes_to_01() -> None:
    import sys
    sys.path.insert(0, "scripts")
    from monthly_mainline import minmax

    import pandas as pd
    values = minmax(pd.Series([1.0, 2.0, 3.0], index=["a", "b", "c"]))
    assert abs(values["a"] - 0.0) < 1e-9
    assert abs(values["c"] - 1.0) < 1e-9
    assert abs(values["b"] - 0.5) < 1e-9


def test_minmax_flat_series_returns_half() -> None:
    import sys
    sys.path.insert(0, "scripts")
    from monthly_mainline import minmax

    import pandas as pd
    values = minmax(pd.Series([2.0, 2.0, 2.0], index=["a", "b", "c"]))
    assert (values == 0.5).all()


# ---------------------------------------------------------------------------
# 利好消息因子
# ---------------------------------------------------------------------------

def test_news_classify_positive() -> None:
    from ashare_monitor.news_factor import classify_text
    assert classify_text("拟回购3亿元彰显信心") == 1
    assert classify_text("中标国家电网项目") == 1
    assert classify_text("业绩预增50%") == 1


def test_news_classify_negative_wins() -> None:
    from ashare_monitor.news_factor import classify_text
    assert classify_text("股东计划减持不超过2%") == 0
    assert classify_text("风险提示：股价异常波动") == 0


def test_news_sort_score_capped_bonus() -> None:
    from ashare_monitor.news_factor import sort_score
    assert sort_score(70.0, 0) == 70.0
    assert sort_score(70.0, 2) == 76.0
    # 多条新闻加成封顶 15
    assert sort_score(70.0, 20) == 85.0
