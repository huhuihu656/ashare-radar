# 设计：把 13 个形态信号收敛为单一「清晰上升趋势」

- 日期：2026-11-07
- 状态：待评审
- 决策人：用户

## 1. 背景与目标

用户要求：**「全部技术指标全部去掉，我现在只需要确认上涨趋势并且不是因为热点情绪而连续打板的股票，上涨 K 线要非常清晰标准且直观的」**。

现状是 13 个形态检测器并行，2026-11-04 当日产出 884 条信号，其中「回踩前期起涨位」274 条、「恰好突破20日线」174 条，噪声远大于信息量。

目标：只保留**一个**信号 `清晰上升趋势`，输出一份短、干净、形态规整的趋势名单。

成功标准：

1. 当日名单量级在 **20–40 只**；
2. 名单中不出现靠连续涨停炒作的情绪票；
3. 名单中的 K 线形态人工目视应呈「规整上行」，而非宽幅震荡或脉冲式上涨。

## 2. 范围

**做**：新增单一信号；删除 13 个旧信号；删除利好新闻加权；删除月度主线；删除对应的配置、前端分支、测试与文档。

**不做**：不改动价格缓存与复权口径；不改动云端扫描工作流；不改动历史归档文件（冻结快照，保留旧信号名）；不改动买卖点与参考仓位机制。

## 3. 信号定义

### 3.1 名称与标识

| 项 | 值 |
|---|---|
| 信号名 | `清晰上升趋势` |
| 前端 kind | `uptrend` |
| 计数键 | `clear_uptrend` |
| 配置块 | `clear_uptrend:` |
| 配置类 | `ClearUptrendConfig` |
| 检测函数 | `clear_uptrend(frame, cfg, risk) -> dict \| None` |

### 3.2 判定链（全部满足才命中）

以最后一根日线为判定日，`c` = 收盘价序列，窗口 `W = 60` 个交易日。

| # | 组 | 条件 | 配置字段 | 默认值 |
|---|---|---|---|---|
| 1 | 趋势 | 收盘 > MA20 > MA60 | — | — |
| 2 | 趋势 | MA20 相对 10 日前上行 | `ma20_slope_days` | 10 |
| 3 | 趋势 | 60 日涨幅 ≥ 10% | `min_gain_60` | 0.10 |
| 4 | 趋势 | 收盘相对 MA20 的偏离 ≤ 8% | `max_ext_pct` | 0.08 |
| 5 | K线老实 | 效率比 ER ≥ 0.18 | `min_efficiency` | 0.18 |
| 6 | K线老实 | 日涨跌幅绝对值 p95 < 7% | `max_daily_p95` | 0.07 |
| 7 | K线老实 | 60 日最大回撤 > -15% | `max_drawdown` | 0.15 |
| 8 | 非情绪 | 近 20 日涨停次数 ≤ 1 | `max_limit_ups` | 1 |
| 9 | 风控 | 6 个月涨幅 ≤ 100% | 复用 `risk.max_position_gain_pct` | 1.0 |
| 10 | 结构（可选） | 60 日均分 3 段，每段低点、高点均高于前段 | `require_higher_swings` | **false** |

**效率比（ER）** = `|c[-1] - c[-W-1]| / Σ|Δc|`，取值 0–1，越接近 1 表示越像一条直线。
实测「均线多头」样本内 ER 中位数 0.102、p90 0.225，故 0.18 约为前 15% 分位。

**60 日最大回撤** = `min_i( c[i] / max(c[0..i]) - 1 )`，即以窗口内滚动最高点为基准的最大跌幅，
恒为负值；判定条件写作 `mdd > -max_drawdown`。

**日涨跌 p95** = 窗口内 60 个 `c[i]/c[i-1]-1` 的绝对值取 95 分位。

**结构检查（可选）** = 把 60 日窗口等分为 3 段各 20 日，取每段的低点与高点；
要求 `低点[1] > 低点[0] && 低点[2] > 低点[1]` 且高点同理。默认关闭（开启后实测 28 → 16）。

**涨停判定**：按板块涨跌幅上限判定（主板 10%、创业板/科创板 20%、北交所 30%），
`当日涨幅 ≥ 上限 - 0.5%` 记一次；统计窗口为最近 20 个交易日。

**位置闸门**：只叠 `position_ok`，**不叠 `low_zone_ok`**——本信号要求价格处于上升结构，
与「120 日低位分位」直接冲突。

### 3.3 输出字段

```text
close, volume, signal, score,
er, gain_60_pct, mdd_pct, ext_pct, ma20, ma60,
limit_ups, higher_lows, higher_highs,
today_high, today_low, note
```

`score` = 100 ×（0.35×ER 归一 + 0.25×涨幅归一 + 0.20×回撤归一 + 0.20×涨停洁净度），各项截断到 [0,1]：

- ER 归一 = `min(er / 0.40, 1)`
- 涨幅归一 = `min(gain_60 / 0.40, 1)`
- 回撤归一 = `min(mdd_depth / 0.20, 1)`，`mdd_depth = -(mdd)`（回撤越浅得分越高）
- 涨停洁净度 = `1 - limit_ups / max(1, max_limit_ups+1)`，0 次涨停得满分

### 3.4 买卖点

- 买入参考 = 当日收盘价
- 止损 = `MA20 × 0.97`
- 止盈 = 买入 + 3 ×（买入 − 止损）
- 失效语义：收盘跌破 MA20 即趋势结构破坏

### 3.5 实测标定（2026-11-05 收盘数据，全市场 5470 只）

| 阶段 | 数量 |
|---|---|
| 全市场 | 5470 |
| 均线多头 + MA20 上行 | 495（9%） |
| 叠加全部条件（不含 ST 剔除） | 28 |
| 其中低点抬高 / 高点抬高 / 两者兼具 | 20 / 21 / 16 |
| 再叠 `position_ok` | 28（无剔除） |

生产环境经 `exclude_st` 剔除 `*ST顾地`、`ST人福` 后，预期 **26 只**。

## 4. 删除清单

### 4.1 信号检测器（`src/ashare_monitor/signals.py`）

`box_breakout_bullish`、`bullish_engulfing`、`limitup_gap`、`dragon_pullback`、
`ma_divergence_breakout`、`low_shadow_test`、`support_retest`、`sideways_breakout`、
`oversold_reversal`、`break_ma20`、`boll_lower_pin`、`ene_lower_touch`、`deep_base_retest`，
以及仅为其服务的 `_rsi`、`_ema`、`_macd`、`_vol_ma`、`_gain_over`、`volume_valid`、`low_zone_ok`、
`zigzag_pivots`。

`scan_frame()` 由 13 次调用收敛为 1 次；`deep_frame` 参数与 `scan.deep_lookback_days` 一并删除。

### 4.2 配置

- `src/ashare_monitor/config.py`：13 个 Config 类 + `Config` 字段 + `load()` 映射
- `config.yaml` / `config.example.yaml`：13 个配置块
- 保留 `ScanConfig`（去掉 `deep_lookback_days`）、`RiskConfig`、`Config`
- 删除后逐一 grep 确认以下字段已无引用，再从 `RiskConfig` 移除：`low_zone_pct`、
  `min_volume_ratio`、`early_trend_max_gain_pct`。**保留** `position_lookback_days`、
  `max_position_gain_pct`（本信号仍在用）、`weak_market_*`（`position_strategy` 在用）

### 4.3 管线与导出

- `cli.py`：`scan_frame` 调用参数、CSV 列清单中 13 组专属字段
- `scripts/refresh_signals.py`：`NAME2KEY`、`DETECTORS`
- `scripts/export_dashboard.py`：`SIGNAL_COUNT_KEYS`、`build_payload` 里对 `support_retest`/`sideways_breakout` 的单独引用

### 4.4 利好新闻加权

- 删除 `src/ashare_monitor/news_factor.py`
- `cli.py`：移除 `news_counts`/`sort_score` 调用；`sort_score` 直接等于 `score`
- `docs/app.js`：移除 `news_count` / `news_total` / `top_headline` 的渲染分支

### 4.5 月度主线

- 删除 `scripts/monthly_mainline.py`、`scripts/mainline_backtest.py`
- 删除 `docs/data/mainline.json` 与相关历史文件
- `scripts/daily_notify.py`：移除「本月主线」文案
- `docs/app.js` / `index.html`：移除主线板块与 `mainline.json` 请求
- `scripts/publish-site.ps1`：移除主线相关调用
- **Windows 计划任务**：删除 `AshareMonthlyMainline`（实现前先 `schtasks /Query` 确认实际任务名，再 `schtasks /Delete /F`）

### 4.6 前端

- `docs/app.js`：`SIGNAL_KINDS` 收敛为 1 项；删除 `EXTRA_KINDS` 与筛 chip 区；
  **删除左侧「信号类型」筛选区**（只剩一种信号，筛选无意义，列表固定展示全部命中）；
  删除 11 处 `kind ===` 分支，只保留 `uptrend` 一支
- `docs/styles.css`：删除 12 组旧信号配色变量与 `.signal-*` 规则，新增 `.signal-uptrend`
- `docs/index.html`：版本号 `?v=` 递增，移除主线容器

### 4.7 测试与文档

- `tests/test_patterns.py` 中依赖旧检测器的用例全部删除
- `tests/test_deep_base.py` 删除
- `README.md` 信号表收敛为一行；删除新闻加权与月度主线章节

## 5. 数据契约

`docs/data/latest.json` 结构不变，仅内容收敛：

```json
{"signal_counts": {"clear_uptrend": 26}, "signals": [{"signal": "清晰上升趋势", ...}]}
```

- 历史归档 `docs/data/archive/<日期>.json` **不重写**：它们仍含旧信号名与旧计数键
- 前端 `signalKind()` 对未知信号回落 `other`，归档视图保持可读
- 归档页的信号筛选 chip 删除后，旧归档仍可按日期浏览

## 6. 测试

TDD，新建 `tests/test_clear_uptrend.py`：

**命中**：合成一条标准上升路径（60 日 +10%，日波动小，无涨停）必须命中。

**反例**（逐项独立构造，各必须被拒）：

1. 收盘未站上 MA60
2. MA20 未上行
3. 60 日涨幅 < 10%
4. 收盘偏离 MA20 > 8%
5. ER < 0.18（宽幅震荡但净涨幅达标）
6. 日涨跌 p95 ≥ 7%（脉冲式上涨）
7. 60 日回撤超过 15%
8. 近 20 日涨停 2 次
9. 历史长度不足
10. `enabled: false`

**真数据回归**：以 2026-11-05 缓存为输入，断言命中集合等于第 3.5 节的 28 只（ST 剔除前）。

## 7. 迁移与发布

1. 仓库已恢复：此前 `HEAD` 卡在一个 2026-09-26 开始、停在中途的交互式 rebase；已建
   `backup-20261107` / `backup-main-20261107` 保底后 `rebase --abort`，并将 `main` 对齐
   `origin/main`（`3675eca`，as_of 2026-11-06）。核对过：本地分叉提交只改 `docs/data/*`，
   代码与 `origin/main` 逐字节一致，因此对齐不丢代码。
2. 实现、测试、本地跑通
3. 提交并推送；云端下一次 15:20 扫描自然产出新口径
4. 首次上线后，`latest.json` 只有 `clear_uptrend`；旧归档仍显示旧信号（预期行为）

## 8. 风险

| 风险 | 说明 | 处置 |
|---|---|---|
| 名单跨日重复 | 趋势是持续状态，同一只票可连续多日上榜 | 属预期语义；如需要，后续可加「首次入选」标记 |
| 历史归档与当前口径不一致 | 归档是冻结快照 | 接受；不做重写 |
| 删除不可逆 | 旧信号代码消失 | 全部改动在单个提交内，必要时可 revert |
| 名单偏向低波动蓝筹 | ER + p95 对低 beta 天然友好 | 已用 `min_gain_60=10%` 下限约束；实测含百亚股份、恒兴新材等并非纯蓝筹 |
| 计划任务名不确定 | 系统任务名可能与记忆不符 | 实现前先 `schtasks /Query` 核对 |

## 9. 验收标准

1. `pytest tests/ -q` 全绿
2. 对 2026-11-05 缓存跑全量扫描，命中集合与第 3.5 节一致（ST 剔除前 28 只）
3. `export_dashboard.py` 产出 `signal_counts = {"clear_uptrend": N}`
4. 浏览器实测：列表、详情弹窗、K 线、归档切换均无 console error
5. `AshareMonthlyMainline` 任务已不存在
