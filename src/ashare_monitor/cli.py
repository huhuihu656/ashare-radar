from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import sys

import pandas as pd
from rich.console import Console
from rich.progress import track

from .config import Config, load
from .data import (append_live_bar, filter_universe, get_universe, history_for,
                   is_ashare_trading_day, polite_pause)
from .signals import scan_frame

console = Console()


def _scan_one(quote: pd.Series, cfg: Config, cache_dir: Path) -> list[dict]:
    symbol = str(quote.symbol)
    history = history_for(symbol, cache_dir, cfg.scan.lookback_days)
    if len(history) < cfg.scan.min_history_days:
        return []
    frame = append_live_bar(history, quote)
    rows = scan_frame(frame, cfg.support_retest, cfg.breakout)
    for row in rows:
        row.update({"symbol": symbol, "name": str(quote["name"]), "board": str(quote["board"]),
                    "scan_time": datetime.now().astimezone().isoformat(timespec="seconds")})
    polite_pause()
    return rows


def scan(config_path: str) -> int:
    cfg = load(config_path)
    try:
        if not is_ashare_trading_day():
            console.print("[yellow]今天不是 A 股交易日，已跳过扫描。[/yellow]")
            return 0
    except Exception as error:
        console.print(f"[red]无法核验 A 股交易日历，为避免使用过期行情已停止：{error}[/red]")
        return 2
    try:
        universe = filter_universe(get_universe(), cfg.scan.exclude_st, cfg.scan.include_boards)
    except Exception as error:
        console.print(f"[red]无法获取实时股票池：{error}[/red]")
        return 2
    cache_dir = Path(cfg.scan.cache_dir)
    report_dir = Path(cfg.scan.output_dir) / datetime.now().strftime("%Y%m%d")
    report_dir.mkdir(parents=True, exist_ok=True)
    signals: list[dict] = []
    failures: list[dict] = []
    records = [row for _, row in universe.iterrows()]
    with ThreadPoolExecutor(max_workers=cfg.scan.workers) as pool:
        future_map = {pool.submit(_scan_one, row, cfg, cache_dir): row for row in records}
        for future in track(as_completed(future_map), total=len(future_map), description="扫描 A 股"):
            quote = future_map[future]
            try:
                signals.extend(future.result())
            except Exception as error:
                failures.append({"symbol": str(quote.symbol), "error": str(error)[:300]})
    columns = ["symbol", "name", "board", "signal", "score", "close", "volume", "scan_time", "note",
               "start_date", "start_price", "distance_to_start_pct", "prior_rally_pct", "ma20", "ma60",
               "range_pct", "atr_pct", "volume_ratio", "breakout_high", "close_position"]
    result = pd.DataFrame(signals)
    if result.empty:
        result = pd.DataFrame(columns=columns)
    else:
        result = result.reindex(columns=columns).sort_values(["signal", "score"], ascending=[True, False])
    result.to_csv(report_dir / "signals.csv", index=False, encoding="utf-8-sig")
    (report_dir / "signals.json").write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {
        "scan_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "universe_count": len(records), "signal_count": len(signals), "failure_count": len(failures),
        "failures": failures, "config": str(Path(config_path).resolve()),
        "warning": "研究预警；默认“资金进入”仅是实时量价代理，非真实主力资金流。",
    }
    (report_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]完成：{len(records)} 只覆盖，{len(signals)} 条信号。[/green] {report_dir.resolve()}")
    if failures:
        console.print(f"[yellow]{len(failures)} 只数据失败，详见 run.json；不要将本次结果视作全市场完整覆盖。[/yellow]")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股收盘前研究信号扫描器（不交易）")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan")
    scan_parser.add_argument("--config", default="config.yaml")
    commands.add_parser("universe")
    args = parser.parse_args()
    if args.command == "universe":
        universe = get_universe()
        console.print(f"实时股票池：{len(universe)} 只；字段：{', '.join(universe.columns[:10])}")
        raise SystemExit(0)
    raise SystemExit(scan(args.config))


if __name__ == "__main__":
    main()
