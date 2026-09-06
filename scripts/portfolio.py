"""组合引擎（Phase 1：从信号到「可复利模拟盘」）。

把每日信号池 → 一个受风控约束的真实组合，按口径模拟：
  - 入场：信号日 T 收盘判定，T+1 开盘买入；T+1 开盘跳空 > 买入参考+5% 或低于止损 → 跳过
  - 出场：T+1 起按 止损/止盈（同日止损优先），超时（默认 40 交易日）按收盘卖出
  - 组合/风控：单票上限、行业集中度上限、大盘环境降仓、最大回撤熔断（分档降仓/清仓）
  - 复利：利润滚入本金、仓位随净值缩放的定期再平衡（默认每周）
  - A股成本：佣金 0.025%（最低 5 元）双边 + 卖出印花税 0.05% + 滑点

两种模式：
  --mode backtest  对信号流（tracked.json）整段回放，输出净值/回撤/年化/月度等指标 → data/portfolio_backtest.json
  --mode paper     从最新信号 + 组合状态增量推进，写 data/portfolio_state.json，并发布 docs/data/portfolio.json

本模块只做研究模拟，不下真实订单、不连券商。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import notify  # noqa: E402  微信实时通知（未配置渠道则静默）

from ashare_monitor.config import load  # noqa: E402

COMMISSION = 0.00025
COMMISSION_MIN = 5.0
STAMP = 0.0005           # 卖出
SLIPPAGE = 0.001
LOT = 100                # A股一手 100 股
CHASE_CAP = 0.05

DEFAULTS = {
    "initial_cash": 20_000.0,         # 本金 20,000
    "slots": 4,                       # 轮动槽位数
    "max_positions": 8,
    "max_single_pct": 0.15,
    "max_sector_pct": 0.40,
    "target_invested_pct": 0.70,       # 大盘中性
    "strong_invested_pct": 0.90,       # 大盘偏强
    "weak_invested_pct": 0.35,         # 大盘偏弱
    "rebalance_every": 5,              # 每 5 个交易日再平衡（≈每周）
    "max_drawdown_pct": 0.18,          # 回撤熔断：清仓进现金
    "drawdown_de_risk_pct": 0.10,      # 回撤超 10%：仓位减半
    "time_stop_days": 40,
    "rotation_time_stop": 20,          # 轮动（超跌反转）时间止损 20 交易日
    "min_risk_reward": 3.0,            # 只开 1:3 盈亏比的仓
    "pause_days": 5,                   # 连亏熔断暂停天数（约 1 周）
    "slippage_pct": 0.002,             # 成交滑点（买入上浮/卖出下浮 0.2%）
    "min_turnover": 5_000_000.0,       # 日成交额下限（元），过低视为流动性差、买不进
    "lot": LOT,
}


def _ts_names_with_industry():
    """返回 {symbol6: industry}；Tushare 不可用时返回 {}（此时跳过行业集中度约束）。"""
    try:
        from ashare_monitor.data import _ts_pro
        pro = _ts_pro()
        if pro is None:
            return {}
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,industry")
        return {str(r["ts_code"])[:6]: str(r["industry"]) for _, r in basic.iterrows()}
    except Exception:
        return {}


def _load_prices(cache_dir: Path) -> dict[str, pd.DataFrame]:
    frames = {}
    for f in sorted(cache_dir.glob("*.csv")):
        try:
            frame = pd.read_csv(f, parse_dates=["date"]).sort_values("date").set_index("date")
        except Exception:
            continue
        frame = frame[~frame.index.duplicated(keep="last")]
        if not frame.empty:
            frames[f.stem] = frame
    return frames


def _sessions_from(frames: dict[str, pd.DataFrame]) -> list[str]:
    all_days = set()
    for f in frames.values():
        all_days.update(f.index.strftime("%Y%m%d"))
    return sorted(all_days)


def _bar(frames: dict[str, pd.DataFrame], sym: str, day: str):
    f = frames.get(sym)
    if f is None:
        return None
    ts = pd.Timestamp(day)
    if ts in f.index:
        return f.loc[ts]
    return None


def _fees_buy(cash: float, price: float, shares: int) -> float:
    notional = price * shares
    return max(COMMISSION_MIN, notional * COMMISSION)


def _fees_sell(notional: float) -> float:
    return max(COMMISSION_MIN, notional * COMMISSION) + notional * STAMP


class Portfolio:
    def __init__(self, cfg: dict, industry: dict[str, str]):
        self.cfg = cfg
        self.industry = industry
        self.cash = float(cfg["initial_cash"])
        self.positions: list[dict] = []   # held positions
        self.equity: list[tuple[str, float]] = []
        self.trades: list[dict] = []
        self.peak = float(cfg["initial_cash"])
        self.defensive = False            # 回撤熔断后保持现金
        self._last_rebalance = -10**9

    def equity_value(self, day: str, frames) -> float:
        val = self.cash
        for p in self.positions:
            b = _bar(frames, p["symbol"], day)
            price = float(b.close) if b is not None else p["entry_price"]
            val += p["shares"] * price
        return val

    def _sell(self, p: dict, price: float, day: str, reason: str):
        notional = p["shares"] * price
        fee = _fees_sell(notional)
        proceeds = notional - fee
        self.cash += proceeds
        cost = p["cost"] + p["buy_fee"]
        net = proceeds - cost
        self.trades.append({
            "symbol": p["symbol"], "name": p.get("name"), "signal": p.get("signal"),
            "entry_day": p["entry_day"], "exit_day": day, "reason": reason,
            "entry_price": round(p["entry_price"], 3), "exit_price": round(price, 3),
            "shares": p["shares"], "net_pnl": round(net, 2), "win": net > 0,
        })

    def settle_exits(self, day: str, sessions, sidx, frames):
        """T+1 起执行止损/止盈/超时。这里假定 day 已按传入顺序递增。"""
        still = []
        for p in self.positions:
            if day <= p["entry_day"]:
                still.append(p)
                continue
            held = sidx[day] - sidx[p["entry_day"]]
            b = _bar(frames, p["symbol"], day)
            if b is None:
                still.append(p)
                continue
            low, high, close = float(b.low), float(b.high), float(b.close)
            if low <= p["stop"]:
                self._sell(p, p["stop"], day, "止损"); continue
            if p["target"] is not None and high >= p["target"]:
                self._sell(p, p["target"], day, "止盈"); continue
            if held >= self.cfg["time_stop_days"]:
                self._sell(p, close, day, "超时"); continue
            still.append(p)
        self.positions = still

    def snapshot(self, day: str, frames) -> dict:
        val = self.equity_value(day, frames)
        dd = val / self.peak - 1 if self.peak else 0.0
        return {
            "day": day, "equity": round(val, 2), "cash": round(self.cash, 2),
            "positions": len(self.positions), "drawdown_pct": round(dd * 100, 2),
        }


def simulate(cfg: dict, signals: list[dict], frames: dict[str, pd.DataFrame],
             sessions: list[str], industry: dict[str, str]) -> dict:
    sidx = {d: i for i, d in enumerate(sessions)}
    pf = Portfolio(cfg, industry)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        by_day[str(sig["sd"])].append(sig)

    rebalance_every = int(cfg["rebalance_every"])
    last_rebalance = -10**9

    for day in sessions:
        # 1) 先出局（已持仓的止损/止盈/超时）
        pf.settle_exits(day, sessions, sidx, frames)
        # 2) 收盘估值
        val = pf.equity_value(day, frames)
        pf.peak = max(pf.peak, val)
        dd = val / pf.peak - 1 if pf.peak else 0.0
        # 3) 回撤风控
        if dd <= -cfg["max_drawdown_pct"]:
            pf.defensive = True
        # 4) 再平衡（每周最后一个交易日收盘后，用当日信号构建，T+1 开盘成交）
        if (sidx[day] - last_rebalance) >= rebalance_every:
            candidates = sorted(by_day.get(day, []), key=lambda s: -float(s.get("sc") or 0))
            if candidates and not pf.defensive:
                _rebalance(pf, day, candidates, frames, cfg, sidx)
            last_rebalance = sidx[day]
        # 5) 若触发熔断：全部清仓进现金
        if pf.defensive and pf.positions:
            for p in list(pf.positions):
                b = _bar(frames, p["symbol"], day)
                price = float(b.close) if b is not None else p["entry_price"]
                pf._sell(p, price, day, "熔断清仓")
            pf.positions = []

        pf.equity.append((day, pf.equity_value(day, frames)))

    return _report(cfg, sessions, pf)


def _rebalance(pf, day, candidates, frames, cfg, sidx):
    """选出目标持仓，在 T+1 开盘成交（跳空规则 + 仓位/集中度约束）。"""
    invested_pct = cfg["target_invested_pct"]
    # 环境缩放（用信号里的 env 众数）
    envs = [c.get("env") for c in candidates if c.get("env")]
    if envs:
        model = max(set(envs), key=envs.count)
        if model == "偏强":
            invested_pct = cfg["strong_invested_pct"]
        elif model == "偏弱":
            invested_pct = cfg["weak_invested_pct"]
    if pf.defensive:
        invested_pct = 0.0
    invest_cap = pf.equity_value(day, frames) * invested_pct

    chosen = []
    sector_used: dict[str, float] = defaultdict(float)
    single_cap = cfg["max_single_pct"]
    sector_cap = cfg["max_sector_pct"]
    for c in candidates:
        sym = c["s"]
        if len(chosen) >= cfg["max_positions"]:
            break
        # 盘口：T+1 开盘
        after = None
        f = frames.get(sym)
        if f is None:
            continue
        idx_after = f.index[f.index.strftime("%Y%m%d") > day]
        if len(idx_after) == 0:
            continue
        entry_day = idx_after[0].strftime("%Y%m%d")
        ob = f.loc[idx_after[0]]
        op = float(ob.open)
        er = float(c.get("er") or 0); st = float(c.get("st") or 0)
        if op <= 0 or op > er * (1 + CHASE_CAP) or op < st:
            continue
        # 行业集中度（有行业映射才约束）
        industry = industry_of(pf.industry, sym)
        weight = float(c.get("pos") or 0) / 100.0
        weight = min(weight, single_cap)
        if industry and sector_used.get(industry, 0) >= sector_cap:
            continue
        if industry and sector_used.get(industry, 0) + weight > sector_cap:
            continue
        budget = invest_cap * weight
        shares = int(budget / op / cfg["lot"]) * cfg["lot"]
        if shares < cfg["lot"]:
            continue
        notional = shares * op
        buy_fee = _fees_buy(pf.cash, op, shares)
        if pf.cash < notional + buy_fee:
            continue
        pf.cash -= notional + buy_fee
        pf.positions.append({
            "symbol": sym, "name": c.get("n"), "signal": c.get("p"),
            "entry_day": entry_day, "entry_price": op, "cost": notional, "buy_fee": buy_fee,
            "stop": st, "target": (float(c["tp"]) if c.get("tp") is not None else None),
            "shares": shares,
        })
        if industry:
            sector_used[industry] += weight
        chosen.append(sym)


def industry_of(mapping: dict[str, str], sym: str) -> str:
    return mapping.get(sym, "")


# ---------------------------------------------------------------------------
# Paper（实盘模拟）模式：用已生成的当日信号 + 风控，输出目标组合 →
# 发布 docs/data/portfolio.json 供站上「组合 · 模拟盘」面板展示。
# 需要数据：只有当日信号（latest.json / 最新 reports/signals.json），无重放、无回测。
# ---------------------------------------------------------------------------

def _ts_pro():
    try:
        from ashare_monitor.data import _ts_pro as _p
        return _p()
    except Exception:
        return None


def _load_industry(pro) -> dict[str, str]:
    """行业映射（集中度约束用）；Tushare 不可用/未缓存时返回 {}（退化为 board 分组）。"""
    p = ROOT / "data" / "meta_industry.json"
    if p.exists():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if m:
                return m
        except Exception:
            pass
    if pro is None:
        return {}
    try:
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,industry")
        m = {str(r["ts_code"])[:6]: str(r["industry"]) for _, r in basic.iterrows()}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return m
    except Exception:
        return {}


def _load_signals_latest(path: Path) -> tuple[list[dict], int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload, -1
    return payload.get("signals", []), int(payload.get("as_of", -1))


def paper_target(cfg: dict, signals: list[dict], industry: dict[str, str],
                 as_of: int, equity_ref: float) -> dict:
    ordered = [s for s in signals if s.get("entry_price") and s.get("stop_loss")]
    ordered.sort(key=lambda s: -(float(s.get("sort_score") or s.get("score") or 0)))
    envs = [s.get("market_env") for s in ordered if s.get("market_env")]
    regime = max(set(envs), key=envs.count) if envs else "中性"
    invested_pct = {"偏强": cfg["strong_invested_pct"], "偏弱": cfg["weak_invested_pct"]}.get(regime, cfg["target_invested_pct"])

    sector_used: dict[str, float] = defaultdict(float)
    chosen = []
    for s in ordered:
        if len(chosen) >= cfg["max_positions"]:
            break
        sym = str(s.get("symbol") or "").zfill(6)
        industry_k = industry.get(sym) or str(s.get("board") or "")
        weight = min(float(s.get("position_pct") or 5) / 100.0, cfg["max_single_pct"])
        if industry_k and sector_used[industry_k] + weight > cfg["max_sector_pct"]:
            continue
        chosen.append({
            "symbol": sym, "name": s.get("name"), "signal": s.get("signal"),
            "score": round(float(s.get("sort_score") or s.get("score") or 0), 1),
            "weight": weight, "industry": industry_k,
            "entry_ref": round(float(s["entry_price"]), 2),
            "stop": round(float(s["stop_loss"]), 2),
            "target": round(float(s["take_profit"]), 2) if s.get("take_profit") is not None else None,
        })
        if industry_k:
            sector_used[industry_k] += weight
    total_weight = sum(c["weight"] for c in chosen)
    scale = invested_pct / total_weight if total_weight > 0 else 0.0
    positions = [dict(c, weight_pct=round(c["weight"] * scale * 100, 1)) for c in chosen]
    invest_value = equity_ref * invested_pct
    return {
        "schema_version": 1,
        "as_of": int(as_of),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "equity_ref": round(equity_ref, 2),
        "invested_pct": round(invested_pct * 100, 1),
        "cash_pct": round((1 - invested_pct) * 100, 1),
        "regime": regime,
        "position_count": len(positions),
        "risk": {"max_single_pct": round(cfg["max_single_pct"] * 100, 1),
                 "max_sector_pct": round(cfg["max_sector_pct"] * 100, 1),
                 "max_drawdown_pct": round(cfg["max_drawdown_pct"] * 100, 1)},
        "method": "模拟盘：当日信号按信号分数优先+单票 ≤ 15%/行业 ≤ 40% 集中度+大盘环境降仓（偏弱 35%/中性 70%/偏强 90%），目标投入 70%；T+1 开盘按计划价/止损/止盈执行。仅研究模拟，不下真实订单。",
        "positions": positions,
    }


class Rotation:
    """N 槽位每日轮动状态机：只做 超跌反转/恰好突破20日线，1:3 止盈止损，槽位完成即换新达标股。

    口径：信号日 T 收盘判定 -> T+1 开盘买入（跳空>5%或低于止损放弃，一字板跳过）；
    止盈=入场+3×风险(1:3)、止损=证伪位，同日双触发按止损优先；20 交易日未达标按收盘时间止。
    复利：槽位等权、每周推进，现金预留；回撤>10% 半仓、>18% 切现金；连亏 3 次暂停 1 周。
    """
    FAMILIES = ("超跌反转", "恰好突破20日线")

    def __init__(self, cfg, frames, sessions, sidx):
        self.cfg = cfg
        self.frames = frames
        self.sessions = sessions
        self.sidx = sidx
        self.cash = float(cfg["initial_cash"])
        self.positions = []
        self.pending = []
        self.equity = []
        self.trades = []
        self.peak = float(cfg["initial_cash"])
        self.defensive = False
        self.de_risk = False
        self.consec_losses = 0
        self.pause_until = ""

    def _bar(self, symbol, day):
        f = self.frames.get(symbol)
        if f is None:
            return None
        ts = pd.Timestamp(day)
        if ts in f.index:
            return f.loc[ts]
        return None

    def equity_value(self, day):
        val = self.cash
        for p in self.positions:
            b = self._bar(p["symbol"], day)
            price = float(b.close) if b is not None else p["entry_price"]
            val += p["shares"] * price
        return val

    def _notify(self, title, content=""):
        try:
            notify.send(title, content)
        except Exception:
            pass

    def _sell(self, p, price, day, reason):
        notional = p["shares"] * price
        fee = max(self.cfg["commission_min"], notional * self.cfg["commission_pct"]) + notional * self.cfg["stamp_pct"]
        proceeds = notional - fee
        self.cash += proceeds
        cost = p["cost"] + p["buy_fee"]
        net = proceeds - cost
        self.trades.append({"symbol": p["symbol"], "name": p.get("name"), "signal": p.get("signal"),
                            "entry_day": p["entry_day"], "exit_day": day, "reason": reason,
                            "entry_price": round(p["entry_price"], 3), "exit_price": round(price, 3),
                            "shares": p["shares"], "net_pnl": round(net, 2), "win": net > 0})
        self.consec_losses = 0 if net > 0 else self.consec_losses + 1
        if reason in ("止盈", "止损", "超时"):
            icon = {"止盈": "✅", "止损": "🛑", "超时": "⏱"}.get(reason, "·")
            self._notify(f"{icon} {reason} {p.get('name')} {net:+.2f}元",
                         f"信号:{p.get('signal')} | 入场{p['entry_day']}@{p['entry_price']} | "
                         f"{day}卖出 {p['shares']}股@{price} | {reason}")
        if self.consec_losses >= 3 and not self.pause_until:
            self.pause_until = self._shift(day, self.cfg["pause_days"])
            self._notify("⚠️ 连亏 3 次，暂停新开仓", f"暂停至 {self.pause_until}")

    def _shift(self, day, n):
        try:
            i = self.sidx[day]
            j = min(i + n, len(self.sessions) - 1)
            return self.sessions[j]
        except Exception:
            return day

    def _settle_exits(self, day):
        still = []
        for p in self.positions:
            if day <= p["entry_day"]:
                still.append(p)
                continue
            b = self._bar(p["symbol"], day)
            if b is None:
                still.append(p)
                continue
            held = self.sidx[day] - self.sidx[p["entry_day"]]
            sl = self.cfg["slippage_pct"]
            if float(b.low) <= p["stop"]:
                # 止损：若开盘已跌破止损（跳空向下），按更差的开盘价成交（保守）
                exit_p = float(b.open) if float(b.open) < p["stop"] else p["stop"]
                self._sell(p, exit_p * (1 - sl), day, "止损")
                continue
            if p["target"] is not None and float(b.high) >= p["target"]:
                self._sell(p, p["target"] * (1 - sl), day, "止盈")
                continue
            if held >= self.cfg["rotation_time_stop"]:
                self._sell(p, float(b.close) * (1 - sl), day, "超时")
                continue
            still.append(p)
        self.positions = still

    def _queue(self, day, signals_today):
        # 只收两类、RR>=1:3 的信号进入待入场队列（次日开盘买）
        for s in signals_today:
            if s.get("signal") not in self.FAMILIES:
                continue
            rr = float(s.get("risk_reward") or 0)
            if rr < self.cfg["min_risk_reward"]:
                continue
            if not s.get("entry_price") or not s.get("stop_loss"):
                continue
            self.pending.append({
                "symbol": str(s.get("symbol") or "").zfill(6), "name": s.get("name"), "signal": s.get("signal"),
                "score": float(s.get("sort_score") or s.get("score") or 0),
                "entry_ref": float(s["entry_price"]), "stop": float(s["stop_loss"]),
                "target": float(s["take_profit"]) if s.get("take_profit") is not None else None,
                "risk_reward": rr, "signal_day": day})
        # 同股只保留分数最高的
        best = {}
        for p in self.pending:
            k = p["symbol"]
            if k not in best or p["score"] > best[k]["score"]:
                best[k] = p
        self.pending = list(best.values())

    def _fill(self, day):
        if self.defensive:
            return
        if self.pause_until and day <= self.pause_until:
            return
        held = {p["symbol"] for p in self.positions}
        empty = self.cfg["slots"] - len(self.positions)
        if empty <= 0:
            return
        cands = sorted([p for p in self.pending if p["signal_day"] < day and p["symbol"] not in held],
                       key=lambda p: -p["score"])
        for p in cands:
            if empty <= 0:
                break
            b = self._bar(p["symbol"], day)
            if b is None:
                continue
            op = float(b.open)
            if op <= 0 or op > p["entry_ref"] * (1 + CHASE_CAP) or op < p["stop"]:
                continue
            if float(b.high) == float(b.low):   # 一字板，无法正常成交
                continue
            if op * float(b.volume) < self.cfg["min_turnover"]:   # 日成交额过低，流动性差，买不进
                continue
            slot_w = self.cfg["target_invested_pct"] / self.cfg["slots"]
            if self.de_risk:
                slot_w *= 0.5
            equity = self.equity_value(day)
            budget = equity * slot_w
            fill = op * (1 + self.cfg["slippage_pct"])           # 买入滑点，接受稍高价
            shares = int(budget / fill / self.cfg["lot"]) * self.cfg["lot"]
            if shares < self.cfg["lot"]:
                continue
            notional = shares * fill
            fee = max(self.cfg["commission_min"], notional * self.cfg["commission_pct"])
            cost = notional + fee
            if cost > self.cash:
                shares = int(self.cash * 0.995 / fill / self.cfg["lot"]) * self.cfg["lot"]
                if shares < self.cfg["lot"]:
                    continue
                notional = shares * fill
                fee = max(self.cfg["commission_min"], notional * self.cfg["commission_pct"])
                cost = notional + fee
            self.cash -= cost
            self.positions.append({"symbol": p["symbol"], "name": p["name"], "signal": p["signal"],
                                   "entry_day": day, "entry_price": fill, "shares": shares, "cost": notional,
                                   "buy_fee": fee, "stop": p["stop"], "target": p["target"], "score": p["score"]})
            self._notify("📈 开仓 " + p["name"],
                         f"信号:{p['signal']} | {day}@{fill:.2f} 买入 {shares}股 | 止损 {p['stop']} 止盈 {p['target']} (1:3)")
            held.add(p["symbol"])
            empty -= 1
            self.pending = [x for x in self.pending if x is not p]
        # 清理过期的待入场信号（>3 交易日，避免久拖）
        try:
            self.pending = [p for p in self.pending if self.sidx[day] - self.sidx[p["signal_day"]] <= 3]
        except Exception:
            pass

    def _liquidate(self, day, reason):
        closed = len(self.positions)
        if closed == 0:
            return
        for p in list(self.positions):
            b = self._bar(p["symbol"], day)
            price = float(b.close) if b is not None else p["entry_price"]
            self._sell(p, price * (1 - self.cfg["slippage_pct"]), day, reason)
        self.positions = []
        self._notify("⚠️ " + reason, f"{day} 清仓 {closed} 只，仓位转现金")

    def step(self, day, signals_today):
        self._settle_exits(day)
        self._queue(day, signals_today)
        self._fill(day)
        val = self.equity_value(day)
        self.peak = max(self.peak, val)
        dd = val / self.peak - 1 if self.peak else 0.0
        if dd <= -self.cfg["max_drawdown_pct"] and not self.defensive:
            self.defensive = True
            self._liquidate(day, "熔断清仓")
        elif dd <= -self.cfg["drawdown_de_risk_pct"]:
            self.de_risk = True
        self.equity.append([day, round(self.equity_value(day), 2)])
        self._notify("📊 轮动日报 " + day,
                     f"净值 {self.equity_value(day):.0f} | 回撤 {dd * 100:.1f}% | "
                     f"持仓 {len(self.positions)}/{self.cfg['slots']} | 现金 {self.cash:.0f} | 待入场 {len(self.pending)}")
        return self.snapshot(day)

    def snapshot(self, day):
        val = self.equity_value(day)
        dd = val / self.peak - 1 if self.peak else 0.0
        prices = []
        for p in self.positions:
            b = self._bar(p["symbol"], day)
            cur = float(b.close) if b is not None else p["entry_price"]
            prices.append({"symbol": p["symbol"], "name": p["name"], "signal": p["signal"],
                           "entry_day": p["entry_day"], "entry_price": round(p["entry_price"], 2),
                           "shares": p["shares"], "stop": round(p["stop"], 2),
                           "target": round(p["target"], 2) if p["target"] is not None else None,
                           "current": round(cur, 2), "pnl_pct": round((cur / p["entry_price"] - 1) * 100, 2)})
        return {
            "day": day, "cash": round(self.cash, 2), "equity": round(val, 2),
            "drawdown_pct": round(dd * 100, 2), "peak": round(self.peak, 2),
            "slots": {"total": self.cfg["slots"], "filled": len(self.positions)},
            "defensive": self.defensive, "de_risk": self.de_risk,
            "consec_losses": self.consec_losses, "pause_until": self.pause_until,
            "positions": prices, "trades": self.trades[-8:],
            "equity_curve": self.equity,
        }

    def to_dict(self):
        return {"cash": self.cash, "positions": self.positions, "pending": self.pending,
                "equity": self.equity, "trades": self.trades, "peak": self.peak,
                "defensive": self.defensive, "de_risk": self.de_risk,
                "consec_losses": self.consec_losses, "pause_until": self.pause_until}

    @classmethod
    def from_dict(cls, d, cfg, frames, sessions, sidx):
        r = cls(cfg, frames, sessions, sidx)
        r.cash = float(d.get("cash", cfg["initial_cash"]))
        r.positions = d.get("positions", [])
        r.pending = d.get("pending", [])
        r.equity = d.get("equity", [])
        r.trades = d.get("trades", [])
        r.peak = float(d.get("peak", cfg["initial_cash"]))
        r.defensive = bool(d.get("defensive", False))
        r.de_risk = bool(d.get("de_risk", False))
        r.consec_losses = int(d.get("consec_losses", 0))
        r.pause_until = str(d.get("pause_until", ""))
        return r


def _load_sessions(cache_dir: Path) -> list[str]:
    """全市场交易日历（所有缓存日期列的并集）。缓存到 data/meta_sessions.json。"""
    p = ROOT / "data" / "meta_sessions.json"
    if p.exists():
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            if s:
                return s
        except Exception:
            pass
    days = set()
    for f in sorted(cache_dir.glob("*.csv")):
        try:
            df = pd.read_csv(f, usecols=["date"])
            days.update(pd.to_datetime(df["date"]).dt.strftime("%Y%m%d").tolist())
        except Exception:
            continue
    sessions = sorted(days)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
    return sessions


def _load_frames_for(cache_dir: Path, symbols) -> dict[str, pd.DataFrame]:
    """只加载指定符号的日线（当日轮动用不到全部 5066 只，避免每天读全量）。"""
    frames = {}
    for sym in symbols:
        f = cache_dir / f"{sym}.csv"
        if f.exists():
            try:
                frame = pd.read_csv(f, parse_dates=["date"]).sort_values("date").set_index("date")
                frame = frame[~frame.index.duplicated(keep="last")]
                frames[sym] = frame
            except Exception:
                continue
    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description="组合引擎（模拟盘/回测/轮动）")
    ap.add_argument("--signals", default="docs/data/latest.json")
    ap.add_argument("--prices", default="data/cache")
    ap.add_argument("--mode", choices=["backtest", "paper", "rotation"], default="backtest")
    ap.add_argument("--out", default="data/portfolio_backtest.json")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--today", default="", help="轮动模式：本次处理的交易日 yyyymmdd")
    args = ap.parse_args()

    cfg = dict(DEFAULTS)
    cfg_path = ROOT / args.config
    if cfg_path.exists():
        try:
            yaml_cfg = load(cfg_path)
            port = getattr(yaml_cfg, "portfolio", None)
            for k in DEFAULTS:
                if port and getattr(port, k, None) is not None:
                    cfg[k] = getattr(port, k)
        except Exception as exc:
            print(f"[pf] config 读取失败（{exc}），用默认风控参数。")

    if args.mode == "paper":
        signals, as_of = _load_signals_latest(ROOT / args.signals)
        industry = _load_industry(_ts_pro())
        print(f"[pf] paper: signals={len(signals)} as_of={as_of} industry_map={len(industry)}")
        equity_ref = cfg["initial_cash"] if not (ROOT / "data" / "portfolio_state.json").exists() else cfg["initial_cash"]
        payload = paper_target(cfg, signals, industry, as_of, equity_ref)
        out = ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "rotation":
        state_path = ROOT / "data" / "portfolio_state.json"
        sessions = _load_sessions(ROOT / args.prices)
        sidx = {d: i for i, d in enumerate(sessions)}
        signals_today, as_of = _load_signals_latest(ROOT / args.signals)
        today = args.today or (str(as_of) if as_of > 0 else (sessions[-1] if sessions else ""))
        if not today or today not in sidx:
            print(f"[rot] 今天 {today} 无行情数据，不推进（等下一交易日扫描后运行）。", flush=True)
            return 0
        # 只加载股票：信号池 + 已有持仓/待入场
        state_old = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        needed = {str(s.get("symbol") or "").zfill(6) for s in signals_today}
        needed |= {p["symbol"] for p in state_old.get("positions", [])}
        needed |= {p["symbol"] for p in state_old.get("pending", [])}
        frames = _load_frames_for(ROOT / args.prices, needed)
        if state_path.exists():
            rot = Rotation.from_dict(state_old, cfg, frames, sessions, sidx)
        else:
            rot = Rotation(cfg, frames, sessions, sidx)
        snap = rot.step(today, signals_today)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(rot.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        out = ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1, "as_of": int(today),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": ("轮动模拟盘：只做超跌反转+恰好突破20日线，1:3止盈止损（RR<3.0不开仓），T+1开盘、跳空>5%或一字板不入场，"
                       "20交易日时间止；槽位完成即换入新达标股；回撤>10%半仓、>18%切现金、连亏3次暂停1周。仅研究模拟，不下真实订单。"),
            **snap,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # ---- backtest 模式（历史重放验证；与信号引擎一致）----
    signals = json.loads((ROOT / args.signals).read_text(encoding="utf-8")).get("signals", [])
    frames = _load_prices(ROOT / args.prices)
    sessions = _sessions_from(frames)
    industry = _load_industry(_ts_pro())
    print(f"[pf] signals={len(signals)} frames={len(frames)} sessions={len(sessions)} industry_map={len(industry)}")

    report = simulate(cfg, signals, frames, sessions, industry)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _report(cfg, sessions, pf) -> dict:
    curve = pd.Series([v for _, v in pf.equity], index=pd.to_datetime([d for d, _ in pf.equity]))
    initial = float(cfg["initial_cash"])
    if len(curve) == 0:
        return {"error": "no sessions"}
    final = float(curve.iloc[-1])
    total = final / initial - 1
    days = len(curve)
    years = max(days / 244, 1e-6)
    cagr = (final / initial) ** (1 / years) - 1
    dd = float((curve / curve.cummax() - 1).min())
    rets = curve.pct_change().dropna()
    vol = float(rets.std() * np.sqrt(244)) if len(rets) else 0.0
    sharpe = float(rets.mean() / rets.std() * np.sqrt(244)) if len(rets) and rets.std() > 0 else None
    n = len(pf.trades)
    wins = sum(1 for t in pf.trades if t["win"])
    monthly = (curve.resample("ME").last().pct_change().dropna() * 100).round(2)
    by_signal, by_reason = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0}), defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in pf.trades:
        for bucket, k in ((by_signal, t["signal"]), (by_reason, t["reason"])):
            bucket[k]["n"] += 1
            bucket[k]["w"] += 1 if t["win"] else 0
            bucket[k]["pnl"] += t["net_pnl"]
    return {
        "initial_cash": round(initial, 2), "final_equity": round(final, 2),
        "total_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2), "annual_vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "trades": n, "win_rate_pct": round(wins / n * 100, 2) if n else None,
        "by_signal": {k: {**v, "win_rate_pct": round(v["w"] / v["n"] * 100, 2) if v["n"] else None,
                          "pnl": round(v["pnl"], 2)} for k, v in by_signal.items()},
        "by_exit_reason": {k: {**v, "win_rate_pct": round(v["w"] / v["n"] * 100, 2) if v["n"] else None,
                               "pnl": round(v["pnl"], 2)} for k, v in by_reason.items()},
        "monthly_returns_pct": {str(k.date()): float(v) for k, v in monthly.items()},
        "window": [str(curve.index[0].date()), str(curve.index[-1].date())],
        "config_projection": {"rebalance_every": cfg["rebalance_every"], "max_positions": cfg["max_positions"],
                              "max_single_pct": cfg["max_single_pct"], "max_sector_pct": cfg["max_sector_pct"],
                              "max_drawdown_pct": cfg["max_drawdown_pct"]},
    }


if __name__ == "__main__":
    raise SystemExit(main())
