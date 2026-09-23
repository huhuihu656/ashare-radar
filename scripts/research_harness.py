"""快速研究管线：并行建二进制数据集缓存 + 向量化条件概率扫描 + 向量化盈亏模拟。

动机：每次实验都重读 5699 个 CSV 并重算特征，是研究迭代里最大的固定成本。
这里把它拆成「一次构建 + 多次秒级复用」。

  python scripts/research_harness.py build          # 并行建缓存（约 5s）
  python scripts/research_harness.py bench          # 建缓存 vs 直读的对比
  python scripts/research_harness.py scan           # 向量化条件概率扫描
  python scripts/research_harness.py pnl            # 向量化括号式盈亏模拟

关键点：
- 16 进程并行读 CSV + 算特征（每只股票彼此独立，天然可并行）。
- 特征落盘为 float32 的 .npz，后续加载无需再解析 CSV、再算 rolling。
- 条件概率用 np.digitize + np.bincount，替代 pd.cut + groupby + get_group。
- 盈亏模拟用 sliding_window_view + argmax，替代逐样本 Python 双重循环。
"""
from __future__ import annotations

import glob
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "backtest_cache")
STORE = os.path.join(ROOT, "data", "research")
DATASET = os.path.join(STORE, "dataset.npz")

HORIZON = 20
FEATURES = ["px_vs_ma20", "px_vs_ma60", "pos120", "ma20_gt_ma60", "ma20_slope5", "ma60_slope5",
            "ret20", "ret60", "rsi14", "amp20", "vol20", "volr5_20", "volr1_20",
            "dist_hi60", "dist_lo60"]
MIN_BARS = 200


def _features_one(path: str):
    """一只股票的全部特征与标签原料，返回 float32 数组 + 日期整数 + 代码。"""
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if len(df) < MIN_BARS:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].to_numpy(np.float64)
    op = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    lo = df["low"].to_numpy(np.float64)
    v = df["volume"].to_numpy(np.float64)
    cs, hs, ls, vs = (pd.Series(x) for x in (c, h, lo, v))

    ma20 = cs.rolling(20).mean()
    ma60 = cs.rolling(60).mean()
    d1 = cs.diff()
    up = d1.clip(lower=0).rolling(14).mean()
    dn = (-d1.clip(upper=0)).rolling(14).mean()
    vol20 = cs.pct_change().rolling(20).std()

    cols = {
        "px_vs_ma20": c / ma20.to_numpy() - 1,
        "px_vs_ma60": c / ma60.to_numpy() - 1,
        "pos120": ((c - ls.rolling(120).min().to_numpy()) /
                   (hs.rolling(120).max().to_numpy() - ls.rolling(120).min().to_numpy() + 1e-9)),
        "ma20_gt_ma60": (ma20.to_numpy() > ma60.to_numpy()).astype(np.float64),
        "ma20_slope5": ma20.to_numpy() / ma20.shift(5).to_numpy() - 1,
        "ma60_slope5": ma60.to_numpy() / ma60.shift(5).to_numpy() - 1,
        "ret20": c / cs.shift(20).to_numpy() - 1,
        "ret60": c / cs.shift(60).to_numpy() - 1,
        "rsi14": 100 - 100 / (1 + up.to_numpy() / (dn.to_numpy() + 1e-9)),
        "amp20": hs.rolling(20).max().to_numpy() / ls.rolling(20).min().to_numpy() - 1,
        "vol20": vol20.to_numpy(),
        "volr5_20": vs.rolling(5).mean().to_numpy() / vs.rolling(20).mean().to_numpy(),
        "volr1_20": v / vs.rolling(20).mean().to_numpy(),
        "dist_hi60": c / hs.rolling(60).max().to_numpy() - 1,
        "dist_lo60": c / ls.rolling(60).min().to_numpy() - 1,
        # 标签原料：未来 20 日的收盘收益、以及区间内的最高/最低（用于盈亏模拟）
        "fwd": cs.shift(-HORIZON).to_numpy() / c - 1,
        "scale": vol20.to_numpy() * np.sqrt(HORIZON),
        "close": c, "high": h, "low": lo, "open": op,
        "above_ma20": (c > ma20.to_numpy()).astype(np.float64),
    }
    # 日期 -> int32 (YYYYMMDD)；不 parse_dates，省下大量解析时间
    ymd = df["date"].astype("string").str.replace("-", "", regex=False).astype(np.int32).to_numpy()

    n = len(df)
    sym = os.path.basename(path)[:-4]
    return {k: v.astype(np.float32) for k, v in cols.items()} | {"ymd": ymd, "n": n, "symbol": sym}


def build(verbose: bool = True):
    paths = sorted(glob.glob(os.path.join(CACHE, "*.csv")))
    t0 = time.perf_counter()
    workers = os.cpu_count() or 8
    with Pool(workers) as pool:
        results = [r for r in pool.map(_features_one, paths, chunksize=64) if r]
    t_par = time.perf_counter() - t0

    os.makedirs(STORE, exist_ok=True)
    cols = FEATURES + ["fwd", "scale", "close", "high", "low", "open", "above_ma20"]
    arrays = {}
    for k in cols:
        arrays[k] = np.concatenate([r[k] for r in results]).astype(np.float32)
    arrays["ymd"] = np.concatenate([r["ymd"] for r in results])
    arrays["symbol"] = np.concatenate([np.full(r["n"], int(r["symbol"]), dtype=np.int64) for r in results])
    np.savez(DATASET, **arrays)
    if verbose:
        print("并行建缓存（%d 进程）：%.2fs" % (workers, t_par))
        print("  股票 %d 只，合计 %d 行，落盘 %s (%.0f MB)" % (
            len(results), len(arrays["ymd"]), DATASET,
            os.path.getsize(DATASET) / 1e6))
    return t_par


def load():
    d = np.load(DATASET, allow_pickle=False)
    return {k: d[k] for k in d.files}


def _market_median(ymd: np.ndarray, fwd: np.ndarray) -> np.ndarray:
    """按日期的横截面中位数，向量化回填到每一行。"""
    order = np.argsort(ymd, kind="stable")
    ys = ymd[order]
    fs = fwd[order]
    bounds = np.flatnonzero(np.r_[True, ys[1:] != ys[:-1], True])
    med = np.empty(len(bounds) - 1)
    for i in range(len(bounds) - 1):
        seg = fs[bounds[i]:bounds[i + 1]]
        # 必须跳过 NaN：fwd 尾部天然有 NaN，np.median 遇到 NaN 会整组返回 NaN，
        # 该日期所有行都会被判为无效样本。pandas 的 groupby.median 默认跳过。
        med[i] = np.nanmedian(seg) if np.isfinite(seg).any() else np.nan
    out = np.empty_like(fwd)
    for i in range(len(bounds) - 1):
        out[order[bounds[i]:bounds[i + 1]]] = med[i]
    return out


def cond_prob(values: np.ndarray, label: np.ndarray, edges: np.ndarray):
    """一次 bincount 得到每箱的条件概率与样本量（替代 groupby）。"""
    # right=True 对齐 pd.cut(include_lowest=True) 的右闭区间 (a, b]，
    # 否则取值恰好等于分箱边界的行会被分进相邻箱。
    idx = np.digitize(values, edges[1:-1], right=True)
    cnt = np.bincount(idx, minlength=len(edges) + 1).astype(np.float64)
    hit = np.bincount(idx, weights=label, minlength=len(edges) + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        p = np.where(cnt > 0, hit / np.maximum(cnt, 1), np.nan) * 100
    return p, cnt


def scan(labels=("A", "B", "C"), train_end=20251231, verbose=True):
    d = load()
    ymd, fwd, scale = d["ymd"], d["fwd"], d["scale"]
    mkt = _market_median(ymd, fwd)
    excess = fwd - mkt
    mask = np.isfinite(fwd) & np.isfinite(excess) & np.isfinite(scale)
    L = {
        "A": ((excess >= 0.10) & mask).astype(np.float32),
        "B": ((fwd >= scale) & mask).astype(np.float32),
        "C": ((excess >= 0.10) & (fwd >= scale) & mask).astype(np.float32),
    }
    # 与慢版本同一口径：只在「16 个特征全部非空」的样本上统计。
    # 否则会把每只股票开头 rolling 未成形的行也计入，样本偏到基准率最低的早期年份。
    complete = mask.copy()
    for feat in FEATURES:
        complete &= np.isfinite(d[feat])
    tr = complete & (ymd <= train_end)
    ho = complete & (ymd > train_end)

    out = {}
    for name in labels:
        lab = L[name]
        base_ho = lab[ho].mean() * 100
        rows = []
        for feat in FEATURES:
            v = d[feat]
            edges = np.unique(np.quantile(v[tr], [0, .2, .4, .6, .8, 1.0]))
            if len(edges) < 3:
                continue
            p_tr, n_tr = cond_prob(v[tr], lab[tr], edges)
            p_ho, n_ho = cond_prob(v[ho], lab[ho], edges)
            for b in range(len(p_tr)):
                if n_tr[b] < 5000 or n_ho[b] < 1000 or not np.isfinite(p_ho[b]):
                    continue
                rows.append((feat, edges, b, p_tr[b], p_ho[b], int(n_ho[b])))
        rows.sort(key=lambda r: -r[4])
        out[name] = (base_ho, rows)
        if verbose:
            print("标签 %s  留出期基准率 %.2f%%" % (name, base_ho))
            for feat, e, b, pt, ph, nh in rows[:5]:
                print("   %-14s 箱%d  训练 %5.2f%%  留出 %5.2f%%  提升 %.2fx  n=%d" % (
                    feat, b, pt, ph, ph / base_ho, nh))
            print()
    return out


def pnl(target=0.20, stop=-0.08, cost=0.001, verbose=True):
    """向量化括号式盈亏模拟：触及 +target 或 -stop 出场，否则 20 日后收盘平仓。"""
    d = load()
    sym, close, high, low = d["symbol"], d["close"], d["high"], d["low"]
    order = np.argsort(sym, kind="stable")
    rets, gruops = [], []
    amp = d["amp20"]

    bounds = np.flatnonzero(np.r_[True, sym[order][1:] != sym[order][:-1], True])
    for i in range(len(bounds) - 1):
        idx = order[bounds[i]:bounds[i + 1]]
        c, h, l = close[idx], high[idx], low[idx]
        n = len(c)
        if n < HORIZON + 62:
            continue
        starts = np.arange(60, n - HORIZON, 20)   # 非重叠：每 20 个交易日取一个，避免同一段行情重复计数
        if len(starts) == 0:
            continue
        hw = np.lib.stride_tricks.sliding_window_view(h[1:], HORIZON)[starts]
        lw = np.lib.stride_tricks.sliding_window_view(l[1:], HORIZON)[starts]
        e = c[starts]
        tgt = e * (1 + target)
        stp = e * (1 + stop)
        hit_t = hw >= tgt[:, None]
        hit_s = lw <= stp[:, None]
        any_t, any_s = hit_t.any(1), hit_s.any(1)
        it = np.where(any_t, hit_t.argmax(1), HORIZON)
        is_ = np.where(any_s, hit_s.argmax(1), HORIZON)
        r = c[starts + HORIZON] / e - 1            # 默认：20 日后平仓
        r = np.where(any_t & (it < is_), target, r)     # 先到止盈
        r = np.where(any_s & (is_ <= it), stop, r)      # 先到止损（同根 K 线止损优先）
        rets.append(r)
        gruops.append(np.stack([amp[idx[starts]], d["vol20"][idx[starts]], e]))
    r = np.concatenate(rets) - cost
    g = np.concatenate(gruops, axis=1)
    if verbose:
        print("非重叠样本：%d 个" % len(r))
        print("  整体：平均净收益 %.2f%%  中位 %.2f%%  胜率 %.1f%%" % (
            r.mean() * 100, np.median(r) * 100, (r > 0).mean() * 100))
        for j, name in enumerate(["amp20", "vol20"]):
            v = g[j]
            q30, q70 = np.quantile(v, [0.3, 0.7])
            for lab, m in (("最低30%", v <= q30), ("中间40%", (v > q30) & (v <= q70)), ("最高30%", v > q70)):
                print("  按 %-6s %-8s 平均 %6.2f%%  中位 %6.2f%%  胜率 %.1f%%" % (
                    name, lab, r[m].mean() * 100, np.median(r[m]) * 100, (r[m] > 0).mean() * 100))
    return r


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "build":
        build()
    elif cmd == "bench":
        t = build(verbose=False)
        t0 = time.perf_counter()
        load()
        t_load = time.perf_counter() - t0
        print("并行建缓存 %.2fs（一次性）| 之后加载缓存 %.2fs" % (t, t_load))
    elif cmd == "scan":
        t0 = time.perf_counter()
        scan()
        print("扫描耗时 %.2fs" % (time.perf_counter() - t0))
    elif cmd == "pnl":
        t0 = time.perf_counter()
        pnl()
        print("盈亏模拟耗时 %.2fs" % (time.perf_counter() - t0))
