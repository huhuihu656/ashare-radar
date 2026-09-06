"""微信实时通知：Server酱 / 企业微信机器人 / PushPlus（三选一，自动识别）。

读取 config.yaml 的 notify 段：
  notify:
    enabled: true            # false 则完全静默
    wechat_sendkey: ""       # Server酱 SendKey (sctapi.ftqq.com)
    wecom_webhook: ""        # 企业微信机器人 webhook
    pushplus_token: ""       # PushPlus token

任一渠道已配置即可推送；未配置任何渠道时静默跳过（不影响主流程）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_LOGGED = False


def _notify_cfg() -> dict:
    try:
        raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        return (raw.get("notify") or {}) or {}
    except Exception:
        return {}


def send(title: str, content: str = "") -> bool:
    """发送一条微信通知；未配置渠道或发送失败返回 False。"""
    global _LOGGED
    cfg = _notify_cfg()
    if cfg.get("enabled", True) is False:
        return False
    key = str(cfg.get("wechat_sendkey") or "")
    webhook = str(cfg.get("wecom_webhook") or "")
    token = str(cfg.get("pushplus_token") or "")
    if not (key or webhook or token):
        if not _LOGGED:
            print("[notify] 未配置微信通知渠道（notify.*），已静默跳过。", flush=True)
            _LOGGED = True
        return False
    text = f"{title}\n{content}".strip()
    try:
        import requests
        if key:
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title, "desp": content}, timeout=15)
            return bool(r.ok)
        if webhook:
            r = requests.post(webhook, json={"msgtype": "text", "text": {"content": text}}, timeout=15)
            return bool(r.ok)
        if token:
            r = requests.post("http://www.pushplus.plus/send",
                              json={"token": token, "title": title, "content": content}, timeout=15)
            return bool(r.ok)
    except Exception as exc:
        print(f"[notify] 发送失败：{exc}", flush=True)
        return False
    return False
