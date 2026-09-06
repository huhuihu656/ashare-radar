"""逐信号战绩（tracked.json）——「信号战绩」面板的数据引擎。

与 scripts/backtest.py 共用同一套信号引擎（evaluate_symbol），对历史交易日
逐股重放 8 类形态信号，并为每条信号计算 T+1 开盘买入后的 1/5/10/20 个交易日
收益（前复权）与 20 日内止损/止盈/持有结果，输出 docs/data/tracked.json。

口径（与回测一致，前端需如实展示）：
  - 信号用当日已收盘 K 线（无前视）；入场 T+1 开盘
  - T+1 开盘跳空 > 计划买入价 +5% 或低于止损价 → 标记 g=0（回测中跳过不入场）
  - 止损/止盈按当日最低/最高价，同日双触发按止损优先（保守）
  - 20 个交易日内未触发 → 记为持有（x=open，按第 20 日收盘计）；数据不足 → 运行中

增量运行：仅扫描比已有 tracked.json 更新的交易日（同信号同股 15 日间隔剪枝用
已有记录接续，口径与全量一致），然后重算所有信号的最新窗口表现（纯本地缓存）。

用法：
  python scripts/signal_track.py                    # 轻量：回放最近约 60 个交易日（可 --recent-sessions 调整）
  python scripts/signal_track.py --full             # 全量（两年，较重）
  python scripts/signal_track.py --recent-sessions 120   # 近约 6 个月
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import backtest as bt  # noqa: E402  复用同一信号引擎
from ashare_monitor.config import load  # noqa: E402

# 多进程 worker 共享数据（GIL 无关，真正并行跑满 8 核；每进程独立解释器）
_WORKER: dict = {}


def _init_worker(mf, sessions, sidx, env_by_day, cfg):
    _WORKER["mf"], _WORKER["sessions"], _WORKER["sidx"] = mf, sessions, sidx
    _WORKER["env"], _WORKER["cfg"] = env_by_day, cfg


def _eval_wrapper(sym, frame, window, seed):
    try:
        return bt.evaluate_symbol(sym, frame, window, _WORKER["sidx"], _WORKER["sessions"],
                                  _WORKER["mf"], _WORKER["env"], _WORKER["cfg"], seed)
    except Exception as exc:  # 单只股票失败不拖垮整体
        return []

START = "20240902"          # 与 backtest.py 的两年回测窗口一致
HORIZONS = (1, 5, 10, 20)   # 跟踪的交易日窗口
CHASE_CAP = 0.05
SCHEMA_VERSION = 1
MIN_BARS = 155


def _ts_pro():
    try:
        return bt._ts_pro()
    except Exception:
        return None


def load_frames(cache_dir: Path, names: dict[str, str]) -> dict[str, pd.DataFrame]:
    """加载前复权日线缓存；过滤 ST 与北交所（与回测一致）。"""
    frames: dict[str, pd.DataFrame] = {}
    for f in sorted(cache_dir.glob("*.csv")):
        sym = f.stem
        if "ST" in str(names.get(sym, "")).upper():
            continue
        if sym.startswith(("8", "4", "92")):
            continue
        try:
            frame = pd.read_csv(f, parse_dates=["date"]).sort_values("date").set_index("date")
        except Exception:
            continue
        if len(frame) >= MIN_BARS:
            frames[sym] = frame
    return frames


def load_moneyflow(mf_path: Path) -> dict[tuple[str, str], float]:
    mf: dict[tuple[str, str], float] = {}
    if mf_path.exists():
        frame = pd.read_csv(mf_path, dtype={"symbol": str})
        for _, r in frame.iterrows():
            mf[(str(r["trade_date"]), str(r["symbol"]).zfill(6))] = float(r["net_mf_amount"])
    return mf


def outcome_for(symbol: str, day: str, entry_ref: float, stop: float,
                target: float | None, frames: dict[str, pd.DataFrame]):
    """T+1 开盘入场后的窗口表现。返回 (eo, gap_ok, rets, exit, exit_off, exit_price)。"""
    f = frames.get(symbol)
    if f is None:
        return None
    dates = f.index.strftime("%Y%m%d")
    pos = {d: i for i, d in enumerate(dates)}
    i0 = pos.get(day)
    if i0 is None or i0 + 1 >= len(f):
        return None
    entry_i = i0 + 1
    eo = float(f["open"].iloc[entry_i])
    if eo <= 0:
        return None
    gap = 1 if (eo <= entry_ref * (1 + CHASE_CAP) and eo >= stop) else 0
    rets: dict[int, float] = {}
    for k in HORIZONS:
        j = entry_i + k
        if j < len(f):
            rets[k] = round(float(f["close"].iloc[j]) / eo - 1, 4)
    exit_kind, exit_off, exit_price = "running", None, None
    for k in range(1, max(HORIZONS) + 1):
        j = entry_i + k
        if j >= len(f):
            break
        if float(f["low"].iloc[j]) <= stop:
            exit_kind, exit_off, exit_price = "stop", k, round(stop, 2)
            break
        if target is not None and float(f["high"].iloc[j]) >= target:
            exit_kind, exit_off, exit_price = "target", k, round(target, 2)
            break
        if k == max(HORIZONS):
            exit_kind, exit_off, exit_price = "open", k, round(float(f["close"].iloc[j]), 2)
    return eo, gap, rets, exit_kind, exit_off, exit_price


def summarize(rows: list[dict]) -> dict:
    def stats(rs: list[dict]) -> dict:
        out = {"cnt": len(rs)}
        for k in HORIZONS:
            vals = [r.get(f"r{k}") for r in rs if r.get(f"r{k}") is not None]
            out[f"c{k}"] = len(vals)
            if vals:
                out[f"a{k}"] = round(sum(vals) / len(vals) * 100, 2)
                out[f"p{k}"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
            else:
                out[f"a{k}"] = None
                out[f"p{k}"] = None
        closed = [r for r in rs if r["x"] in ("stop", "target", "open")]
        out["closed"] = len(closed)
        if closed:
            out["stp"] = round(sum(1 for r in closed if r["x"] == "stop") / len(closed) * 100, 1)
            out["tgt"] = round(sum(1 for r in closed if r["x"] == "target") / len(closed) * 100, 1)
        else:
            out["stp"] = None
            out["tgt"] = None
        vals20 = [r for r in rs if r.get("r20") is not None]
        if vals20:
            best = max(vals20, key=lambda r: r["r20"])
            worst = min(vals20, key=lambda r: r["r20"])
            out["b"] = [round(best["r20"] * 100, 2), best["sd"], best["s"], best["n"], best["p"]]
            out["w"] = [round(worst["r20"] * 100, 2), worst["sd"], worst["s"], worst["n"], worst["p"]]
        return out

    patterns = {}
    for p in sorted({r["p"] for r in rows}):
        patterns[p] = stats([r for r in rows if r["p"] == p])
    return {"patterns": patterns, "overall": stats(rows)}


def build_seed(rows: list[dict]) -> dict[str, dict[str, str]]:
    """从既有跟踪表接续剪枝状态：仅收录 position_pct > 5（与引擎更新规则一致）。"""
    seed: dict[str, dict[str, str]] = defaultdict(dict)
    for r in rows:
        if r.get("pos", 0) > 5:
            key = (r["s"], r["p"])
            if key[1] not in seed[key[0]] or r["sd"] > seed[key[0]][key[1]]:
                seed[key[0]][key[1]] = str(r["sd"])
    return dict(seed)


def _load_names(pro) -> dict[str, str]:
    """名称（ST 过滤/展示用）本地缓存，重跑不依赖网络。"""
    p = ROOT / "data" / "meta_names.json"
    if p.exists():
        try:
            names = json.loads(p.read_text(encoding="utf-8"))
            if names:
                return names
        except Exception:
            pass
    basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
    names = dict(zip(basic["ts_code"].astype(str).str[:6], basic["name"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    return names


def _load_cal(pro, data_end) -> tuple[list[str], dict[str, str]]:
    """交易日历 + 大盘环境（env_by_day）本地缓存，按 data_end 校验。"""
    p = ROOT / "data" / "meta_cal.json"
    if p.exists():
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if str(c.get("data_end")) == str(data_end):
                return c["sessions"], c["env_by_day"]
        except Exception:
            pass
    cal = pro.trade_cal(exchange="SSE", start_date="20240101", end_date=data_end, is_open="1")
    sessions = sorted(cal["cal_date"].astype(str).tolist())
    idx_raw = pro.index_daily(ts_code="000001.SH", start_date="20231201", end_date=data_end)
    idx_raw = idx_raw.rename(columns={"trade_date": "date"})
    idx_raw["date"] = pd.to_datetime(idx_raw["date"])
    idx = idx_raw.set_index("date").sort_index()[["close"]]
    env_by_day = {d: bt.market_env_at(d, sessions, idx) for d in sessions}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"data_end": str(data_end), "sessions": sessions, "env_by_day": env_by_day},
                            ensure_ascii=False), encoding="utf-8")
    return sessions, env_by_day


def main() -> int:
    ap = argparse.ArgumentParser(description="生成逐信号战绩 tracked.json")
    ap.add_argument("--out", default="docs/data/tracked.json")
    ap.add_argument("--cache-dir", default="data/cache")
    ap.add_argument("--full", action="store_true", help="全量重放（两年，较重）")
    ap.add_argument("--recent-sessions", type=int, default=60,
                    help="初始轻量窗口：只回放最近 N 个交易日（约 3 个月；设为 120 约 6 个月）")
    ap.add_argument("--warmup", type=int, default=20,
                    help="轻量窗口向前多扫 N 个交易日，让同信号 15 日间隔剪枝状态稳定")
    args = ap.parse_args()

    cfg = load(ROOT / "config.yaml")
    cache_dir = ROOT / args.cache_dir
    out = ROOT / args.out
    pro = _ts_pro()
    if pro is None:
        print("[track] Tushare 不可用，跳过（不影响已上线数据）。", flush=True)
        return 3

    # ---- 元数据（名称/交易日历/大盘环境，本地缓存，缓存命中即不依赖网络）----
    mf = load_moneyflow(ROOT / "data" / "backtest_moneyflow.csv")
    try:
        names = _load_names(pro)
        frames = load_frames(cache_dir, names)
        print(f"[track] loaded {len(frames)} symbols", flush=True)
        if not frames:
            print("[track] 价格缓存为空", flush=True)
            return 2
        data_end = max(f.index.max().strftime("%Y%m%d") for f in frames.values())
        sessions, env_by_day = _load_cal(pro, data_end)
    except Exception as exc:
        print(f"[track] 元数据获取失败（{exc}）；若缓存缺失则跳过本轮。", flush=True)
        return 3
    sidx = {d: i for i, d in enumerate(sessions)}

    # ---- 既有跟踪表 ----
    old_rows: list[dict] = []
    old_by_key: dict[tuple[str, str, str], dict] = {}
    max_covered = ""
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            old_rows = old.get("signals", [])
            for r in old_rows:
                old_by_key[(str(r["sd"]), r["s"], r["p"])] = r
            if old_rows:
                max_covered = max(str(r["sd"]) for r in old_rows)
        except Exception as exc:
            print(f"[track] 旧 tracked.json 解析失败（{exc}），按全量处理。", flush=True)
            max_covered = ""

    full = args.full
    keep_from = None
    if full:
        window = [d for d in sessions if START <= d <= data_end]
        print(f"[track] 全量重放 {len(window)} 个交易日…", flush=True)
        seed = None
    elif old_rows:
        window = [d for d in sessions if START <= d and d > max_covered]
        print(f"[track] 增量：{len(window)} 个新交易日（已有截至 {max_covered}）", flush=True)
        seed = build_seed(old_rows)
    else:
        recent = max(1, args.recent_sessions)
        warmup = max(0, args.warmup)
        target_start = max(0, len(sessions) - recent)
        scan_start = max(0, target_start - warmup)
        window = sessions[scan_start:]
        keep_from = sessions[target_start]
        print(f"[track] 轻量窗口：回放最近 {len(sessions) - target_start} 个交易日（扫描自 {sessions[scan_start]}，"
              f"含 {warmup} 日预热以稳定剪枝）…", flush=True)
        seed = None

    # ---- 信号重放（多进程并行，8 核跑满）----
    # 资金流只传窗口内子集，避免 8 个进程各 pickle 一次 65MB 大字典
    window_days = set(window)
    mf_sub = {(d, s): v for (d, s), v in mf.items() if d in window_days and s in frames}
    candidates: list[dict] = []
    items = list(frames.items())
    with ProcessPoolExecutor(max_workers=8, initializer=_init_worker,
                             initargs=(mf_sub, sessions, sidx, env_by_day, cfg)) as pool:
        futures = [pool.submit(_eval_wrapper, sym, frame, window, seed)
                   for sym, frame in items]
        for done, future in enumerate(futures, 1):
            candidates.extend(future.result())
            if done % 800 == 0:
                print(f"[track] scanned {done}/{len(items)}: {len(candidates)} new candidates", flush=True)
    if keep_from:
        candidates = [c for c in candidates if c.get("signal_day") >= keep_from]
    print(f"[track] {len(candidates)} new candidates from {len(window)} session(s)", flush=True)

    rows_by_key = dict(old_by_key)
    for row in candidates:
        key = (str(row["signal_day"]), row["symbol"], row["signal"])
        rows_by_key[key] = row
    print(f"[track] total signals {len(rows_by_key)}", flush=True)

    # ---- 每条信号补齐窗口表现 ----
    rows: list[dict] = []
    skipped = 0
    for key, row in rows_by_key.items():
        day, symbol, pattern = key
        er = float(row.get("entry_price") or 0)
        stop = float(row.get("stop_loss") or 0)
        target_v = row.get("take_profit")
        target = float(target_v) if target_v is not None else None
        if er <= 0 or stop <= 0 or stop >= er:
            skipped += 1
            continue
        oc = outcome_for(symbol, day, er, stop, target, frames)
        if oc is None:
            skipped += 1
            continue
        eo, gap, rets, x, xd, xp = oc
        rec = {
            "sd": int(day), "s": symbol, "n": names.get(symbol, symbol), "p": pattern,
            "env": row.get("market_env") or "未知", "sc": round(float(row.get("score") or 0), 1),
            "pos": int(row.get("position_pct") or 0),
            "er": round(er, 2), "st": round(stop, 2),
            "tp": round(target, 2) if target is not None else None,
            "eo": round(eo, 2), "g": gap,
            "x": x, "xd": xd, "xp": xp,
        }
        for k in HORIZONS:
            if k in rets:
                rec[f"r{k}"] = rets[k]
        rows.append(rec)

    rows.sort(key=lambda r: (-r["sd"], -r["sc"]))
    first_sd = min((r["sd"] for r in rows), default=int(data_end))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_end": int(data_end),
        "signal_range": [int(first_sd), int(data_end)],
        "horizons": list(HORIZONS),
        "method": ("与回测同一引擎；信号为当日已收盘K线判定，T+1开盘买入；跳空>5%或低于止损不入场(g=0)；"
                   "止损/止盈按日内最高最低判定且同日止损优先；收益为前复权口径（20日内未触发记为持有 at20）。"),
        "patterns_summary": summarize(rows)["patterns"],
        "overall": summarize(rows)["overall"],
        "signals": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[track] wrote {out} | {len(rows)} signals | data_end={data_end} | skipped={skipped}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
