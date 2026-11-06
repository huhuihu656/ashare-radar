"""Export the newest completed scanner report to a static dashboard payload.

The dashboard is deliberately static: it can be served by GitHub Pages without
putting scanner credentials, brokerage access, or a Python server on the
public internet.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json

import pandas as pd
from pathlib import Path
import sys
from typing import Any


SCHEMA_VERSION = 1


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_latest_report(reports_dir: Path) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Return the most recent complete report directory, not merely its name."""
    if not reports_dir.exists():
        raise FileNotFoundError(f"报告目录不存在：{reports_dir}")
    candidates = sorted((path for path in reports_dir.iterdir() if path.is_dir()), reverse=True)
    for report_dir in candidates:
        signals_path = report_dir / "signals.json"
        run_path = report_dir / "run.json"
        if signals_path.exists() and run_path.exists():
            signals = _read_json(signals_path)
            run = _read_json(run_path)
            if not isinstance(signals, list) or not isinstance(run, dict):
                raise ValueError(f"报告格式无效：{report_dir}")
            return report_dir, signals, run
    raise FileNotFoundError(f"没有完整的 signals.json + run.json 报告：{reports_dir}")


def coverage_of(run: dict[str, Any]) -> float:
    total = int(run.get("universe_count", 0) or 0)
    failures = int(run.get("failure_count", 0) or 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, (total - failures) / total))


SIGNAL_COUNT_KEYS = {
    "清晰上升趋势": "clear_uptrend",
}


def build_payload(report_dir: Path, signals: list[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
    counts = {key: 0 for key in SIGNAL_COUNT_KEYS.values()}
    for item in signals:
        key = SIGNAL_COUNT_KEYS.get(item.get("signal"))
        if key:
            counts[key] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": report_dir.name,
        "generated_at": run.get("scan_time"),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_count": int(run.get("universe_count", 0) or 0),
        "failure_count": int(run.get("failure_count", 0) or 0),
        "coverage": round(coverage_of(run), 4),
        "signal_counts": counts,
        "warning": "研究预警，不构成投资建议；“资金进入”为量价代理，不是已证实的资金流。",
        "signals": signals,
    }


KLINE_DAY_BARS = 90   # 日线：MA60 有 30 个可见点
KLINE_WEEK_BARS = 64  # 周线
KLINE_MONTH_BARS = 48 # 月线


def _to_bars(frame: pd.DataFrame) -> list[list[Any]]:
    """Compact [yyyymmdd, open, high, low, close, volume] arrays."""
    bars: list[list[Any]] = []
    for _, row in frame.iterrows():
        bars.append([
            int(row.date.strftime("%Y%m%d")),
            round(float(row.open), 2),
            round(float(row.high), 2),
            round(float(row.low), 2),
            round(float(row.close), 2),
            int(row.volume),
        ])
    return bars


def aggregate_week(frame: pd.DataFrame) -> pd.DataFrame:
    """ISO-week OHLCV aggregation: first open / max high / min low / last close / volume sum."""
    agg = (frame.assign(_week=frame.date.dt.isocalendar().week,
                        _year=frame.date.dt.isocalendar().year)
               .groupby(["_year", "_week"], as_index=False)
               .agg(date=("date", "max"), open=("open", "first"),
                    high=("high", "max"), low=("low", "min"),
                    close=("close", "last"), volume=("volume", "sum")))
    return agg[["date", "open", "high", "low", "close", "volume"]].sort_values("date")


def aggregate_month(frame: pd.DataFrame) -> pd.DataFrame:
    """Calendar-month OHLCV aggregation."""
    agg = (frame.assign(_ym=frame.date.dt.to_period("M"))
               .groupby(["_ym"], as_index=False)
               .agg(date=("date", "max"), open=("open", "first"),
                    high=("high", "max"), low=("low", "min"),
                    close=("close", "last"), volume=("volume", "sum")))
    return agg[["date", "open", "high", "low", "close", "volume"]].sort_values("date")


def build_klines(signals: list[dict[str, Any]], cache_dir: Path) -> dict[str, Any]:
    """Back-end multi-granularity K-line series per signal symbol.

    Aggregation happens here once per scan (week/month buckets built from the
    daily bars); the frontend receives ready-made `day` / `week` / `month`
    series and only renders the selected granularity -- no client-side rolling
    of raw detail data.
    """
    stocks: dict[str, Any] = {}
    seen: set[str] = set()
    for item in signals:
        symbol = str(item.get("symbol") or "").zfill(6)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        path = cache_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path, parse_dates=["date"]).sort_values("date")
        except Exception:
            continue
        if frame.empty:
            continue
        tail = frame.tail(KLINE_DAY_BARS)
        week = aggregate_week(frame).tail(KLINE_WEEK_BARS)
        month = aggregate_month(frame).tail(KLINE_MONTH_BARS)
        stocks[symbol] = {
            "series": {
                "day": _to_bars(tail),
                "week": _to_bars(week),
                "month": _to_bars(month),
            },
            "as_of": int(tail.date.iloc[-1].strftime("%Y%m%d")),
        }
    return {
        "schema_version": 2,
        "granularities": ["day", "week", "month"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stocks": stocks,
    }


BOARD_LIMIT = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}


def build_universe(report_dir: Path, cache_dir: Path) -> dict[str, Any]:
    """全市场逐只技术指标与买卖参考价，供网页搜索任意个股。

    「实时」只能到最近一次收盘：静态站没有行情 API，盘中价格无法获取。
    """
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    from ashare_monitor.config import load
    from ashare_monitor.signals import clear_uptrend, uptrend_metrics

    cfg = load(root / "config.yaml")
    uni_path = report_dir / "universe.json"
    if not uni_path.exists():
        return {"as_of": report_dir.name, "params": {}, "stocks": {}}
    try:
        universe = _read_json(uni_path)
    except Exception:
        return {"as_of": report_dir.name, "params": {}, "stocks": {}}

    c = cfg.clear_uptrend
    stocks: dict[str, Any] = {}
    for item in universe:
        sym = str(item.get("symbol") or "").zfill(6)
        path = cache_dir / f"{sym}.csv"
        if not sym or not path.exists():
            continue
        try:
            frame = pd.read_csv(path, parse_dates=["date"]).sort_values("date").set_index("date")
            frame = frame[~frame.index.duplicated(keep="last")].dropna(
                subset=["open", "high", "low", "close", "volume"])
        except Exception:
            continue
        if len(frame) < c.min_history_days:
            continue
        f = frame.tail(cfg.scan.lookback_days)
        limit_pct = BOARD_LIMIT.get(str(item.get("board")), 0.10)
        m = uptrend_metrics(f, c, limit_pct)
        if m is None:
            continue
        qualified = clear_uptrend(f, c, cfg.risk, limit_pct) is not None
        entry = round(m["px"], 2)
        stop = round(m["ma20"] * 0.97, 2)
        stocks[sym] = {
            "name": item.get("name") or sym,
            "board": item.get("board") or "",
            "close": entry,
            "er": round(m["er"], 3),
            "gain60": round(m["gain_60"], 4),
            "mdd": round(m["mdd"], 4),
            "ext": round(m["ext"], 4),
            "p95": round(m["p95"], 4),
            "ma20": round(m["ma20"], 3),
            "ma20prev": round(m["ma20_prev"], 3),
            "ma60": round(m["ma60"], 3),
            "lim": m["limit_ups"],
            "lows": bool(m["higher_lows"]),
            "highs": bool(m["higher_highs"]),
            "q": qualified,
            "entry": entry if qualified else None,
            "stop": stop if qualified else None,
            "target": round(entry + 3 * (entry - stop), 2) if qualified else None,
        }
    return {
        "as_of": report_dir.name,
        "params": {
            "min_gain_60": c.min_gain_60,
            "max_ext_pct": c.max_ext_pct,
            "min_efficiency": c.min_efficiency,
            "max_daily_p95": c.max_daily_p95,
            "max_drawdown": c.max_drawdown,
            "max_limit_ups": c.max_limit_ups,
            "ma20_slope_days": c.ma20_slope_days,
        },
        "stocks": stocks,
    }


def export(reports_dir: Path, output: Path, min_coverage: float = 0.0,
           klines_output: Path | None = None) -> dict[str, Any]:
    report_dir, signals, run = find_latest_report(reports_dir)
    payload = build_payload(report_dir, signals, run)
    if payload["coverage"] < min_coverage:
        raise RuntimeError(
            f"本次覆盖率 {payload['coverage']:.1%} 低于发布门槛 {min_coverage:.1%}；保留已上线的旧数据。"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if klines_output is not None:
        klines = build_klines(signals, reports_dir.parent / "cache")
        klines_output.parent.mkdir(parents=True, exist_ok=True)
        klines_output.write_text(json.dumps(klines, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return payload


def archive_payload(payload: dict[str, Any], archive_dir: Path) -> None:
    """每日档案：docs/data/archive/<as_of>.json + 倒序索引 index.json。

    网站可切换任意历史日期查看当日全局数据（候选表/统计/主线均按当日存档）。
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    day = str(payload.get("as_of") or "")
    if not day:
        return
    (archive_dir / f"{day}.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    days = sorted((f.stem for f in archive_dir.glob("*.json") if f.stem != "index"), reverse=True)
    (archive_dir / "index.json").write_text(
        json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出静态股票监测网页数据")
    parser.add_argument("--reports-dir", default="data/reports")
    parser.add_argument("--out", default="docs/data/latest.json")
    parser.add_argument("--min-coverage", type=float, default=0.0)
    parser.add_argument("--klines-out", default=None, help="K线数据输出（默认与 --out 同目录的 klines.json）")
    parser.add_argument("--archive-dir", default=None, help="每日档案目录（默认与 --out 同目录的 archive/）")
    args = parser.parse_args()
    if not 0 <= args.min_coverage <= 1:
        parser.error("--min-coverage 必须在 0 到 1 之间")
    klines_out = Path(args.klines_out) if args.klines_out else Path(args.out).parent / "klines.json"
    archive_dir = Path(args.archive_dir) if args.archive_dir else Path(args.out).parent / "archive"
    try:
        payload = export(Path(args.reports_dir), Path(args.out), args.min_coverage, klines_out)
        archive_payload(payload, archive_dir)
        report_dir, _, _ = find_latest_report(Path(args.reports_dir))
        universe = build_universe(report_dir, Path(args.reports_dir).parent / "cache")
        universe_out = Path(args.out).parent / "universe.json"
        universe_out.parent.mkdir(parents=True, exist_ok=True)
        universe_out.write_text(json.dumps(universe, ensure_ascii=False, separators=(",", ":")),
                                encoding="utf-8")
        print(f"Universe payload: {len(universe.get('stocks', {}))} stocks -> {universe_out}")
    except Exception as error:
        print(f"Dashboard export failed: {error}", file=sys.stderr)
        return 2
    print(
        f"Published payload: {payload['as_of']} | {payload['universe_count']} stocks | "
        f"{payload['coverage']:.1%} coverage | {len(payload['signals'])} signals"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
