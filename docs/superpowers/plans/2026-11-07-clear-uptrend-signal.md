# 清晰上升趋势 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 13 个形态信号收敛为单一「清晰上升趋势」，删除利好新闻加权与月度主线。

**Architecture:** 在 `signals.py` 中新增一个两段式检测器——`uptrend_metrics()` 只算原始指标、`clear_uptrend()` 只做阈值判断与打分，便于逐项构造反例测试。随后把 `scan_frame()` 从 13 次调用收敛为 1 次，再删除全部旧检测器、配置、前端分支与主线/新闻功能。

**Tech Stack:** Python 3.11、pandas、numpy、pytest；前端为原生 JS（无构建步骤），GitHub Pages 静态托管。

**Spec:** `docs/superpowers/specs/2026-11-07-clear-uptrend-signal-design.md`

## Global Constraints

- 信号名固定为 `清晰上升趋势`；前端 kind `uptrend`；计数键 `clear_uptrend`；配置块 `clear_uptrend:`；配置类 `ClearUptrendConfig`；检测函数 `clear_uptrend(frame, cfg, risk, limit_pct)`。
- 参数默认值必须与 spec 3.2 完全一致：`window=60`、`ma20_slope_days=10`、`min_gain_60=0.10`、`max_ext_pct=0.08`、`min_efficiency=0.18`、`max_daily_p95=0.07`、`max_drawdown=0.15`、`limitup_lookback=20`、`max_limit_ups=1`、`limit_tolerance=0.005`、`require_higher_swings=false`、`min_history_days=150`。
- 只叠 `position_ok`，**不得**调用 `low_zone_ok`（该函数将被删除）。
- 历史归档 `docs/data/archive/*.json` 一律不重写。
- 仓库文件为 CRLF；新建/改写文件必须保持 CRLF、无裸 LF。
- 本地测试命令固定为 `.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`。
- 所有任务只在本地提交；**推送统一放在 Task 8**，避免云端 15:20 扫描读到半成品。

## Review Focus

- **价格序列含零或负值**（停牌占位/脏数据）：`seg[0] <= 0` 时必须返回 `None` 而不是抛 `ZeroDivisionError`。
- **窗口不足 61 根但 ≥ `min_history_days`**：`seg` 长度不足时不得让 `np.diff` 越界。
- **停牌后一根跳空拉起**：前复权日线里表现为单根巨跳。`p95` 抓不到它（60 个样本里 1 个异常值的 95 分位仍是 0），真正拦截的是 `ext`（收盘瞬间偏离 MA20 超限）。必须有测试钉住这条，否则会误以为 p95 已兜底。
- **涨停容差与板块上限**：创业板/科创板 20%、北交所 30%、主板 10%；容差 0.5% 用于吸收复权与四舍五入误差。
- **归档页仍含旧信号名**：`signalKind()` 必须继续回落 `other`，不能让归档视图抛错。

---

### Task 1: 新增 `ClearUptrendConfig` 与指标计算（TDD）

**Files:**
- Create: `tests/test_clear_uptrend.py`
- Modify: `src/ashare_monitor/config.py`
- Modify: `src/ashare_monitor/signals.py`

**Interfaces:**
- Produces:
  - `ClearUptrendConfig`（dataclass，字段见 Global Constraints）
  - `uptrend_metrics(frame: pd.DataFrame, cfg: ClearUptrendConfig, limit_pct: float) -> dict | None`
  - 返回键：`px, ma20, ma60, ma20_prev, gain_60, ext, er, p95, mdd, limit_ups, higher_lows, higher_highs`
- Consumes: 现有的 `Config`/`load()` 结构。

- [ ] **Step 1: 写失败测试（指标层）**

创建 `tests/test_clear_uptrend.py`：

```python
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_monitor.config import ClearUptrendConfig
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'ClearUptrendConfig'`

- [ ] **Step 3: 新增配置类**

在 `src/ashare_monitor/config.py` 的 `@dataclass(frozen=True)\nclass Config:` 之前插入：

```python
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
```

- [ ] **Step 4: 新增指标函数**

在 `src/ashare_monitor/signals.py` 中、`def position_strategy(` 之前插入：

```python
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
    blocks = [seg[i * block:(i + 1) * block] for i in range(3)]
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
```

同时在 `signals.py` 顶部 `from .config import (...)` 中加入 `ClearUptrendConfig,`。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: `3 passed`

- [ ] **Step 6: 提交**

```bash
git add tests/test_clear_uptrend.py src/ashare_monitor/config.py src/ashare_monitor/signals.py
git commit -m "feat: 新增清晰上升趋势的指标计算层"
```

---

### Task 2: 新增 `clear_uptrend` 判定与逐项反例（TDD）

**Files:**
- Modify: `tests/test_clear_uptrend.py`
- Modify: `src/ashare_monitor/signals.py`

**Interfaces:**
- Consumes: `uptrend_metrics()`、`position_ok()`
- Produces: `clear_uptrend(frame, cfg, risk, limit_pct=0.10) -> dict | None`，返回行含
  `signal, score, close, volume, er, gain_60_pct, mdd_pct, ext_pct, ma20, ma60, limit_ups, higher_lows, higher_highs, today_high, today_low, note`

- [ ] **Step 1: 追加失败测试**

在 `tests/test_clear_uptrend.py` 末尾追加：

```python
from ashare_monitor.config import RiskConfig

RISK = RiskConfig()


def assert_rejected_by(close: np.ndarray, key: str, predicate) -> None:
    """先证明反例确实违反了目标指标，再证明检测器拒绝——防止因别的原因被拒。"""
    frame = build(close)
    m = uptrend_metrics(frame, CFG, 0.10)
    assert m is not None, "反例样本本身长度必须足够"
    assert predicate(m[key]), f"反例没有违反 {key}: {m[key]}"
    assert clear_uptrend(frame, CFG, RISK) is None


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
    assert_rejected_by(close, "px", lambda v: v < 8.0)


def test_rejects_ma20_not_rising() -> None:
    close = uptrend()
    close[-10:] = close[-11]          # 末段横住，MA20 不再上行
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
    close[-33] = close[-34] * 1.10
    m_main = uptrend_metrics(build(close), CFG, 0.10)
    m_gem = uptrend_metrics(build(close), CFG, 0.20)
    assert m_main is not None and m_gem is not None
    assert m_main["limit_ups"] >= 1
    assert m_gem["limit_ups"] == 0


def test_rejects_short_history() -> None:
    assert clear_uptrend(build(np.full(100, 8.0)), CFG, RISK) is None


def test_rejects_when_disabled() -> None:
    assert clear_uptrend(build(uptrend()), ClearUptrendConfig(enabled=False), RISK) is None
```


- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'clear_uptrend'`

- [ ] **Step 3: 实现判定函数**

在 `signals.py` 的 `uptrend_metrics` 之后插入：

```python
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
```

- [ ] **Step 4: 跑测试并修正合成样本**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: 全部 PASS。若某个反例的 `assert predicate(...)` 不成立，说明合成样本没造对该缺陷——
调整该样本的数值直到 pred 成立，**不要**放宽 predicate。

- [ ] **Step 5: 提交**

```bash
git add tests/test_clear_uptrend.py src/ashare_monitor/signals.py
git commit -m "feat: 清晰上升趋势判定与逐项反例测试"
```

---

### Task 3: `scan_frame` 收敛为单一信号

**Files:**
- Modify: `src/ashare_monitor/signals.py`
- Modify: `src/ashare_monitor/cli.py`
- Test: `tests/test_clear_uptrend.py`

**Interfaces:**
- Produces: `scan_frame(frame, uptrend_cfg, risk, limit_pct=0.10) -> list[dict]`
- Consumes: `clear_uptrend`

- [ ] **Step 1: 写失败测试**

在 `tests/test_clear_uptrend.py` 追加：

```python
def test_scan_frame_returns_single_signal() -> None:
    from ashare_monitor.signals import scan_frame
    rows = scan_frame(build(uptrend()), CFG, RISK, limit_pct=0.10)
    assert len(rows) == 1
    assert rows[0]["signal"] == "清晰上升趋势"


def test_scan_frame_returns_empty_when_disabled() -> None:
    from ashare_monitor.signals import scan_frame
    rows = scan_frame(build(uptrend()), ClearUptrendConfig(enabled=False), RISK)
    assert rows == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: FAIL — `TypeError: scan_frame() missing ...` 或参数数量不符

- [ ] **Step 3: 替换 `scan_frame`**

把 `signals.py` 中从 `def scan_frame(` 到文件末尾的整段替换为：

```python
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
```

- [ ] **Step 4: 更新 `cli.py` 的调用**

把 `cli.py` 中 `_scan_one` 里的：

```python
    rows = scan_frame(frame, cfg.support_retest, cfg.breakout, cfg.risk,
                      cfg.box_breakout, cfg.bullish_engulfing, cfg.limitup_gap,
                      cfg.dragon_pullback, cfg.ma_divergence, cfg.low_shadow,
                      cfg.oversold_reversal, cfg.break_ma20, cfg.boll_pin,
                      cfg.ene_pullback, cfg.deep_base,
                      limit_pct=limit_pct, deep_frame=deep_frame)
```

替换为：

```python
    rows = scan_frame(frame, cfg.clear_uptrend, cfg.risk, limit_pct=limit_pct)
```

同时把上方取帧逻辑：

```python
    history = history_for(symbol, cache_dir, cfg.scan.deep_lookback_days, as_of=day)
    ...
    deep_frame = history[history.index <= pd.Timestamp(day)]
    frame = deep_frame.tail(cfg.scan.lookback_days)
```

替换为：

```python
    history = history_for(symbol, cache_dir, cfg.scan.lookback_days, as_of=day)
    ...
    frame = history[history.index <= pd.Timestamp(day)]
```

- [ ] **Step 5: 在 `config.py` 中挂上新配置**

在 `Config` 中把 `deep_base: DeepBaseConfig = ...` 之后追加：

```python
    clear_uptrend: ClearUptrendConfig = field(default_factory=ClearUptrendConfig)
```

在 `load()` 中追加：

```python
        clear_uptrend=ClearUptrendConfig(**raw.get("clear_uptrend", {})),
```

并在 `ScanConfig` 中删除 `deep_lookback_days`。

- [ ] **Step 6: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clear_uptrend.py -q -p no:cacheprovider`
Expected: PASS

Run: `.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'src');from ashare_monitor.config import load;print(load('config.yaml').clear_uptrend)"`
Expected: 打印 `ClearUptrendConfig(...)`（此时 `config.yaml` 还没有该块，
`raw.get("clear_uptrend", {})` 返回空字典，全部落到默认值——这是预期行为）。

- [ ] **Step 7: 提交**

```bash
git add src/ashare_monitor/signals.py src/ashare_monitor/cli.py src/ashare_monitor/config.py tests/test_clear_uptrend.py
git commit -m "refactor: scan_frame 收敛为单一趋势信号"
```

---

### Task 4: 删除 13 个旧检测器、配置类与测试

**Files:**
- Modify: `src/ashare_monitor/signals.py`
- Modify: `src/ashare_monitor/config.py`
- Delete: `tests/test_patterns.py`
- Delete: `tests/test_deep_base.py`
- Modify: `config.yaml`, `config.example.yaml`

**Interfaces:**
- 删除后 `signals.py` 只保留：`_base_row`、`position_ok`、`uptrend_metrics`、`clear_uptrend`、`position_strategy`、`entry_exit_plan`、`scan_frame`

- [ ] **Step 1: 删除 `signals.py` 中的旧检测器**

删除以下函数（每个函数从 `def X(` 起到下一个顶层 `def` 前）：

`box_breakout_bullish`、`bullish_engulfing`、`limitup_gap`、`dragon_pullback`、
`ma_divergence_breakout`、`low_shadow_test`、`support_retest`、`sideways_breakout`、
`_rsi`、`_ema`、`_macd`、`oversold_reversal`、`break_ma20`、`boll_lower_pin`、
`ene_lower_touch`、`zigzag_pivots`、`deep_base_retest`、`low_zone_ok`、`volume_valid`、
`_vol_ma`、`_gain_over`

保留 `_base_row`、`position_ok`、`position_strategy`、`entry_exit_plan`、`scan_frame`。

- [ ] **Step 2: 清理 import**

`signals.py` 顶部只保留需要的：

```python
from .config import ClearUptrendConfig, RiskConfig
```

- [ ] **Step 3: 给 `entry_exit_plan` 加新分支**

在 `entry_exit_plan` 的分支链最前面（`if kind == "回踩前期起涨位":` 之前）插入：

```python
    if kind == "清晰上升趋势":
        m20 = row.get("ma20")
        if m20:
            entry, stop = round(close, 2), round(m20 * 0.97, 2)
            if entry > stop:
                target = round(entry + 3 * (entry - stop), 2)
                note = "趋势确认后买入；收盘跌破 MA20（趋势结构破坏）止损"
```

然后把原来的 `if kind == "回踩前期起涨位":` 改为 `elif kind == "回踩前期起涨位":`，
并删除其余所有 `elif` 分支（`横盘后放量突破` … `深跌筑底回踩前高`）。

- [ ] **Step 4: 删除 `config.py` 中的旧配置类**

删除：`SupportConfig`、`BreakoutConfig`、`BoxBreakoutConfig`、`EngulfingConfig`、
`LimitUpGapConfig`、`DragonConfig`、`MaDivergenceConfig`、`ShadowTestConfig`、
`OversoldReversalConfig`、`BreakMa20Config`、`BollPinConfig`、`EneConfig`、`DeepBaseConfig`。

`Config` 只保留：

```python
@dataclass(frozen=True)
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    clear_uptrend: ClearUptrendConfig = field(default_factory=ClearUptrendConfig)
```

`load()` 只解析 `scan`、`risk`、`clear_uptrend`。

> `notify:` 配置块**不要删**：它由 `scripts/notify.py` 直接读 YAML（`_notify_cfg()`），
> 不经过 `Config` 类。`config.py` 里从来没有 `NotifyConfig`。

- [ ] **Step 5: 清理 `RiskConfig` 死字段**

先确认无引用：

```bash
grep -rn "low_zone_pct\|min_volume_ratio\|early_trend_max_gain_pct" --include="*.py" src scripts
```

预期只剩 `config.py` 定义处。确认后从 `RiskConfig` 删除这三个字段。
保留 `position_lookback_days`、`max_position_gain_pct`、`weak_market_skip_signals`、`weak_market_ma`、`enabled`。

- [ ] **Step 6: 删除旧测试**

```bash
git rm tests/test_patterns.py tests/test_deep_base.py tests/test_signals.py
```

> `test_signals.py` 同样 import 了 `SupportConfig` / `sideways_breakout` / `support_retest`，
> 不删会导致整个测试目录 collect 失败。

- [ ] **Step 7: 重写配置文件**

`config.yaml` 与 `config.example.yaml` 都替换为：

```yaml
scan:
  workers: 8
  lookback_days: 320
  min_history_days: 150
  exclude_st: true
  include_boards: ["主板", "创业板", "科创板", "北交所"]
  cache_dir: "data/cache"
  output_dir: "data/reports"

# 共性风控：6 个月涨幅上限（本信号仍在使用）
risk:
  enabled: true
  position_lookback_days: 120
  max_position_gain_pct: 1.0
  weak_market_skip_signals: false
  weak_market_ma: 60

# 清晰上升趋势
clear_uptrend:
  enabled: true
  window: 60                 # 判定窗口（交易日）
  ma20_slope_days: 10        # MA20 上行的回看天数
  min_gain_60: 0.10          # 窗口净涨幅下限
  max_ext_pct: 0.08          # 收盘相对 MA20 的最大偏离（不追高）
  min_efficiency: 0.18       # 效率比下限（净涨幅 / 路径总长）
  max_daily_p95: 0.07        # 日涨跌幅绝对值 95 分位上限
  max_drawdown: 0.15         # 窗口内最大回撤上限
  limitup_lookback: 20       # 涨停统计窗口
  max_limit_ups: 1           # 允许的涨停次数
  limit_tolerance: 0.005     # 涨停判定容差
  require_higher_swings: false  # 是否强制三段低点/高点同步抬高
  min_history_days: 150
```

> `notify` 块：以当前 `config.yaml` 里已有的 `notify:` 原文为准，原样保留；上面为简洁未展开。

- [ ] **Step 8: 跑测试与导入检查**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`
Expected: PASS（只剩 `test_clear_uptrend.py` 与 `test_data_adjust.py`）

Run: `.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'src');from ashare_monitor.config import load;c=load('config.yaml');print(c.clear_uptrend)"`
Expected: 打印配置对象

- [ ] **Step 9: 提交**

```bash
git add -A src/ashare_monitor tests config.yaml config.example.yaml
git commit -m "refactor: 删除 13 个形态检测器与配置，只保留清晰上升趋势"
```

---

### Task 5: 管线与导出收敛

**Files:**
- Modify: `scripts/refresh_signals.py`
- Modify: `scripts/export_dashboard.py`
- Modify: `src/ashare_monitor/cli.py`

**Interfaces:**
- `SIGNAL_COUNT_KEYS` 变为单键
- `refresh_signals.py` 直接调用 `clear_uptrend`

- [ ] **Step 1: `export_dashboard.py` 收敛计数键**

把 `SIGNAL_COUNT_KEYS` 整块替换为：

```python
SIGNAL_COUNT_KEYS = {
    "清晰上升趋势": "clear_uptrend",
}
```

删除 `build_payload` 中的：

```python
    support = counts["support_retest"]
    breakout = counts["sideways_breakout"]
```

- [ ] **Step 2: `refresh_signals.py` 收敛检测器注册**

删除 `NAME2KEY`、`DETECTORS` 两个字典，import 改为：

```python
from ashare_monitor.signals import clear_uptrend, entry_exit_plan, position_strategy
```

新增板块涨跌停映射（与 `cli.py` 保持一致）：

```python
LIMIT_PCT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}
```

把扫描循环中的：

```python
        for sig_name, (det, cfg_key, deep_window) in DETECTORS.items():
            row = det(frame if deep_window else short, getattr(cfg, cfg_key), cfg.risk)
            if not row:
                continue
```

替换为：

```python
        limit_pct = LIMIT_PCT_BY_BOARD.get(board_of(sym), 0.10)
        row = clear_uptrend(frame, cfg.clear_uptrend, cfg.risk, limit_pct)
        if row:
```

> 注意缩进：原来 `if not row: continue` 下面的逻辑现在位于 `if row:` 块内，
> 需要整体缩进一级。实现后必须用 `python -c "import ast;ast.parse(open(...).read())"` 校验语法。

把计数段：

```python
    counts = {v: 0 for v in NAME2KEY.values()}
    for s in latest["signals"]:
        k = NAME2KEY.get(s.get("signal"))
        if k:
            counts[k] += 1
```

替换为：

```python
    counts = {"clear_uptrend": sum(1 for s in latest["signals"]
                                   if s.get("signal") == "清晰上升趋势")}
```

- [ ] **Step 3: `cli.py` 收敛 CSV 列**

把 `columns = [...]` 整块替换为：

```python
    columns = ["symbol", "name", "board", "signal", "score", "sort_score", "close", "volume",
               "market_env", "scan_time", "note", "mf_date", "net_mf_amount",
               "position_pct", "position_tier", "position_reason",
               "entry_price", "stop_loss", "take_profit", "risk_reward", "entry_state", "plan_note",
               "er", "gain_60_pct", "mdd_pct", "ext_pct", "ma20", "ma60",
               "limit_ups", "higher_lows", "higher_highs", "today_high", "today_low"]
```

- [ ] **Step 4: 语法与导入检查**

```bash
.venv/Scripts/python.exe -c "import ast;[ast.parse(open(f,encoding='utf-8').read()) for f in ('scripts/refresh_signals.py','scripts/export_dashboard.py','src/ashare_monitor/cli.py')];print('syntax ok')"
.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'src');sys.path.insert(0,'scripts');import refresh_signals, export_dashboard;print('imports ok', export_dashboard.SIGNAL_COUNT_KEYS)"
```

Expected: `syntax ok` / `imports ok {'清晰上升趋势': 'clear_uptrend'}`

- [ ] **Step 5: 提交**

```bash
git add scripts/refresh_signals.py scripts/export_dashboard.py src/ashare_monitor/cli.py
git commit -m "refactor: 管线与导出只保留单一信号"
```

---

### Task 6: 删除利好新闻加权

**Files:**
- Delete: `src/ashare_monitor/news_factor.py`
- Modify: `src/ashare_monitor/cli.py`
- Delete: `tests/test_news.py`（若存在；否则跳过）

- [ ] **Step 1: 删除 cli.py 中的新闻调用**

删除 import：

```python
from .news_factor import news_counts, sort_score
```

删除整段：

```python
    # 利好消息因子：每条信号附加近3日利好新闻数（东财公开资讯，文本非行情源）
    news = news_counts([str(s["symbol"]) for s in signals],
                       Path(cfg.scan.cache_dir).parent / "news_cache")
    for row in signals:
        info = news.get(str(row["symbol"]), {})
        row["news_count"] = info.get("news_count", 0)
        row["news_total"] = info.get("news_total", 0)
        row["top_headline"] = info.get("top_headline", "")
        row["sort_score"] = sort_score(float(row.get("score") or 0), int(row.get("news_count") or 0))
```

替换为：

```python
    for row in signals:
        row["sort_score"] = float(row.get("score") or 0)
```

- [ ] **Step 2: 删除模块与测试**

```bash
git rm src/ashare_monitor/news_factor.py
grep -rn "news_factor" --include="*.py" src scripts tests || echo "no refs"
```

- [ ] **Step 3: 验证**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'src');import ashare_monitor.cli;print('cli ok')"
```

Expected: 全部 PASS / `cli ok`

- [ ] **Step 4: 提交**

```bash
git add -A src/ashare_monitor scripts
git commit -m "refactor: 删除利好消息加权"
```

---

### Task 7: 删除月度主线（脚本 + 通知 + 计划任务）

**Files:**
- Delete: `scripts/monthly_mainline.py`、`scripts/mainline_backtest.py`、`scripts/sector_backtest.py`
- Delete: `scripts/run-mainline.ps1`、`scripts/install-mainline-schedule.ps1`
- Modify: `scripts/daily_notify.py`、`scripts/publish-site.ps1`

- [ ] **Step 1: 删除主线脚本**

```bash
git rm scripts/monthly_mainline.py scripts/mainline_backtest.py scripts/sector_backtest.py
git rm scripts/run-mainline.ps1 scripts/install-mainline-schedule.ps1
```

- [ ] **Step 2: 删除通知里的主线文案**

`scripts/daily_notify.py` 中删除：

```python
    # 主线板块（若有）
    mainline_path = ROOT / "docs" / "data" / "mainline.json"
    mainline = ""
    if mainline_path.exists():
        try:
            ml = json.loads(mainline_path.read_text(encoding="utf-8"))
            sectors = [s.get("industry", "") for s in ml.get("top_sectors", [])[:3]]
            if sectors:
                mainline = f"\n\n**本月主线**：{' / '.join(sectors)}"
        except Exception:
            pass
```

并把 `f"**今日候选 Top**：\n{top_text}{mainline}\n\n"` 改为
`f"**今日候选 Top**：\n{top_text}\n\n"`。

- [ ] **Step 3: `publish-site.ps1` 去掉 mainline.json**

把 `git status --porcelain` 与 `git add` 两行中的 `docs/data/mainline.json` 删掉，
其余参数保持不变。改完后执行：

```bash
grep -n "mainline" scripts/*.ps1 scripts/*.py || echo "no mainline refs"
```

Expected: `no mainline refs`

- [ ] **Step 4: 删除 Windows 计划任务**

先核对真实任务名（不要凭记忆）：

```bash
schtasks /Query /FO LIST 2>/dev/null | grep -i "ashare" 
```

确认输出中含 `AshareMonthlyMainline` 后执行：

```bash
schtasks /Delete /TN "AshareMonthlyMainline" /F
```

再查一次确认已消失。

- [ ] **Step 5: 提交**

```bash
git add -A scripts
git commit -m "refactor: 删除月度主线功能与计划任务"
```

---

### Task 8: 前端改造

**Files:**
- Modify: `docs/app.js`
- Modify: `docs/styles.css`
- Modify: `docs/index.html`

- [ ] **Step 1: `SIGNAL_KINDS` 收敛**

```javascript
  const SIGNAL_KINDS = {
    "清晰上升趋势": "uptrend",
  };
```

删除 `EXTRA_KINDS` 数组与 `renderExtraChips` 函数，以及调用它的位置。

- [ ] **Step 2: 删除信号类型筛选与 chip 容器**

`docs/index.html`：
- 删除 `<div class="filter-row filter-row-chips" id="extra-filter-chips" ...></div>`
- 删除 `<legend>信号类型</legend>` 所在的整个筛选块（`data-filter="support"` / `"breakout"` 两个按钮）

`docs/app.js`：
- 删除 `$("#filter-support-count")` 与 `$("#filter-breakout-count")` 两行赋值
- 删除 `state.signal` 相关的过滤条件，改为仅按代码/名称关键词过滤：
  把 `const kindMatch = state.signal === "all" || signalKind(item.signal) === state.signal;`
  改为 `const kindMatch = true;`

- [ ] **Step 3: 顶部 KPI 卡片改为单一信号**

`docs/index.html` 把 `support-count` 与 `breakout-count` 两张卡片替换为一张：

```html
        <article class="kpi kpi-uptrend">
          <span class="kpi-index" aria-hidden="true">02</span>
          <span class="kpi-label">清晰上升趋势</span>
          <strong class="kpi-value" id="uptrend-count">—</strong>
          <small>趋势结构 + K线规整 + 非打板情绪</small>
        </article>
```

`docs/app.js` 把两行赋值替换为：

```javascript
    $("#uptrend-count").textContent = hasData ? number.format(Number(counts.clear_uptrend || 0)) : "—";
```

并把数组 `["#universe-count", "#support-count", "#breakout-count", "#failure-count"]`
改为 `["#universe-count", "#uptrend-count", "#failure-count"]`。

- [ ] **Step 4: 删除月度主线前端**

`docs/index.html`：删除 `<section class="mainline" id="mainline" ...> ... </section>` 整块。
`docs/app.js`：删除 `/* ---------- 月度主线板块 ---------- */` 起的整段（`MAINLINE_URL`、
`mainlineCache`、渲染函数与它的调用）。
`docs/styles.css`：删除所有 `.mainline*` 规则。

- [ ] **Step 5: 指标行与弹窗改为新字段**

`metricLines()` 整段替换为：

```javascript
  function metricLines(item) {
    const er = num(item.er);
    return [
      { text: `趋势效率 ${er === null ? "—" : er.toFixed(3)}`, cls: er !== null && er >= 0.25 ? "is-up" : "" },
      { text: `60日涨幅 ${signedPct(item.gain_60_pct)}`, cls: signClass(item.gain_60_pct) },
      { text: `最大回撤 ${signedPct(item.mdd_pct)}`, cls: signClass(item.mdd_pct) },
      { text: `距MA20 ${signedPct(item.ext_pct)}`, cls: signClass(item.ext_pct) },
      { text: `近20日涨停 ${cleanText(item.limit_ups)} 次`, cls: num(item.limit_ups) === 0 ? "is-up" : "" },
    ];
  }
```

`dialogRows()` 中删除全部 `kind === ...` 分支，追加：

```javascript
    if (kind === "uptrend") {
      rows.push(
        { dt: "趋势效率（ER）", dd: cleanText(item.er) },
        { dt: "60日涨幅", dd: signedPct(item.gain_60_pct), cls: signClass(item.gain_60_pct) },
        { dt: "60日最大回撤", dd: signedPct(item.mdd_pct), cls: signClass(item.mdd_pct) },
        { dt: "距 MA20", dd: signedPct(item.ext_pct), cls: signClass(item.ext_pct) },
        { dt: "MA20 / MA60", dd: `${cleanText(item.ma20)} / ${cleanText(item.ma60)}` },
        { dt: "近20日涨停", dd: `${cleanText(item.limit_ups)} 次` },
        { dt: "结构（低点/高点抬高）", dd: `${item.higher_lows ? "是" : "否"} / ${item.higher_highs ? "是" : "否"}` },
        { dt: "当日高 / 低", dd: `${cleanText(item.today_high)} / ${cleanText(item.today_low)}` },
      );
    }
```

删除 `news_count` / `news_total` / `top_headline` 的渲染分支。

- [ ] **Step 6: 配色**

`docs/styles.css` 顶部变量区删除 `--support*`、`--breakout*`、`--boll*`、`--ene*`、`--deepbase*` 各组，
新增：

```css
  --uptrend: #3fb6a8; /* 信号：清晰上升趋势 */
  --uptrend-bright: #5fd8c8;
  --uptrend-soft: rgba(63, 182, 168, 0.13);
  --uptrend-line: rgba(63, 182, 168, 0.45);
```

把 `.signal-support` … `.signal-deepbase` 全部替换为：

```css
.signal-uptrend {
  --acc: var(--uptrend);
  color: var(--uptrend-bright);
  border-color: var(--uptrend-line);
  background: var(--uptrend-soft);
}
```

- [ ] **Step 7: 缓存版本递增**

`docs/index.html` 中把 `?v=20`（或当前值）全部 +1。

- [ ] **Step 8: 语法检查与提交**

```bash
node --check docs/app.js && echo "app.js ok"
git add docs/app.js docs/styles.css docs/index.html
git commit -m "feat: 前端收敛为清晰上升趋势单一信号"
```

- [ ] **Step 9: 浏览器实测**

用本地静态服务器打开 `docs/`，确认：列表渲染、详情弹窗、K 线、归档切换均无 console error。
若失败，记录报错原文并修复后重新提交。

---

### Task 9: 文档更新与全链路验收

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 重写 README 信号表**

把原「信号一览」整表替换为单行：

```markdown
| 信号 | 必要条件 | 失效/注意点 |
|---|---|---|
| 清晰上升趋势 | 收盘 > MA20 > MA60，MA20 近 10 日上行，60 日涨幅 ≥ 10%，偏离 MA20 ≤ 8%（`clear_uptrend.*`）；60 日效率比 ER ≥ 0.18、日涨跌 p95 < 7%、最大回撤 > -15%；近 20 日涨停 ≤ 1 次 | 只叠 6 个月涨幅上限，不叠低位闸门；收盘跌破 MA20 即趋势结构破坏 |
```

删除 README 中「利好消息加权」「月度主线」相关章节。

- [ ] **Step 2: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`
Expected: 全部 PASS

- [ ] **Step 3: 真数据验收（不进 pytest，因 data/ 未跟踪）**

```bash
.venv/Scripts/python.exe -c "
import sys, glob, os; sys.path.insert(0,'src')
import pandas as pd
from ashare_monitor.config import load
from ashare_monitor.signals import scan_frame
cfg = load('config.yaml')
LP = {'主板':0.10,'创业板':0.20,'科创板':0.20,'北交所':0.30}
def board(s):
    if s.startswith(('688','689')): return '科创板'
    if s.startswith(('300','301','302')): return '创业板'
    if s.startswith(('8','43','92')): return '北交所'
    return '主板'
hits=[]
for f in sorted(glob.glob('data/cache/*.csv')):
    sym=os.path.basename(f)[:6]
    df=pd.read_csv(f,parse_dates=['date']).sort_values('date').set_index('date')
    df=df[~df.index.duplicated(keep='last')].dropna(subset=['open','high','low','close','volume'])
    if len(df)<150 or df.index[-1].strftime('%Y%m%d')<'20261001': continue
    rows=scan_frame(df.tail(cfg.scan.lookback_days), cfg.clear_uptrend, cfg.risk, LP[board(sym)])
    hits += [r['close'] and sym for r in rows]
print('hits', len(hits)); print(sorted(hits))
"
```

Expected: 用 2026-11-05 缓存跑出 **28 只**，与 spec 3.5 的名单一致（ST 剔除前）。
若数量不符，先查是否误用了 `lookback_days` 以外的窗口或涨停容差。

- [ ] **Step 4: 导出验收**

跑一次本地扫描后确认 `docs/data/latest.json`：

```bash
python -c "import json;d=json.load(open('docs/data/latest.json',encoding='utf-8'));print(d['signal_counts']);print({s['signal'] for s in d['signals']})"
```

Expected: `{'clear_uptrend': N}` 且信号集合只有 `清晰上升趋势`。

- [ ] **Step 5: 提交并推送**

```bash
git add README.md
git commit -m "docs: 更新为单一趋势信号说明"
git push origin main
```

> 推送前先 `git fetch origin && git log --oneline HEAD..origin/main` 确认没有新的云端提交；
> 若有，先 `git rebase origin/main` 再推。**不要** rebase 任何 `docs/data` 冲突提交。

---

## 验收标准

1. `pytest tests/ -q -p no:cacheprovider` 全绿（此时只剩 `test_clear_uptrend.py` 与 `test_data_adjust.py`）
2. 2026-11-05 缓存全量扫描命中 28 只，与 spec 名单一致
3. `signal_counts` 只有 `clear_uptrend` 一个键
4. 浏览器实测无 console error
5. `schtasks /Query` 中不再有 `AshareMonthlyMainline`
6. `grep -rn "mainline\|news_factor" --include="*.py" src scripts` 无结果
