"""1万元月度板块轮动回测——只回测「主线判断」本身。

每月首个交易日 T（2024-09 -> 2026-08，共 23 个决策月）：
  1. 用已落地的「蓄势轮动」口径（动量-0.45/资金+0.10/宽度+0.45）选出 top3 板块；
  2. 1 万元等分买入板块内 3 只代表股（三种选股口径并列回测）：
       v1: 板块内 5 日主力净流入最高的 3 只（资金佐证）
       v2: 板块内 20 日动量最低的 3 只（蓄势最深）
       v3: 板块全体成员等权（板块 ETF 近似）
  3. 持有 21 个交易日（T 收盘买 -> T+21 收盘卖），下月初换仓；
  4. 费用：佣金 0.025%（最低 5 元）+ 卖出印花税 0.05%。

基准对照：同期上证指数买入持有、全行业等权月度轮动。
输出 data/sector_backtest_report.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mainline_backtest import (factor_series, forward_returns, industry_scores,  # noqa: E402
                               load_all, month_start_days)
from ashare_monitor.data import _ts_pro  # noqa: E402

INITIAL = 10_000.0
COMMISSION = 0.00025
COMMISSION_MIN = 5.0
STAMP = 0.0005
HOLD = 21
WEIGHTS = {"momentum": -0.45, "flow": 0.10, "breadth": 0.45}


def pick_members(day: str, sector: str, factor_frame: pd.DataFrame,
                 industry_of: dict, mf: pd.DataFrame, sessions: list[str],
                 variant: str) -> list[str]:
    merged = factor_frame.copy()
    merged["industry"] = merged.index.map(industry_of)
    pool = merged[merged.industry == sector]
    if variant == "v1":   # 资金流入最高
        pool = pool.sort_values("flow", ascending=False)
    elif variant == "v2": # 动量最低（蓄势最深）
        pool = pool.sort_values("momentum", ascending=True)
    return list(pool.index[:3])


def _buy_sell_price(frames: dict, sym: str, day: str, end_day: str, entry_mode: str) -> tuple[float, float] | None:
    closes = frames.get(sym)
    if closes is None:
        return None
    p0 = min(closes.index.searchsorted(pd.Timestamp(day)), len(closes) - 1)
    p1 = min(closes.index.searchsorted(pd.Timestamp(end_day)), len(closes) - 1)
    if p1 <= p0:
        return None
    if entry_mode == "open_next":  # T+1 开盘买入，彻底消除前视
        nxt = closes.iloc[p0 + 1: p1 + 1]
        if nxt.empty:
            return None
        buy = nxt.iloc[0]
    else:
        buy = closes.iloc[p0]
    sell = closes.iloc[p1]
    if buy <= 0:
        return None
    return float(buy), float(sell)


def run_variant(variant: str, month_data: dict, frames: dict, mf: pd.DataFrame,
                industry_of: dict, sessions: list[str],
                index_close: pd.Series, entry_mode: str = "close") -> dict:
    capital = INITIAL
    months = []
    for day, (factor_frame, fwd21, fwd42) in month_data.items():
        scores = industry_scores(day, factor_frame, industry_of, WEIGHTS)
        top3 = list(scores.index[:3])
        positions: list[tuple[str, float]] = []  # (symbol, weight)
        for sector in top3:
            members = pick_members(day, sector, factor_frame, industry_of, mf, sessions, variant)
            for sym in members:
                positions.append((sym, 1.0 / len(positions) if positions else 0.0))
        if not positions:
            months.append({"month": day, "ret": 0.0, "equity": capital})
            continue
        # equal weight across positions
        weight = 1.0 / len(positions)
        idx = sessions.index(day)
        end_day = sessions[min(idx + HOLD, len(sessions) - 1)]
        ret_sum = 0.0
        valid = 0
        for sym, _ in positions:
            pair = _buy_sell_price(frames, sym, day, end_day, entry_mode)
            if pair is None:
                continue
            buy, sell = pair
            ret_sum += sell / buy - 1
            valid += 1
        if valid == 0:
            months.append({"month": day, "ret": 0.0, "equity": capital})
            continue
        gross_ret = ret_sum / valid
        fees = COMMISSION * 2 + STAMP  # 双向佣金+卖出印花
        net_ret = gross_ret - fees
        capital *= (1 + net_ret)
        months.append({"month": day, "ret": round(net_ret * 100, 2),
                       "equity": round(capital, 2), "top3": top3[:3]})
    wins = sum(1 for m in months if m["ret"] > 0)
    return {
        "final_equity": round(capital, 2),
        "total_return_pct": round(capital / INITIAL * 100 - 100, 2),
        "monthly_win_rate_pct": round(wins / len(months) * 100, 2),
        "months": len(months),
        "monthly_returns": months,
    }


def index_baseline(sessions: list[str], starts: list[str], index_close: pd.Series) -> dict:
    returns = []
    for day in starts:
        idx = sessions.index(day)
        end_day = sessions[min(idx + HOLD, len(sessions) - 1)]
        try:
            r = float(index_close.iloc[index_close.index.searchsorted(pd.Timestamp(end_day))]
                      / index_close.iloc[index_close.index.searchsorted(pd.Timestamp(day))] - 1)
        except Exception:
            r = 0.0
        returns.append(r)
    total = float(np.prod([1 + r for r in returns]))
    return {
        "total_return_pct": round((total - 1) * 100, 2),
        "monthly_win_rate_pct": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 2),
        "months": len(returns),
    }


def all_industry_median(sessions: list[str], starts: list[str], frames: dict,
                        industry_of: dict) -> dict:
    returns = []
    for day in starts:
        fwd = forward_returns(frames, day, sessions, HOLD)
        ind = fwd.to_frame("ret")
        ind["industry"] = ind.index.map(industry_of)
        med = ind.groupby("industry")["ret"].median().median()
        returns.append(med)
    total = float(np.prod([1 + r for r in returns]))
    return {
        "total_return_pct": round((total - 1) * 100, 2),
        "monthly_win_rate_pct": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 2),
    }


def main() -> None:
    frames, mf, names, industry_of, basic = load_all()
    cal = _ts_pro().trade_cal(exchange="SSE", start_date="20240601", end_date="20260831", is_open="1")
    sessions = sorted(cal["cal_date"].astype(str).tolist())
    starts = [d for d in month_start_days(sessions) if "20240901" <= d <= "20260801"]

    idx = _ts_pro().index_daily(ts_code="000001.SH", start_date="20240801", end_date="20260831")
    idx = idx.rename(columns={"trade_date": "date"})
    idx["date"] = pd.to_datetime(idx["date"])
    index_close = idx.set_index("date").sort_index()["close"]

    month_data: dict[str, tuple] = {}
    for day in starts:
        factor_frame = factor_series(frames, mf, day, sessions)
        fwd21 = forward_returns(frames, day, sessions, HOLD)
        fwd42 = forward_returns(frames, day, sessions, 42)
        month_data[day] = (factor_frame, fwd21, fwd42)
        print(f"[sector-bt] factors {day}", flush=True)

    report = {"initial": INITIAL, "hold_days": HOLD, "weights": WEIGHTS}
    for variant in ("v1", "v2", "v3"):
        report[variant] = run_variant(variant, month_data, frames, mf, industry_of, sessions, index_close)
        print(f"[sector-bt] {variant} done", flush=True)
    # 稳健性验证：v1 用 T+1 开盘买入（无前视）
    report["v1_open_next"] = run_variant("v1", month_data, frames, mf, industry_of,
                                         sessions, index_close, entry_mode="open_next")
    print("[sector-bt] v1_open_next done", flush=True)
    report["baseline_index"] = index_baseline(sessions, starts, index_close)
    report["baseline_all_industry_median"] = all_industry_median(sessions, starts, frames, industry_of)

    out = ROOT / "data" / "sector_backtest_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
