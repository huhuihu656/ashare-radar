"""把最新搜到的达标股（超跌反转 / 恰好突破20日线）合并进 docs/data/latest.json 并发布。"""
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from ashare_monitor.config import load  # noqa: E402
from ashare_monitor.signals import (  # noqa: E402
    break_ma20, entry_exit_plan, oversold_reversal, position_strategy,
)

cfg = load(ROOT / "config.yaml")
NAME2KEY = {
    "回踩前期起涨位": "support_retest", "横盘后放量突破": "sideways_breakout",
    "箱体突破红肥绿瘦": "box_breakout", "阳包阴反包启动": "bullish_engulfing",
    "涨停跳空缺口共振": "limitup_gap", "龙回头二次启动": "dragon_pullback",
    "均线多头发散": "ma_divergence", "低位仙人指路": "low_shadow",
    "超跌反转": "oversold_reversal", "恰好突破20日线": "break_ma20",
}
DETECTORS = {"超跌反转": oversold_reversal, "恰好突破20日线": break_ma20}


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
        for sig_name, det in DETECTORS.items():
            row = det(frame, getattr(cfg, "oversold_reversal" if sig_name == "超跌反转" else "break_ma20"), cfg.risk)
            if not row:
                continue
            key = (sym, row["signal"])
            if key in existing_keys:
                continue
            row.update({"symbol": sym, "name": names.get(sym, sym), "board": board_of(sym),
                        "market_env": env, "scan_time": scan_time,
                        "news_count": 0, "news_total": 0, "top_headline": ""})
            row.update(position_strategy(row, env))
            plan = entry_exit_plan(row)
            if not plan:
                continue
            row.update(plan)
            row["sort_score"] = row.get("score")
            new_rows.append(row)
            existing_keys.add(key)

    latest["signals"] = list(latest.get("signals", [])) + new_rows
    counts = {v: 0 for v in NAME2KEY.values()}
    for s in latest["signals"]:
        k = NAME2KEY.get(s.get("signal"))
        if k:
            counts[k] += 1
    latest["signal_counts"] = counts
    latest["generated_at"] = scan_time
    latest["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"as_of={as_of} env={env} new_signals={len(new_rows)} total_signals={len(latest['signals'])}")
    print("counts=", json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
