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
class RiskConfig:
    """共性风控：6 个月涨幅上限。

    低位分位（low_zone_pct）与量能门槛（min_volume_ratio）已随旧形态一起删除；
    本信号要求价格已在上升结构中，低位闸门与定义直接冲突。
    """
    enabled: bool = True
    position_lookback_days: int = 120
    max_position_gain_pct: float = 1.0
    weak_market_skip_signals: bool = False
    weak_market_ma: int = 60


@dataclass(frozen=True)
class ClearUptrendConfig:
    """清晰上升趋势：价格沿上行的 MA20/MA60 规整抬升，且非打板情绪驱动。

    判定分三组（见 spec）：
      趋势——收盘 > MA20 > MA60、MA20 上行、60 日涨幅下限、偏离 MA20 上限；
      K 线老实——效率比 ER（净涨幅/路径总长）、日涨跌 p95、60 日最大回撤；
      非情绪——近 limitup_lookback 日涨停次数上限。
    只叠 position_ok，不叠任何低位闸门（本形态要求价格已在上升结构中）。
    """
    enabled: bool = True
    window: int = 60                 # 判定窗口（交易日）
    ma20_slope_days: int = 10        # MA20 上行的回看天数
    min_gain_60: float = 0.10        # 窗口净涨幅下限
    max_ext_pct: float = 0.08        # 收盘相对 MA20 的最大偏离（不追高）
    min_efficiency: float = 0.18     # 效率比下限
    max_daily_p95: float = 0.07      # 日涨跌幅绝对值 95 分位上限
    max_drawdown: float = 0.15       # 窗口内最大回撤上限（正值）
    limitup_lookback: int = 20       # 涨停统计窗口
    max_limit_ups: int = 1           # 允许的涨停次数
    limit_tolerance: float = 0.005   # 涨停判定容差
    require_higher_swings: bool = False  # 是否强制三段低点/高点同步抬高
    min_history_days: int = 150


@dataclass(frozen=True)
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    clear_uptrend: ClearUptrendConfig = field(default_factory=ClearUptrendConfig)


def load(path: str | Path) -> Config:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Config(
        scan=ScanConfig(**raw.get("scan", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        clear_uptrend=ClearUptrendConfig(**raw.get("clear_uptrend", {})),
    )
