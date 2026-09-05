"""Positive-news harvesting for signal ranking (东财公开个股资讯).

Tushare news/major_news interfaces need a higher points tier; until then the
per-stock public news feed (Eastmoney, textual only - not a quote source) is
used.  The count of POSITIVE news headlines over the last 3 sessions becomes a
ranking tie-breaker: signals with more positive news rank earlier.

Keyword lists are heuristic and publicly auditable; a hit is a vote, not a
guarantee.  Cache: data/news_cache/<yyyymmdd>.json (per scan day).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

POSITIVE = [
    "回购", "增持", "中标", "业绩预增", "扭亏", "重组", "签约", "战略合作",
    "获批", "涨价", "订单", "分红", "机构买入", "达成", "入选", "政策",
    "补贴", "突破", "合作", "扩产", "创新高", "涉足", "拟收购", "并购",
    "获得", "授予", "公告利好", "净利润增长", "营收增长", "合同", "中标价",
]
NEGATIVE = [
    "减持", "亏损", "跌停", "立案", "处罚", "退市", "违规", "风险提示",
    "商誉减值", "减持计划", "关注函", "问询函", "质押", "冻结",
]
SEEN_WINDOW_DAYS = 3
POSITIVE_BONUS = 3.0
BONUS_CAP = 15.0
_HEADLINE_RE = re.compile(r"[（(](?:退市)?[A-Za-z0-9]{6}(?:\.(?:SH|SZ|BJ))?[)）]")


def _clean_headline(text: str) -> str:
    """Strip the '(code)' prefix (noise) for keyword matching."""
    return _HEADLINE_RE.sub("", str(text)).strip()


def classify_text(text: str) -> int:
    """+1 when text carries a positive keyword and no negative keyword."""
    text = _clean_headline(text)
    if any(word in text for word in NEGATIVE):
        return 0
    return 1 if any(word in text for word in POSITIVE) else 0


def cache_path(cache_dir: Path, day: str) -> Path:
    return cache_dir / f"{day}.json"


def build_news_cache(symbols: list[str], cache_dir: Path,
                     days: int = SEEN_WINDOW_DAYS) -> dict[str, list[dict]]:
    """Fetch recent headlines once per scan and cache under data/news_cache/.

    Returns {symbol: [{"date": ..., "title": ..., "source": ..., "pos": bool}]}.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    since = (today - timedelta(days=days * 3)).strftime("%Y%m%d")
    out: dict[str, list[dict]] = {}
    for symbol in symbols:
        try:
            import akshare as ak

            raw = ak.stock_news_em(symbol=symbol)
        except Exception:
            continue
        if raw is None or raw.empty:
            continue
        items = []
        for _, row in raw.head(20).iterrows():
            title = str(row.get("新闻标题", "")).strip()
            day = str(row.get("发布时间", "")).replace("-", "")[:8]
            if day and day < since:
                continue
            pos = classify_text(title)
            items.append({"date": day, "title": title, "pos": bool(pos)})
        if items:
            out[symbol] = items
    cache_path(cache_dir, today.strftime("%Y%m%d")).write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def news_counts(symbols: list[str], cache_dir: Path) -> dict[str, dict]:
    """Positive-news count per symbol (cached for the day; rebuild on miss)."""
    today = date.today().strftime("%Y%m%d")
    path = cache_path(cache_dir, today)
    loaded: dict[str, list[dict]] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
    needs = [s for s in symbols if s not in loaded]
    if needs:
        fresh = build_news_cache(needs, cache_dir)
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
        loaded.update(fresh)
    return {
        symbol: {
            "news_count": sum(1 for item in items if item["pos"]),
            "news_total": len(items),
            "top_headline": next((i["title"] for i in items if i["pos"]), ""),
        }
        for symbol, items in loaded.items()
    }


def sort_score(signal_score: float, news_count: int) -> float:
    """Ranking key: pattern score + capped positive-news bonus."""
    return round(signal_score + min(news_count * POSITIVE_BONUS, BONUS_CAP), 1)
