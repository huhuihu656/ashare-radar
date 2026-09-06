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
    cfg = {}
    try:
        raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        cfg = dict((raw.get("notify") or {}) or {})
    except Exception:
        pass
    # 密钥优先从本地不提交的 data/notify_secret.json 读取（避免 key 进 git）
    try:
        import json
        secret = json.loads((ROOT / "data" / "notify_secret.json").read_text(encoding="utf-8"))
        for k in ("wechat_sendkey", "wecom_webhook", "pushplus_token", "callback_url", "token"):
            if secret.get(k):
                cfg[k] = secret[k]
        if secret.get("enabled") is not None:
            cfg["enabled"] = secret["enabled"]
    except Exception:
        pass
    return cfg


def send(title: str, content: str = "") -> bool:
    """发送一条通知；未配置渠道或发送失败返回 False。"""
    global _LOGGED
    cfg = _notify_cfg()
    if cfg.get("enabled", True) is False:
        return False
    # 自定义回调（claude-phone-bridge / loca.lt 隧道）
    cb = str(cfg.get("callback_url") or "")
    key = str(cfg.get("wechat_sendkey") or "")
    webhook = str(cfg.get("wecom_webhook") or "")
    token = str(cfg.get("pushplus_token") or "")
    cb_token = str(cfg.get("token") or "")
    if not (cb or key or webhook or token):
        if not _LOGGED:
            print("[notify] 未配置微信通知渠道（notify.*），已静默跳过。", flush=True)
            _LOGGED = True
        return False
    text = f"{title}\n{content}".strip()
    try:
        import requests
        if cb:
            cb_key = str(cfg.get("key") or "")
            sep = "&" if "?" in cb else "?"
            url = f"{cb}{sep}token={cb_token}" if cb_token else cb
            headers = {"Content-Type": "application/json"}
            if cb_key:
                headers["Authorization"] = f"Bearer {cb_key}"
            r = requests.post(url, json={"title": title, "content": content, "text": text},
                              headers=headers, timeout=15)
            return bool(r.ok)
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
