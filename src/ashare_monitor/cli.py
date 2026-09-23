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
from .data import (_ts_pro, cache_latest_date, filter_universe, get_universe,
                   history_for, index_frame, is_ashare_trading_day,
                   market_regime, polite_pause)
from .data import refresh_history_cache_bulk, session_fetched
from .signals import entry_exit_plan, position_strategy, scan_frame
from .news_factor import news_counts, sort_score
from .tushare_src import latest_moneyflow

LIMIT_PCT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}

console = Console()


def _scan_one(quote: pd.Series, cfg: Config, cache_dir: Path, market_state: str,
             moneyflow: pd.DataFrame | None, day: str) -> list[dict]:
    symbol = str(quote.symbol)
    history = history_for(symbol, cache_dir, cfg.scan.lookback_days, as_of=day)
    if len(history) < cfg.scan.min_history_days:
        return []
    # 收盘后扫描：缓存已含当日真实收盘 bar，直接用；
    # 历史回填（--day）时缓存可能含更晚数据，必须切到基准日为止（防未来函数）。
    frame = history[history.index <= pd.Timestamp(day)]
    if len(frame) < cfg.scan.min_history_days:
        return []
    limit_pct = LIMIT_PCT_BY_BOARD.get(str(quote["board"]), 0.10)
    rows = scan_frame(frame, cfg.support_retest, cfg.breakout, cfg.risk,
                      cfg.box_breakout, cfg.bullish_engulfing, cfg.limitup_gap,
                      cfg.dragon_pullback, cfg.ma_divergence, cfg.low_shadow,
                      cfg.oversold_reversal, cfg.break_ma20, cfg.boll_pin,
                      cfg.ene_pullback,
                      limit_pct=limit_pct)
    money = moneyflow.loc[symbol] if moneyflow is not None and symbol in moneyflow.index else None
    for row in rows:
        row.update({"symbol": symbol, "name": str(quote["name"]), "board": str(quote["board"]),
                    "market_env": market_state,
                    "scan_time": _now_cn().isoformat(timespec="seconds")})
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


def _now_cn() -> datetime:
    """当前北京时间（云端 runner 本地时间是 UTC，交易日/收盘判断必须用北京时间）。"""
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now(_tz(_td(hours=8)))


def scan_day() -> str:
    """最新已完成交易日。

    收盘后（>=15:10）且今日为交易日 → 今日；否则回退到最近的交易日。
    保证任何时刻补跑（如开机补跑、次日晨跑）数据都标注正确日期。
    """
    from datetime import timedelta as _td

    now = _now_cn()
    today = now.strftime("%Y%m%d")
    days: list[str] = []
    last_error = None
    for attempt in range(3):
        try:
            pro = _ts_pro()
            if pro is not None:
                cal = pro.trade_cal(exchange="SSE",
                                    start_date=(now - _td(days=14)).strftime("%Y%m%d"),
                                    end_date=today, is_open="1")
                if cal is not None and not cal.empty:
                    days = sorted(cal["cal_date"].astype(str).tolist())
                    break
        except Exception as error:
            last_error = error
        import time as _time

        _time.sleep(2.0 + attempt * 2.0)
    if not days:
        # Tushare 日历不可用时的兜底：周一到周五视为交易日（节假日由扫描守卫再核）
        import calendar as _cal

        cursor = now.date()
        while _cal.weekday(cursor.year, cursor.month, cursor.day) >= 5:
            cursor -= _td(days=1)
        days = [cursor.strftime("%Y%m%d")]
        console.print(f"[yellow]Tushare 日历不可用（{last_error}），按工作日兜底：{days[0]}[/yellow]")
    if now.time() < datetime.strptime("15:10", "%H:%M").time():
        days = [d for d in days if d < today]
    if not days:
        raise RuntimeError("近14日无可用 A 股交易日，无法扫描。")
    return days[-1]


def scan(config_path: str, day: str | None = None, rebuild_cache: bool = False) -> int:
    cfg = load(config_path)
    backfill = bool(day)
    day = day or scan_day()
    console.print(f"[cyan]扫描基准日：{day}" + ("（历史回填）" if backfill else "") + "[/cyan]")
    today_str = day
    # 保护：Tushare 日线发布可能晚于任务时间（尤其 15:20 准点跑）；
    # 股票池获取失败时每 60s 重试，最多 8 分钟，避免"行情尚未发布"直接失败。
    import time as _t

    universe = None
    last_error = None
    for attempt in range(26):
        try:
            universe = filter_universe(get_universe(today_str), cfg.scan.exclude_st, cfg.scan.include_boards)
            break
        except Exception as error:
            last_error = error
            if attempt < 25:
                console.print(f"[yellow]股票池获取失败（{str(error)[:80]}），等待 60s 重试（{attempt + 1}/25）…[/yellow]")
                _t.sleep(60)
    if universe is None:
        console.print(f"[red]无法获取收盘股票池：{last_error}[/red]")
        return 2
    # 大盘环境：一次请求，写入审计并可选过滤信号。
    regime = market_regime(index_frame(), cfg.risk.weak_market_ma)
    console.print(f"[cyan]大盘环境：{regime['state']}（指数 {regime['close']}，MA20 {regime['ma20']}，MA60 {regime['ma60']}）[/cyan]")
    # 付费数据源（Tushare）：先增量刷新全市场历史缓存（批量、秒级），
    # 再拉真实主力资金流标注；任何一步失败都软降级。
    cache_dir = Path(cfg.scan.cache_dir)
    written, sessions = refresh_history_cache_bulk(cache_dir, cfg.scan.lookback_days, force=rebuild_cache)
    if sessions:
        console.print(f"[green]Tushare 行情缓存：更新 {written} 只 / {sessions} 个交易日[/green]")
    # 保护：Tushare 日线发布可能晚于任务时间；等待当日缓存就绪（最多 8 分钟）
    import time as _time

    def cache_ready() -> bool:
        # 记账优先：cache_latest_date 取的是"任一文件"的最大日期，
        # 只要有一只股票新鲜就会把整体判为就绪，掩盖其余股票的陈旧与缺洞。
        if session_fetched(cache_dir, day):
            return True
        latest = cache_latest_date(cache_dir)
        if latest is None:
            return False
        return latest >= day if backfill else latest == day

    for attempt in range(25):
        if cache_ready():
            break
        console.print(f"[yellow]({day})行情尚未入库，等待 60s 重试（{attempt + 1}/25）…[/yellow]")
        _time.sleep(60)
        refresh_history_cache_bulk(cache_dir, cfg.scan.lookback_days, force=rebuild_cache)
    if not cache_ready():
        console.print(f"[red]({day})行情仍未入库，为避免用旧行情误标，停止本次扫描。[/red]")
        return 2
    moneyflow = latest_moneyflow()
    if moneyflow is not None:
        console.print(f"[green]Tushare 资金流：{len(moneyflow)} 只（净流入 {int((moneyflow.net_mf_amount > 0).sum())} 只）[/green]")
    else:
        console.print("[yellow]Tushare 资金流不可用（token/积分不足），信号仅含量价代理。[/yellow]")
    cache_dir = Path(cfg.scan.cache_dir)
    report_dir = Path(cfg.scan.output_dir) / day
    report_dir.mkdir(parents=True, exist_ok=True)
    signals: list[dict] = []
    failures: list[dict] = []
    records = [row for _, row in universe.iterrows()]
    with ThreadPoolExecutor(max_workers=cfg.scan.workers) as pool:
        future_map = {pool.submit(_scan_one, row, cfg, cache_dir, regime["state"], moneyflow, day): row for row in records}
        for future in track(as_completed(future_map), total=len(future_map), description="扫描 A 股"):
            quote = future_map[future]
            try:
                signals.extend(future.result())
            except Exception as error:
                failures.append({"symbol": str(quote.symbol), "error": str(error)[:300]})
    # 利好消息因子：每条信号附加近3日利好新闻数（东财公开资讯，文本非行情源）
    news = news_counts([str(s["symbol"]) for s in signals],
                       Path(cfg.scan.cache_dir).parent / "news_cache")
    for row in signals:
        info = news.get(str(row["symbol"]), {})
        row["news_count"] = info.get("news_count", 0)
        row["news_total"] = info.get("news_total", 0)
        row["top_headline"] = info.get("top_headline", "")
        row["sort_score"] = sort_score(float(row.get("score") or 0), int(row.get("news_count") or 0))
    columns = ["symbol", "name", "board", "signal", "score", "sort_score", "close", "volume", "market_env", "scan_time", "note",
               "mf_date", "net_mf_amount",
               "position_pct", "position_tier", "position_reason",
               "entry_price", "stop_loss", "take_profit", "risk_reward", "entry_state", "plan_note",
               "start_date", "start_price", "distance_to_start_pct", "prior_rally_pct", "ma20", "ma60",
               "range_pct", "atr_pct", "volume_ratio", "breakout_high", "close_position",
               "box_high", "converge_ratio", "red_green_vol_ratio",
               "pullback_ratio", "prior2_gain_pct", "engulf_vol_ratio",
               "limit_date", "gap_size_pct", "days_since_limit", "pullback_vol_ratio",
               "wave_gain_pct", "pullback_pct", "second_vol_ratio", "prior_high",
               "ma_gap_pct", "shadow_ratio", "shadow_vol_ratio", "cover_vol_ratio", "prior_gain_60d_pct",
               "bb_lower", "bb_mid", "pierce_pct", "today_high", "today_low",
               "ene_upper", "ene_lower", "trend_gap_pct", "touch_depth_pct"]
    result = pd.DataFrame(signals)
    if result.empty:
        result = pd.DataFrame(columns=columns)
    else:
        result = result.reindex(columns=columns).sort_values(["signal", "sort_score"], ascending=[True, False])
    result.to_csv(report_dir / "signals.csv", index=False, encoding="utf-8-sig")
    (report_dir / "signals.json").write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {
        "scan_time": _now_cn().isoformat(timespec="seconds"),
        "scan_day": day,
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
    scan_parser.add_argument("--day", default=None,
                             help="扫描基准日 YYYYMMDD（历史回填用，默认自动取最近已完成交易日）")
    scan_parser.add_argument("--rebuild-cache", action="store_true",
                             help="强制全窗口重建价格缓存（补齐历史空洞、统一复权基准）")
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
    if args.day is not None and (len(args.day) != 8 or not args.day.isdigit()):
        parser.error("--day 必须是 YYYYMMDD 格式")
    raise SystemExit(scan(args.config, args.day, args.rebuild_cache))


if __name__ == "__main__":
    main()
