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

# 两年回测（2024.09-2026.08，训练/验证分离）确定的口径：
# 月度板块呈现均值回归——追高动量组合验证期 top1 命中 27-46%（低于随机50%），
# 而"蓄势轮动"（低动量 + 高宽度 + 资金流入）验证期 top1 命中 54.5%、
# top3 平均分位 0.549，显著优于随机基线。signal 因子未纳入回测，不计分仅展示。
WEIGHTS = {"momentum": -0.45, "flow": 0.10, "breadth": 0.45, "signal": 0.0}
MIN_MEMBERS = 10
TOP_N = 5

# 东财细分行业 -> 主流可交易行业 ETF（名称关键词匹配 + 手动精选流动性好的）
SECTOR_ETF = [
    (("证券", "多元金融"), "512880.SH", "券商ETF"),
    (("白酒", "饮料制造"), "512690.SH", "酒ETF"),
    (("半导体", "元件", "光学光电子", "电子化学品"), "512480.SH", "半导体ETF"),
    (("通信设备", "通信服务"), "515880.SH", "通信ETF"),
    (("软件开发", "计算机设备", "IT服务"), "512720.SH", "计算机ETF"),
    (("银行",), "512800.SH", "银行ETF"),
    (("港口", "航运港口", "物流", "铁路公路"), "159766.SZ", "运输ETF"),
    (("贵金属",), "518880.SH", "黄金ETF"),
    (("油气开采", "石油加工", "油服工程"), "561360.SH", "油气ETF"),
    (("工业金属", "小金属"), "159980.SZ", "有色ETF"),
    (("煤炭开采", "煤炭加工"), "515220.SH", "煤炭ETF"),

    (("汽车整车", "汽车零部件", "乘用车", "商用车"), "516110.SH", "汽车ETF"),
    (("化学制药", "生物制品", "中药", "医药商业", "医疗服务", "医疗器械"), "512010.SH", "医药ETF"),
    (("光伏设备", "风电设备"), "515790.SH", "光伏ETF"),
    (("电池",), "561910.SH", "电池ETF"),
    (("电力", "燃气", "电力行业"), "561560.SH", "电力ETF"),
    (("种植业", "农产品加工", "养殖业", "渔业", "饲料", "农业综合"), "159825.SZ", "农业ETF"),
    (("房地产开发", "房地产服务"), "512200.SH", "房地产ETF"),
    (("白色家电", "家电零部件", "厨卫电器"), "159996.SZ", "家电ETF"),
    (("游戏", "数字媒体", "影视院线", "广告营销", "出版", "电视广播"), "512980.SH", "传媒ETF"),
    (("保险",), "512070.SH", "保险ETF"),
    (("船舶", "航海装备", "航空装备", "航天装备", "地面兵装", "军工电子"), "512660.SH", "军工ETF"),
]


_ETF_OFFICIAL: dict[str, str] | None = None


def load_etf_official(pro) -> None:
    """拉取场内基金官方全名（判定日一次）；用于保证用户可搜索到真实 ETF。"""
    global _ETF_OFFICIAL
    try:
        fund = pro.fund_basic(market="E")
        official = dict(zip(fund["ts_code"], fund["name"]))
        mgmt = dict(zip(fund["ts_code"], fund["management"]))
        stripped: dict[str, str] = {}
        for code, name in official.items():
            company = str(mgmt.get(code, ""))
            short = name
            if company and name.startswith(company):
                short = name[len(company):]
            # 再剥一层常见简称（如 国泰 -> 无）；保底保留原名
            stripped[code] = short if short else name
        _ETF_OFFICIAL = stripped
    except Exception:
        _ETF_OFFICIAL = {}


def sector_etf(industry: str) -> dict | None:
    for keywords, code, label in SECTOR_ETF:
        if any(k in industry for k in keywords):
            official = (_ETF_OFFICIAL or {}).get(code)
            # 显示名 = 官方名（去公司前缀，仍是官方名的子串，搜索必命中）；
            # 若官方名缺失则退回手工 label，并保留代码可查证。
            display = official or label
            return {"code": code, "label": display, "official": official or None}
    return None


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


def per_stock_flow(pro, members: pd.DataFrame, sessions: int = 5) -> pd.Series:
    """个股级近 N 日主力净流入合计（万元），用于板块内选股（回测 v1 口径）。"""
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
    flow = pd.concat(days).groupby("symbol")["net_mf_amount"].sum()
    return flow.reindex(members.symbol).fillna(0.0)


def compute_mainline(pro, members: pd.DataFrame, cache_dir: Path, reports_dir: Path) -> dict:
    load_etf_official(pro)
    mom = momentum_factor(members, cache_dir)
    flow = flow_factor(pro, members)
    brd = breadth_factor(members, cache_dir)
    sig = signal_factor(members, reports_dir)
    stock_flow = per_stock_flow(pro, members)

    table = pd.DataFrame(index=mom.index)
    table["momentum"] = mom
    table["flow"] = flow.reindex(table.index)
    table["breadth"] = brd.reindex(table.index)
    table["signal"] = sig.reindex(table.index)
    table = table.dropna(subset=["momentum", "breadth"])
    sizes = members.groupby("industry").size()
    table["members"] = sizes.reindex(table.index)
    table = table[table.members >= MIN_MEMBERS]

    total_w = sum(abs(w) for w in WEIGHTS.values())
    score = (
        (WEIGHTS["momentum"] / total_w) * minmax(table.momentum)
        + (WEIGHTS["flow"] / total_w) * minmax(table.flow)
        + (WEIGHTS["breadth"] / total_w) * minmax(table.breadth)
        + (WEIGHTS["signal"] / total_w) * minmax(table.signal)
    )
    table["score"] = (score * 100).round(1)
    table = table.sort_values("score", ascending=False)

    top = []
    names = dict(zip(members.symbol, members.name))
    total_sectors = int(table.shape[0])
    for rank_pos, industry in enumerate(table.head(TOP_N).index, start=1):
        ind_members = members[members.industry == industry].symbol
        # 推荐个股：板块内近 5 日主力净流入最高 3 只（回测验证 v1 口径）；剔除 ST
        ind_members = ind_members[
            ~ind_members.map(lambda s: "ST" in str(names.get(s, "")).upper())]
        flows = stock_flow.reindex(ind_members).dropna().sort_values(ascending=False)
        recs = []
        for symbol in flows.index[:3]:
            path = cache_dir / f"{symbol}.csv"
            ret = None
            if path.exists():
                try:
                    closes = pd.read_csv(path, usecols=["close"])["close"].dropna()
                    if len(closes) >= 21:
                        ret = float(closes.iloc[-1] / closes.iloc[-21] - 1) * 100
                except Exception:
                    pass
            recs.append({
                "symbol": symbol,
                "name": str(names.get(symbol, symbol)),
                "flow_5d_10k": round(float(flows[symbol]), 1),
                "ret_20d_pct": round(ret, 2) if ret is not None else None,
            })
        etf = sector_etf(industry)
        rank_pctile = round((1 - (rank_pos - 1) / max(total_sectors - 1, 1)) * 100, 1)
        top.append({
            "industry": industry,
            "rank": rank_pos,
            "rank_total": total_sectors,
            "rank_pctile": rank_pctile,
            "momentum_20d_pct": round(float(table.loc[industry, "momentum"]) * 100, 2),
            "flow_5d_avg_10k": round(float(table.loc[industry, "flow"]), 1),
            "breadth_pct": round(float(table.loc[industry, "breadth"]) * 100, 1),
            "signal_density": round(float(table.loc[industry, "signal"]), 2),
            "members": int(table.loc[industry, "members"]),
            "etf": etf,
            "top_stocks": recs,
        })
    return {
        "as_of": date.today().strftime("%Y%m%d"),
        "method": {
            "weights": WEIGHTS,
            "rationale": "蓄势轮动口径（经两年回测验证）：主线预判不追高动量，优选横盘蓄势充分（高宽度）且尚未大涨（低动量）的板块，资金流入为佐证。排名为相对优先级，不代表板块当前涨幅或未来涨幅。",
            "window": {"momentum": "20 个交易日", "flow": "近 5 个交易日主力净流入均值（万元）",
                        "breadth": "站上 MA20 成员占比", "signal": "每 100 只成员信号数"},
        },
        "top_sectors": top,
        "warning": "蓄势轮动判定为规则化测量，可 100% 复现；历史回测验证期 top1 命中约 54.5%，并非 100%，板块走势为概率问题，不构成投资建议。",
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
        print(f"  #{s['rank']}/{s['rank_total']}  {s['industry']}  动量{s['momentum_20d_pct']:+.1f}% "
              f"资金{s['flow_5d_avg_10k']:+.0f}万 宽度{s['breadth_pct']:.0f}% 成员{s['members']}")


if __name__ == "__main__":
    main()
