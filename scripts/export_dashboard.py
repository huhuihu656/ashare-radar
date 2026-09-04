"""Export the newest completed scanner report to a static dashboard payload.

The dashboard is deliberately static: it can be served by GitHub Pages without
putting scanner credentials, brokerage access, or a Python server on the
public internet.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
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
    "回踩前期起涨位": "support_retest",
    "横盘后放量突破": "sideways_breakout",
    "箱体突破红肥绿瘦": "box_breakout",
    "阳包阴反包启动": "bullish_engulfing",
    "涨停跳空缺口共振": "limitup_gap",
    "龙回头二次启动": "dragon_pullback",
    "均线多头发散": "ma_divergence",
    "低位仙人指路": "low_shadow",
}


def build_payload(report_dir: Path, signals: list[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
    counts = {key: 0 for key in SIGNAL_COUNT_KEYS.values()}
    for item in signals:
        key = SIGNAL_COUNT_KEYS.get(item.get("signal"))
        if key:
            counts[key] += 1
    support = counts["support_retest"]
    breakout = counts["sideways_breakout"]
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


def export(reports_dir: Path, output: Path, min_coverage: float = 0.0) -> dict[str, Any]:
    report_dir, signals, run = find_latest_report(reports_dir)
    payload = build_payload(report_dir, signals, run)
    if payload["coverage"] < min_coverage:
        raise RuntimeError(
            f"本次覆盖率 {payload['coverage']:.1%} 低于发布门槛 {min_coverage:.1%}；保留已上线的旧数据。"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="导出静态股票监测网页数据")
    parser.add_argument("--reports-dir", default="data/reports")
    parser.add_argument("--out", default="docs/data/latest.json")
    parser.add_argument("--min-coverage", type=float, default=0.0)
    args = parser.parse_args()
    if not 0 <= args.min_coverage <= 1:
        parser.error("--min-coverage 必须在 0 到 1 之间")
    try:
        payload = export(Path(args.reports_dir), Path(args.out), args.min_coverage)
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
