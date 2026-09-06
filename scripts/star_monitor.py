"""监控 GitHub repo 的 star 数，每增加 100 实时微信通知（Server酱）。

用法：
  python scripts/star_monitor.py           # 检查一次；跨过 100 里程碑则通知并更新基准
  --api 之前可用环境变量 GH_TOKEN_GITHUB 提供 token（可选，无 token 也能读公开仓库）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import notify  # noqa: E402

REPO = "huhuihu656/ashare-radar"
MILESTONE = 100
STATE = ROOT / "data" / "star_baseline.json"


def get_stars() -> int:
    url = f"https://api.github.com/repos/{REPO}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "deepseek-harness"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return int(r.json()["stargazers_count"])


def main() -> int:
    stars = get_stars()
    state = {"stars": stars, "last_milestone": stars // MILESTONE}
    if STATE.exists():
        try:
            old = json.loads(STATE.read_text(encoding="utf-8"))
            state["last_milestone"] = old.get("last_milestone", stars // MILESTONE)
        except Exception:
            pass
    milestone = stars // MILESTONE
    if milestone > state["last_milestone"]:
        # 每跨过一个 100 里程碑都通知（一次补发多条）
        for m in range(state["last_milestone"] + 1, milestone + 1):
            notify.send(f"⭐ GitHub star 破 {m * 100}！",
                        f"repo {REPO} 当前 star = {stars}\n较上次里程碑增长，感谢关注本站。")
        state["last_milestone"] = milestone
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[star] stars={stars} milestone={milestone} last_notified={state['last_milestone']}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[star] 检查失败：{exc}", flush=True)
        raise SystemExit(2)
