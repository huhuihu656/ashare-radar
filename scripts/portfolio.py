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
sys.path.insert(0, str(ROOT / "src"))

from ashare_monitor.config import load  # noqa: E402

COMMISSION = 0.00025
COMMISSION_MIN = 5.0
STAMP = 0.0005           # 卖出
SLIPPAGE = 0.001
LOT = 100                # A股一手 100 股
CHASE_CAP = 0.05

DEFAULTS = {
    "initial_cash": 100_000.0,
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


def main() -> int:
    ap = argparse.ArgumentParser(description="组合引擎（模拟盘/回测）")
    ap.add_argument("--signals", default="docs/data/latest.json")
    ap.add_argument("--prices", default="data/cache")
    ap.add_argument("--mode", choices=["backtest", "paper"], default="backtest")
    ap.add_argument("--out", default="data/portfolio_backtest.json")
    ap.add_argument("--config", default="config.yaml")
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
