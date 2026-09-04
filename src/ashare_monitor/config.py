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
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    support_retest: SupportConfig = field(default_factory=SupportConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)


def load(path: str | Path) -> Config:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Config(
        scan=ScanConfig(**raw.get("scan", {})),
        support_retest=SupportConfig(**raw.get("support_retest", {})),
        breakout=BreakoutConfig(**raw.get("breakout", {})),
    )
