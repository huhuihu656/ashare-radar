"""把最新搜到的「清晰上升趋势」候选合并进 docs/data/latest.json 并重建 K 线。

只做 append-only：已存在的 (symbol, signal) 不会重复写入，也不会改写其他信号的行。
"""
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from ashare_monitor.config import load  # noqa: E402
from ashare_monitor.signals import clear_uptrend, entry_exit_plan, position_strategy  # noqa: E402
from export_dashboard import build_klines  # noqa: E402

cfg = load(ROOT / "config.yaml")
SIGNAL_NAME = "清晰上升趋势"
COUNT_KEY = "clear_uptrend"
LIMIT_PCT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}


def board_of(sym: str) -> str:
    if sym.startswith(("688", "689")):
        return "科创板"
    if sym.startswith(("300", "301", "302")):
        return "创业板"
    if sym.startswith(("8", "43", "92")):
        return "北交所"
    return "主板"


def load_names() -> dict:
    cached = ROOT / "data" / "meta_names.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except Exception:
            pass
    names = {}
    try:
        from ashare_monitor.data import _ts_pro
        pro = _ts_pro()
        if pro is not None:
            basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
            names = dict(zip(basic["ts_code"].astype(str).str[:6], basic["name"]))
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return names


def main() -> int:
    latest_path = ROOT / "docs" / "data" / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    as_of = str(latest.get("as_of") or "")
    envs = [s.get("market_env") for s in latest.get("signals", []) if s.get("market_env")]
    env = max(set(envs), key=envs.count) if envs else "中性"
    scan_time = datetime.now().astimezone().isoformat(timespec="seconds")
    names = load_names()

    existing_keys = {(str(s.get("symbol")), s.get("signal")) for s in latest.get("signals", [])}
    new_rows = []
    for f in sorted((ROOT / "data" / "cache").glob("*.csv")):
        sym = f.stem
        try:
            frame = pd.read_csv(f, parse_dates=["date"]).sort_values("date").set_index("date")
            frame = frame[~frame.index.duplicated(keep="last")]
        except Exception:
            continue
        if len(frame) < 60 or frame.index[-1].strftime("%Y%m%d") < "20260901":
            continue
        board = board_of(sym)
        row = clear_uptrend(frame.tail(cfg.scan.lookback_days), cfg.clear_uptrend, cfg.risk,
                            LIMIT_PCT_BY_BOARD.get(board, 0.10))
        if not row:
            continue
        key = (sym, row["signal"])
        if key in existing_keys:
            continue
        row.update({"symbol": sym, "name": names.get(sym, sym), "board": board,
                    "market_env": env, "scan_time": scan_time})
        row.update(position_strategy(row, env))
        plan = entry_exit_plan(row)
        if not plan:
            continue
        row.update(plan)
        row["sort_score"] = row.get("score")
        new_rows.append(row)
        existing_keys.add(key)

    latest["signals"] = list(latest.get("signals", [])) + new_rows
    counts = {COUNT_KEY: sum(1 for s in latest["signals"] if s.get("signal") == SIGNAL_NAME)}
    latest["signal_counts"] = counts
    latest["generated_at"] = scan_time
    latest["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 重建 klines.json（用含新信号的 latest 信号集），否则新标的一进详情 K 线为空
    try:
        klines = build_klines(latest["signals"], ROOT / "data" / "cache")
        (ROOT / "docs" / "data" / "klines.json").write_text(
            json.dumps(klines, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"klines.json rebuilt: {len(klines.get('stocks', {}))} stocks")
    except Exception as exc:
        print(f"klines rebuild failed: {exc}")

    print(f"as_of={as_of} env={env} new_signals={len(new_rows)} total_signals={len(latest['signals'])}")
    print("counts=", json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
