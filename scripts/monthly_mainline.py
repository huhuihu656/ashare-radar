"""Monthly mainline-sector detection (月初首个交易日自动运行).

Four-factor deterministic scoring per industry (Eastern-money classification):
  1. 动量 momentum    : median 20-session return of member stocks
  2. 资金流 flow      : mean per-member main-force net inflow, last 5 sessions
  3. 宽度 breadth    : share of members closing above their MA20
  4. 信号共振 signal : scanner pattern signals per 100 members (latest report)

The composite score is a transparent weighted min-max blend
(0.35/0.30/0.20/0.15).  Detection is 100% reproducible: same data -> same
result.  Whether a sector CONTINUES leading next month is a probability, not
a guarantee; the output says so explicitly.

Guard: only runs on the first trading day of the month (--force overrides,
e.g. initial backfill).  Outputs:
  data/reports/mainline/YYYYMM.json (audit) + docs/data/mainline.json (site).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ashare_monitor.data import _ts_pro  # noqa: E402

WEIGHTS = {"momentum": 0.35, "flow": 0.30, "breadth": 0.20, "signal": 0.15}
MIN_MEMBERS = 10
TOP_N = 5


def minmax(values: pd.Series) -> pd.Series:
    lo, hi = values.min(), values.max()
    if hi <= lo:
        return pd.Series(0.5, index=values.index)
    return (values - lo) / (hi - lo)


def is_first_trading_day_of_month(pro) -> bool:
    today = date.today()
    cal = pro.trade_cal(exchange="SSE", start_date=today.strftime("%Y%m"),
                        end_date=today.strftime("%Y%m"), is_open="1")
    if cal is None or cal.empty:
        return False
    first = cal["cal_date"].astype(str).min()
    return first == today.strftime("%Y%m%d")


def load_members(pro) -> pd.DataFrame:
    raw = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    raw = raw.rename(columns={"ts_code": "symbol"})
    raw["symbol"] = raw["symbol"].astype(str).str[:6]
    raw = raw[raw.industry.notna() & (raw.industry != "")]
    return raw


def momentum_factor(members: pd.DataFrame, cache_dir: Path, sessions: int = 20) -> pd.Series:
    rows = []
    for symbol in members.symbol:
        path = cache_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            closes = pd.read_csv(path, usecols=["close"])["close"].dropna()
        except Exception:
            continue
        if len(closes) < sessions + 1:
            continue
        rows.append((symbol, closes.iloc[-1] / closes.iloc[-sessions - 1] - 1))
    frame = pd.DataFrame(rows, columns=["symbol", "ret"]).merge(members, on="symbol")
    return frame.groupby("industry")["ret"].median()


def breadth_factor(members: pd.DataFrame, cache_dir: Path) -> pd.Series:
    rows = []
    for symbol in members.symbol:
        path = cache_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            closes = pd.read_csv(path, usecols=["close"])["close"].dropna()
        except Exception:
            continue
        if len(closes) < 21:
            continue
        rows.append((symbol, float(closes.iloc[-1] > closes.rolling(20).mean().iloc[-1])))
    frame = pd.DataFrame(rows, columns=["symbol", "above"]).merge(members, on="symbol")
    return frame.groupby("industry")["above"].mean()


def flow_factor(pro, members: pd.DataFrame, sessions: int = 5) -> pd.Series:
    days: list[pd.DataFrame] = []
    end = date.today()
    cursor = end
    while len(days) < sessions:
        day = cursor.strftime("%Y%m%d")
        try:
            raw = pro.moneyflow(trade_date=day)
        except Exception:
            raw = None
        if raw is not None and not raw.empty:
            frame = raw.copy()
            frame["symbol"] = frame["ts_code"].astype(str).str[:6]
            frame["net_mf_amount"] = pd.to_numeric(frame["net_mf_amount"], errors="coerce")
            days.append(frame[["symbol", "net_mf_amount"]])
        cursor -= timedelta(days=1)
        if cursor < end - timedelta(days=20):
            break
    if not days:
        return pd.Series(dtype=float)
    flow = pd.concat(days).groupby("symbol")["net_mf_amount"].sum().reset_index()
    merged = members.merge(flow, on="symbol", how="left")
    merged["net_mf_amount"] = merged["net_mf_amount"].fillna(0.0)
    return merged.groupby("industry")["net_mf_amount"].mean()


def signal_factor(members: pd.DataFrame, reports_dir: Path) -> pd.Series:
    signals_path = reports_dir / date.today().strftime("%Y%m%d") / "signals.json"
    if not signals_path.exists():
        newest = sorted(reports_dir.glob("*/signals.json"))
        if not newest:
            return pd.Series(dtype=float)
        signals_path = newest[-1]
    signals = pd.DataFrame(json.loads(signals_path.read_text(encoding="utf-8")))
    if signals.empty or "symbol" not in signals.columns:
        return pd.Series(dtype=float)
    density = signals.groupby("symbol").size().reset_index(name="n")
    merged = members.merge(density, on="symbol", how="left").fillna(0)
    return merged.groupby("industry").apply(lambda g: g["n"].sum() / max(len(g), 1) * 100, include_groups=False)


def compute_mainline(pro, members: pd.DataFrame, cache_dir: Path, reports_dir: Path) -> dict:
    mom = momentum_factor(members, cache_dir)
    flow = flow_factor(pro, members)
    brd = breadth_factor(members, cache_dir)
    sig = signal_factor(members, reports_dir)

    table = pd.DataFrame(index=mom.index)
    table["momentum"] = mom
    table["flow"] = flow.reindex(table.index)
    table["breadth"] = brd.reindex(table.index)
    table["signal"] = sig.reindex(table.index)
    table = table.dropna(subset=["momentum", "breadth"])
    sizes = members.groupby("industry").size()
    table["members"] = sizes.reindex(table.index)
    table = table[table.members >= MIN_MEMBERS]

    score = (
        WEIGHTS["momentum"] * minmax(table.momentum)
        + WEIGHTS["flow"] * minmax(table.flow)
        + WEIGHTS["breadth"] * minmax(table.breadth)
        + WEIGHTS["signal"] * minmax(table.signal)
    )
    table["score"] = (score * 100).round(1)
    table = table.sort_values("score", ascending=False)

    top = []
    member_ret = None
    for industry in table.head(TOP_N).index:
        ind_members = members[members.industry == industry].symbol
        # strongest members by 20d return
        if member_ret is None:
            member_ret = momentum_factor(members, cache_dir).to_frame() if False else None
        rets = []
        for symbol in ind_members:
            path = cache_dir / f"{symbol}.csv"
            if not path.exists():
                continue
            try:
                closes = pd.read_csv(path, usecols=["close"])["close"].dropna()
            except Exception:
                continue
            if len(closes) < 21:
                continue
            rets.append((symbol, float(closes.iloc[-1] / closes.iloc[-21] - 1)))
        rets.sort(key=lambda x: -x[1])
        names = dict(zip(members.symbol, members.name))
        top.append({
            "industry": industry,
            "score": float(table.loc[industry, "score"]),
            "momentum_20d_pct": round(float(table.loc[industry, "momentum"]) * 100, 2),
            "flow_5d_avg_10k": round(float(table.loc[industry, "flow"]), 1),
            "breadth_pct": round(float(table.loc[industry, "breadth"]) * 100, 1),
            "signal_density": round(float(table.loc[industry, "signal"]), 2),
            "members": int(table.loc[industry, "members"]),
            "top_stocks": [
                {"symbol": s, "name": str(names.get(s, s)), "ret_20d_pct": round(r * 100, 2)}
                for s, r in rets[:3]
            ],
        })
    return {
        "as_of": date.today().strftime("%Y%m%d"),
        "method": {
            "weights": WEIGHTS,
            "window": {"momentum": "20 个交易日", "flow": "近 5 个交易日主力净流入均值（万元）",
                        "breadth": "站上 MA20 成员占比", "signal": "每 100 只成员信号数"},
        },
        "top_sectors": top,
        "warning": "主线判定为规则化的近期强度测量，可 100% 复现；板块下月是否延续为概率问题，不构成投资建议。",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="跳过月初守卫（手动/回填）")
    args = parser.parse_args()

    pro = _ts_pro()
    if pro is None:
        print("Tushare 不可用，中止。")
        raise SystemExit(2)
    if not args.force and not is_first_trading_day_of_month(pro):
        print("今天不是本月首个交易日，跳过主线判定。")
        return

    members = load_members(pro)
    cache_dir = ROOT / "data" / "cache"
    reports_dir = ROOT / "data" / "reports"
    result = compute_mainline(pro, members, cache_dir, reports_dir)

    month_dir = reports_dir / "mainline"
    month_dir.mkdir(parents=True, exist_ok=True)
    audit = month_dir / f"{result['as_of']}.json"
    audit.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    site_out = ROOT / "docs" / "data" / "mainline.json"
    site_out.parent.mkdir(parents=True, exist_ok=True)
    site_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"主线判定完成（{result['as_of']}）→ {site_out}")
    for s in result["top_sectors"]:
        print(f"  {s['score']:5.1f}  {s['industry']}  动量{s['momentum_20d_pct']:+.1f}% "
              f"资金{s['flow_5d_avg_10k']:+.0f}万 宽度{s['breadth_pct']:.0f}% 成员{s['members']}")


if __name__ == "__main__":
    main()
