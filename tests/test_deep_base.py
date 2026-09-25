"""深跌筑底回踩前高：摆动点识别 + 形态判定。

合成 K 线全部由「路径点」线性插值构造，high/low 取路径值 ±0.2%，
这样每个摆动极值都精确落在预期位置，断言可以写死数值。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_monitor.config import DeepBaseConfig, RiskConfig
from ashare_monitor.signals import deep_base_retest, zigzag_pivots

RISK = RiskConfig()


def path(waypoints: list[tuple[int, float]]) -> np.ndarray:
    """按 (下标, 价格) 路径点线性插值出收盘价序列。"""
    out: list[float] = []
    for (i0, v0), (i1, v1) in zip(waypoints, waypoints[1:]):
        for i in range(i0, i1):
            out.append(v0 + (v1 - v0) * ((i - i0) / (i1 - i0)))
    out.append(waypoints[-1][1])
    return np.array(out, dtype=float)


def frame(values: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": values, "high": values * 1.002, "low": values * 0.998,
         "close": values, "volume": np.full(len(values), 100.0)},
        index=pd.date_range("2024-01-01", periods=len(values), freq="B"),
    )


# 教科书形态：-60.2% 深跌 → 底部 11.98 → 反弹 +67% → 高点/低点各三次抬高
# → 突破颈线 28.056 → 回踩 27.445（未破）→ 收盘 28.5
TEXTBOOK = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 24.0),
            (150, 19.0), (180, 28.0), (190, 25.0), (210, 30.0), (216, 27.5), (219, 28.5)]


def textbook(**overrides) -> pd.DataFrame:
    pts = overrides.pop("waypoints", TEXTBOOK)
    return frame(path(pts))


# ---------------------------------------------------------------------------
# ZigZag 摆动点识别
# ---------------------------------------------------------------------------

def test_zigzag_alternates_and_respects_min_amplitude() -> None:
    df = textbook()
    piv = zigzag_pivots(df.high.to_numpy(), df.low.to_numpy(), 0.10)
    assert len(piv) >= 6
    for k in range(1, len(piv)):
        assert piv[k][1] != piv[k - 1][1], "摆动点必须高低交替"
        assert piv[k][0] > piv[k - 1][0], "下标必须递增"
    # 除首个种子点外，相邻摆动幅度必须 >= 10%
    for k in range(2, len(piv)):
        a, b = piv[k - 1][2], piv[k][2]
        amp = b / a - 1 if piv[k][1] == "H" else 1 - b / a
        assert amp >= 0.10 - 1e-9, f"第{k}个摆动幅度 {amp:.4f} 不足 10%"


def test_zigzag_never_emits_two_pivots_on_one_bar() -> None:
    """回归：早期实现用两个独立 if，同一个 bar 会同时吐出高点和低点。"""
    df = textbook()
    piv = zigzag_pivots(df.high.to_numpy(), df.low.to_numpy(), 0.10)
    idx = [p[0] for p in piv]
    assert len(set(idx)) == len(idx), f"同一 bar 出现多个摆动点: {idx}"
    assert len(piv) < len(df), "摆动点数不应接近 bar 数"


def test_zigzag_returns_empty_on_flat_series() -> None:
    flat = np.full(60, 10.0)
    assert zigzag_pivots(flat * 1.002, flat * 0.998, 0.10) == []


# ---------------------------------------------------------------------------
# 形态判定：命中
# ---------------------------------------------------------------------------

def test_deep_base_accepts_textbook_pattern() -> None:
    row = deep_base_retest(textbook(), DeepBaseConfig(), RISK)
    assert row is not None, "教科书形态必须命中"
    assert row["signal"] == "深跌筑底回踩前高"
    assert row["drop_pct"] <= -50.0            # 前期跌幅达标
    assert 0 < row["rebound_pct"] <= 100.0     # 反弹不超过 1 倍
    assert row["higher_lows"] >= 3             # 底部抬高次数
    assert row["higher_highs"] >= 3
    assert row["break_pct"] > 0                # 已突破前高
    assert row["retest_pct"] >= -3.0           # 回踩未破前高


def test_deep_base_plan_is_tradeable() -> None:
    from ashare_monitor.signals import entry_exit_plan
    row = deep_base_retest(textbook(), DeepBaseConfig(), RISK)
    plan = entry_exit_plan(row)
    assert plan, "深跌筑底回踩前高 必须有买卖点计划"
    assert plan["stop_loss"] < plan["entry_price"] < plan["take_profit"]
    assert plan["risk_reward"] == 3.0


# ---------------------------------------------------------------------------
# 形态判定：逐个反例
# ---------------------------------------------------------------------------

def test_rejects_shallow_drop() -> None:
    """只跌 40%，不满足「前期深跌」。"""
    pts = [(0, 30.0), (60, 18.0), (90, 22.0), (105, 19.0), (135, 25.0),
           (150, 21.0), (180, 30.0), (190, 27.0), (210, 33.0), (216, 31.0), (219, 32.0)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_rebound_over_100pct() -> None:
    """底部 12 反弹到 26（+116%），超过「不超过1倍」。"""
    pts = [(0, 30.0), (60, 12.0), (90, 26.0), (105, 21.0), (135, 30.0),
           (150, 25.0), (180, 36.0), (190, 32.0), (210, 40.0), (216, 37.0), (219, 38.0)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_when_bottoms_not_rising() -> None:
    """最后一个回调低点跌破了前一个低点，底部没有越抬越高。"""
    pts = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 24.0),
           (150, 14.0), (180, 28.0), (190, 25.0), (210, 30.0), (216, 27.5), (219, 28.5)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_when_tops_not_rising() -> None:
    """中间那个高点低于前一个高点，高点没有越抬越高。"""
    pts = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 18.0),
           (150, 15.0), (180, 28.0), (190, 25.0), (210, 30.0), (216, 27.5), (219, 28.5)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_when_never_broke_neck() -> None:
    """一路上涨但从没突破最后一个摆动高点（27.7 < 28.056*1.01）。"""
    pts = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 24.0),
           (150, 19.0), (180, 28.0), (190, 25.0), (210, 27.6), (216, 27.5), (219, 27.6)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_when_pullback_breaks_neck() -> None:
    """突破后回踩跌破前高（26.5 < 28.056*0.97 = 27.21），前高没守住。"""
    pts = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 24.0),
           (150, 19.0), (180, 28.0), (190, 25.0), (210, 30.0), (216, 26.5), (219, 27.0)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_when_close_below_neck() -> None:
    """回踩守住了，但今日收盘仍在（未站上）前高之下。"""
    pts = [(0, 30.0), (60, 12.0), (90, 20.0), (105, 16.0), (135, 24.0),
           (150, 19.0), (180, 28.0), (190, 25.0), (210, 30.0), (216, 27.5), (219, 27.9)]
    assert deep_base_retest(textbook(waypoints=pts), DeepBaseConfig(), RISK) is None


def test_rejects_short_history() -> None:
    """样本不足时不得抛异常，直接返回 None。"""
    short = frame(path([(0, 30.0), (60, 12.0)]))
    assert deep_base_retest(short, DeepBaseConfig(), RISK) is None


def test_disabled_config_returns_none() -> None:
    assert deep_base_retest(textbook(), DeepBaseConfig(enabled=False), RISK) is None


# ---------------------------------------------------------------------------
# 配置闸门
# ---------------------------------------------------------------------------

def test_position_gate_can_reject() -> None:
    """6 个月涨幅上限收紧到 10% 后，本形态（+59%）应被位置闸门拦下。"""
    strict = RiskConfig(max_position_gain_pct=0.10)
    assert deep_base_retest(textbook(), DeepBaseConfig(), strict) is None
    # 关闭风控则放行，证明拒绝确实来自位置闸门而非形态本身
    assert deep_base_retest(textbook(), DeepBaseConfig(), RiskConfig(enabled=False)) is not None


def test_swing_pct_is_configurable() -> None:
    """把摆动阈值放大到 25%，同一个形态的摆动点被合并，不再满足结构要求。"""
    assert deep_base_retest(textbook(), DeepBaseConfig(swing_pct=0.25), RISK) is None


# ---------------------------------------------------------------------------
# 长窗口接线（deep_frame）
# ---------------------------------------------------------------------------

def scan(frame_df, deep=None):
    from ashare_monitor.config import Config
    from ashare_monitor.signals import scan_frame
    c = Config()
    return scan_frame(frame_df, c.support_retest, c.breakout, c.risk, c.box_breakout,
                      c.bullish_engulfing, c.limitup_gap, c.dragon_pullback,
                      c.ma_divergence, c.low_shadow, c.oversold_reversal,
                      c.break_ma20, c.boll_pin, c.ene_pullback, c.deep_base,
                      deep_frame=deep)


def test_deep_frame_is_what_the_new_signal_reads() -> None:
    """短帧本身不足以判定，只有 deep_frame 补上历史后才命中。"""
    short = textbook().tail(120)          # 120 < min_history_days，直接判负
    assert not [r for r in scan(short) if r["signal"] == "深跌筑底回踩前高"]
    hit = [r for r in scan(short, deep=textbook()) if r["signal"] == "深跌筑底回踩前高"]
    assert hit, "提供了 deep_frame 就应该能判定出形态"


def tight_breakout_frame() -> pd.DataFrame:
    """横盘后放量突破：20 日窄幅整理，末日放量大阳收在日内高位。"""
    n = 60
    close = np.concatenate([np.full(n, 10.0), [10.5]])
    return pd.DataFrame(
        {"open": np.concatenate([np.full(n, 10.0), [10.05]]),
         "high": np.concatenate([np.full(n, 10.02), [10.55]]),
         "low": np.concatenate([np.full(n, 9.98), [10.0]]),
         "close": close,
         "volume": np.concatenate([np.full(n, 100.0), [500.0]])},
        index=pd.date_range("2024-01-01", periods=n + 1, freq="B"),
    )


def test_deep_frame_does_not_change_other_signals() -> None:
    """换 deep_frame 不得影响其余检测器的输出——这是长窗口接线的核心约束。"""
    base = tight_breakout_frame()
    other = textbook()
    # 逐字段比对，而不是只比信号名——只比值的话，数值被改写是抓不出来的
    a = {r["signal"]: r for r in scan(base) if r["signal"] != "深跌筑底回踩前高"}
    b = {r["signal"]: r for r in scan(base, deep=other) if r["signal"] != "深跌筑底回踩前高"}
    # 对照组必须真的含其他信号，否则下面这行等于没断言（曾经就是空的）
    assert "横盘后放量突破" in a, f"对照组没触发既有信号，断言会变成空转: {a}"
    assert a == b, f"deep_frame 泄漏到了其他检测器: {a}"


def test_scan_frame_reports_deep_base_signal() -> None:
    rows = scan(textbook())
    sigs = [r["signal"] for r in rows]
    assert "深跌筑底回踩前高" in sigs
