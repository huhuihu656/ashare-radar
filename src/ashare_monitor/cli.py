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
from .data import (filter_universe, get_universe, history_for,
                   index_frame, is_ashare_trading_day, market_regime, polite_pause)
from .data import refresh_history_cache_bulk
from .signals import entry_exit_plan, position_strategy, scan_frame
from .tushare_src import latest_moneyflow

LIMIT_PCT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}

console = Console()


def _scan_one(quote: pd.Series, cfg: Config, cache_dir: Path, market_state: str,
             moneyflow: pd.DataFrame | None) -> list[dict]:
    symbol = str(quote.symbol)
    history = history_for(symbol, cache_dir, cfg.scan.lookback_days)
    if len(history) < cfg.scan.min_history_days:
        return []
    # 收盘后扫描：缓存已含今日真实收盘 bar（refresh_history_cache_bulk 拉当日），
    # 直接用最后一行即可，不再构造盘中模拟 bar。
    frame = history
    limit_pct = LIMIT_PCT_BY_BOARD.get(str(quote["board"]), 0.10)
    rows = scan_frame(frame, cfg.support_retest, cfg.breakout, cfg.risk,
                      cfg.box_breakout, cfg.bullish_engulfing, cfg.limitup_gap,
                      cfg.dragon_pullback, cfg.ma_divergence, cfg.low_shadow,
                      limit_pct=limit_pct)
    money = moneyflow.loc[symbol] if moneyflow is not None and symbol in moneyflow.index else None
    for row in rows:
        row.update({"symbol": symbol, "name": str(quote["name"]), "board": str(quote["board"]),
                    "market_env": market_state,
                    "scan_time": datetime.now().astimezone().isoformat(timespec="seconds")})
        if money is not None:
            net = float(money.net_mf_amount) if pd.notna(money.net_mf_amount) else None
            row["mf_date"] = str(money.mf_date)
            row["net_mf_amount"] = round(net, 1) if net is not None else None
            if net is not None:
                flow_tag = ("资金净流入" if net > 0 else "资金净流出")
                row["note"] = f"{row['note']} | {flow_tag} {abs(net)/10000:.1f}亿（{row.get('mf_date', '')}主力口径）"
        # 参考仓位策略（分数+大盘+资金流三重推导，研究参考，非投资建议）
        row.update(position_strategy(row, market_state))
        # 买卖点计划：形态确认位买入 / 证伪位止损 / 风险收益比推止盈
        row.update(entry_exit_plan(row))
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
    today_str = datetime.now().strftime("%Y%m%d")
    try:
        universe = filter_universe(get_universe(today_str), cfg.scan.exclude_st, cfg.scan.include_boards)
    except Exception as error:
        console.print(f"[red]无法获取当日收盘股票池（需 15:10 后运行）：{error}[/red]")
        return 2
    # 大盘环境：一次请求，写入审计并可选过滤信号。
    regime = market_regime(index_frame(), cfg.risk.weak_market_ma)
    console.print(f"[cyan]大盘环境：{regime['state']}（指数 {regime['close']}，MA20 {regime['ma20']}，MA60 {regime['ma60']}）[/cyan]")
    # 付费数据源（Tushare）：先增量刷新全市场历史缓存（批量、秒级），
    # 再拉真实主力资金流标注；任何一步失败都软降级。
    written, sessions = refresh_history_cache_bulk(Path(cfg.scan.cache_dir), cfg.scan.lookback_days)
    if sessions:
        console.print(f"[green]Tushare 行情缓存：更新 {written} 只 / {sessions} 个交易日[/green]")
    moneyflow = latest_moneyflow()
    if moneyflow is not None:
        console.print(f"[green]Tushare 资金流：{len(moneyflow)} 只（净流入 {int((moneyflow.net_mf_amount > 0).sum())} 只）[/green]")
    else:
        console.print("[yellow]Tushare 资金流不可用（token/积分不足），信号仅含量价代理。[/yellow]")
    cache_dir = Path(cfg.scan.cache_dir)
    report_dir = Path(cfg.scan.output_dir) / datetime.now().strftime("%Y%m%d")
    report_dir.mkdir(parents=True, exist_ok=True)
    signals: list[dict] = []
    failures: list[dict] = []
    records = [row for _, row in universe.iterrows()]
    with ThreadPoolExecutor(max_workers=cfg.scan.workers) as pool:
        future_map = {pool.submit(_scan_one, row, cfg, cache_dir, regime["state"], moneyflow): row for row in records}
        for future in track(as_completed(future_map), total=len(future_map), description="扫描 A 股"):
            quote = future_map[future]
            try:
                signals.extend(future.result())
            except Exception as error:
                failures.append({"symbol": str(quote.symbol), "error": str(error)[:300]})
    columns = ["symbol", "name", "board", "signal", "score", "close", "volume", "market_env", "scan_time", "note",
               "mf_date", "net_mf_amount",
               "position_pct", "position_tier", "position_reason",
               "entry_price", "stop_loss", "take_profit", "risk_reward", "entry_state", "plan_note",
               "start_date", "start_price", "distance_to_start_pct", "prior_rally_pct", "ma20", "ma60",
               "range_pct", "atr_pct", "volume_ratio", "breakout_high", "close_position",
               "box_high", "converge_ratio", "red_green_vol_ratio",
               "pullback_ratio", "prior2_gain_pct", "engulf_vol_ratio",
               "limit_date", "gap_size_pct", "days_since_limit", "pullback_vol_ratio",
               "wave_gain_pct", "pullback_pct", "second_vol_ratio", "prior_high",
               "ma_gap_pct", "shadow_ratio", "shadow_vol_ratio", "cover_vol_ratio", "prior_gain_60d_pct"]
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
        "market": regime,
        "warning": "研究预警；默认“资金进入”仅是实时量价代理，非真实主力资金流。技术形态为概率工具，"
                   "需结合位置、量能与市场环境三重验证，不构成投资建议。",
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
        # 收盘后模式：打印最近一个已收盘交易日的股票池概况。
        try:
            universe = get_universe(datetime.now().strftime("%Y%m%d"))
        except Exception as error:
            console.print(f"[red]{error}[/red]")
            raise SystemExit(2)
        console.print(f"收盘股票池：{len(universe)} 只")
        raise SystemExit(0)
    raise SystemExit(scan(args.config))


if __name__ == "__main__":
    main()
