"""每日数据更新后通过 Server酱 推送微信通知。

读取 docs/data/latest.json（已发布的快照）生成摘要，经 scripts/notify.py
的 send() 发送。未配置渠道或发送失败时静默返回非零（不影响发布主流程）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import send  # noqa: E402

SITE = "https://huhuihu656.github.io/ashare-radar/"


def main() -> int:
    payload_path = ROOT / "docs" / "data" / "latest.json"
    if not payload_path.exists():
        print("[notify] latest.json 不存在，跳过。", flush=True)
        return 1
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    signals = payload.get("signals") or []
    counts = payload.get("signal_counts") or {}
    as_of = payload.get("as_of", "?")

    # 主线板块（若有）
    mainline_path = ROOT / "docs" / "data" / "mainline.json"
    mainline = ""
    if mainline_path.exists():
        try:
            ml = json.loads(mainline_path.read_text(encoding="utf-8"))
            sectors = [s.get("industry", "") for s in ml.get("top_sectors", [])[:3]]
            if sectors:
                mainline = f"\n\n**本月主线**：{' / '.join(sectors)}"
        except Exception:
            pass

    # Top 信号（按 sort_score）
    top = sorted(signals, key=lambda s: -(s.get("sort_score") or 0))[:5]
    lines = []
    for s in top:
        name = s.get("name", "")
        sig = str(s.get("signal", ""))[:8]
        score = s.get("sort_score", s.get("score", 0))
        news = int(s.get("news_count") or 0)
        lines.append(f"- {name}（{s.get('symbol','')}）{sig} {score} 分" + (f"，利好×{news}" if news else ""))
    top_text = "\n".join(lines)

    title = f"势能雷达 {as_of} 收盘扫描：{len(signals)} 条信号"
    desp = (
        f"覆盖 {payload.get('universe_count', '?')} 只 · 成功率 {round(float(payload.get('coverage') or 0) * 100, 1)}%\n\n"
        f"**今日候选 Top**：\n{top_text}{mainline}\n\n"
        f"[查看完整信号]({SITE})"
    )
    ok = send(title, desp)
    print(f"[notify] 推送{'成功' if ok else '失败/未配置'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
