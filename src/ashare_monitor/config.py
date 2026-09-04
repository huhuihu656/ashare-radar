from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass(frozen=True)
class ScanConfig:
    workers: int = 8
    lookback_days: int = 320
    min_history_days: int = 150
    exclude_st: bool = True
    include_boards: tuple[str, ...] = ("主板", "创业板", "科创板", "北交所")
    cache_dir: str = "data/cache"
    output_dir: str = "data/reports"


@dataclass(frozen=True)
class SupportConfig:
    enabled: bool = True
    min_prior_rally_pct: float = 0.15
    rally_min_days: int = 15
    rally_max_days: int = 90
    approach_band_above_pct: float = 0.06
    invalidation_below_pct: float = 0.03
    ma_fast: int = 20
    ma_slow: int = 60


@dataclass(frozen=True)
class BreakoutConfig:
    enabled: bool = True
    consolidation_days: int = 20
    max_range_pct: float = 0.12
    max_atr_pct: float = 0.035
    volume_baseline_days: int = 20
    min_volume_ratio: float = 1.8
    breakout_lookback_days: int = 20
    min_close_position: float = 0.65


@dataclass(frozen=True)
class RiskConfig:
    """Shared risk guards: position, volume and market regime.

    位置错误（6个月涨幅>100%）、量能缺失（量比<1.5）、大盘单边下跌是形态
    共性的失效条件；这些门槛在所有新形态上统一生效。
    """
    enabled: bool = True
    position_lookback_days: int = 120
    max_position_gain_pct: float = 1.0
    low_zone_pct: float = 0.45
    min_volume_ratio: float = 1.5
    early_trend_max_gain_pct: float = 0.60
    weak_market_skip_signals: bool = False
    weak_market_ma: int = 60


@dataclass(frozen=True)
class BoxBreakoutConfig:
    """箱体突破 + 红肥绿瘦。"""
    enabled: bool = True
    box_days: int = 40
    box_max_range_pct: float = 0.25
    converge_days: int = 5
    converge_ratio: float = 0.6
    red_green_vol_ratio: float = 1.3
    min_breakout_vol_ratio: float = 2.0
    vol_ma_days: int = 5


@dataclass(frozen=True)
class EngulfingConfig:
    """阳包阴反包启动。"""
    enabled: bool = True
    max_pullback_ratio: float = 0.30
    pullback_vol_ratio: float = 0.8
    engulf_min_vol_ratio: float = 1.2
    vol_ma_days: int = 5


@dataclass(frozen=True)
class LimitUpGapConfig:
    """涨停 + 跳空缺口共振（封单金额无法从免费行情源获取，不做核验）。"""
    enabled: bool = True
    lookback_days: int = 20
    gap_hold_days: int = 3
    limit_tolerance_pct: float = 0.003
    pressure_lookback_days: int = 60
    pullback_vol_ratio: float = 0.5


@dataclass(frozen=True)
class DragonConfig:
    """龙回头二次启动。"""
    enabled: bool = True
    wave_lookback_days: int = 60
    min_first_wave_pct: float = 0.20
    max_pullback_pct: float = 0.30
    pullback_vol_ratio: float = 0.30
    pullback_ma_tolerance: float = 0.05
    min_second_vol_ratio: float = 2.3
    prior_high_tolerance: float = 0.98
    vol_ma_days: int = 20


@dataclass(frozen=True)
class MaDivergenceConfig:
    """均线多头发散 + 量能突破。"""
    enabled: bool = True
    ma_fast: int = 20
    ma_slow: int = 60
    cross_lookback_days: int = 60
    divergence_hold_days: int = 7
    pullback_ma_tolerance: float = 0.03
    min_breakout_vol_ratio: float = 2.0
    vol_ma_days: int = 50
    breakout_lookback_days: int = 20


@dataclass(frozen=True)
class ShadowTestConfig:
    """低位仙人指路（长上影试盘）。"""
    enabled: bool = True
    min_shadow_ratio: float = 2.0
    max_prior_gain_pct: float = 0.30
    shadow_vol_ratio: float = 1.2
    support_days: int = 5
    vol_ma_days: int = 5


@dataclass(frozen=True)
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    support_retest: SupportConfig = field(default_factory=SupportConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    box_breakout: BoxBreakoutConfig = field(default_factory=BoxBreakoutConfig)
    bullish_engulfing: EngulfingConfig = field(default_factory=EngulfingConfig)
    limitup_gap: LimitUpGapConfig = field(default_factory=LimitUpGapConfig)
    dragon_pullback: DragonConfig = field(default_factory=DragonConfig)
    ma_divergence: MaDivergenceConfig = field(default_factory=MaDivergenceConfig)
    low_shadow: ShadowTestConfig = field(default_factory=ShadowTestConfig)


def load(path: str | Path) -> Config:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Config(
        scan=ScanConfig(**raw.get("scan", {})),
        support_retest=SupportConfig(**raw.get("support_retest", {})),
        breakout=BreakoutConfig(**raw.get("breakout", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        box_breakout=BoxBreakoutConfig(**raw.get("box_breakout", {})),
        bullish_engulfing=EngulfingConfig(**raw.get("bullish_engulfing", {})),
        limitup_gap=LimitUpGapConfig(**raw.get("limitup_gap", {})),
        dragon_pullback=DragonConfig(**raw.get("dragon_pullback", {})),
        ma_divergence=MaDivergenceConfig(**raw.get("ma_divergence", {})),
        low_shadow=ShadowTestConfig(**raw.get("low_shadow", {})),
    )
