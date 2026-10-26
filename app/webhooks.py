"""Platform-native chat webhook receivers.

Translate Telegram / WeChat / Discord / generic inbound payloads into the
shared inbound chain so merchants can point the platform webhook directly at
this app without writing glue code. Token matching and delivery remain inside
the same security boundary as /chat/inbound.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Workspace


def _telegram(payload: dict) -> dict:
    msg = payload.get("message") or payload.get("edited_message") or payload.get("channel_post") or {}
    if not isinstance(msg, dict):
        return {"channel": "telegram", "external_id": "", "message": "", "customer_name": ""}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    name = sender.get("first_name") or sender.get("username") or ""
    if sender.get("last_name"):
        name = f"{name} {sender['last_name']}".strip()
    return {
        "channel": "telegram",
        "external_id": str(chat.get("id") or ""),
        "message": msg.get("text") or msg.get("caption") or "",
        "customer_name": name,
    }


def _wechat(payload: dict) -> dict:
    # JSON-form form preferred; also accept a raw XML string.
    raw = payload.get("xml") if isinstance(payload.get("xml"), str) else None
    if raw:
        def grab(tag):
            m = re.search(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", raw)
            if not m:
                m = re.search(rf"<{tag}>(.*?)</{tag}>", raw)
            return (m.group(1) if m else "").strip()
        return {
            "channel": "wechat",
            "external_id": grab("FromUserName"),
            "message": grab("Content") or grab("Recognition"),
            "customer_name": "",
        }
    return {
        "channel": "wechat",
        "external_id": str(payload.get("FromUserName") or payload.get("from_user") or ""),
        "message": payload.get("Content") or payload.get("text") or "",
        "customer_name": str(payload.get("nickname") or ""),
    }


def _discord(payload: dict) -> dict:
    author = payload.get("author") or {}
    return {
        "channel": "discord",
        "external_id": str(payload.get("channel_id") or payload.get("chat_id") or ""),
        "message": payload.get("content") or payload.get("text") or "",
        "customer_name": str(author.get("username") or author.get("global_name") or ""),
    }


def _generic(payload: dict) -> dict:
    return {
        "channel": str(payload.get("channel") or "webhook"),
        "external_id": str(payload.get("external_id") or ""),
        "message": str(payload.get("message") or payload.get("text") or ""),
        "customer_name": str(payload.get("customer_name") or ""),
    }


PARSERS = {
    "telegram": _telegram,
    "wechat": _wechat,
    "discord": _discord,
    "webhook": _generic,
    "generic": _generic,
    "other": _generic,
}


def _chat_secret(ws) -> str:
    from . import crypto

    chat_cfg = (ws.settings or {}).get("chat") or {}
    if chat_cfg.get("webhook_secret_enc"):
        try:
            return crypto.decrypt(chat_cfg["webhook_secret_enc"])
        except Exception:  # noqa: BLE001 - a corrupt secret must not crash inbound
            return ""
    return str(chat_cfg.get("webhook_secret") or "")


def verify_signature(provider: str, secret: str, raw_body: bytes, headers: dict) -> bool:
    """Return True when the inbound webhook is authentic.

    telegram: the platform sends the secret verbatim in the
    X-Telegram-Bot-Api-Secret-Token header. Everything else verifies an
    HMAC-SHA256 over the raw body in X-Krama-Signature (``sha256=<hex>``).
    An empty secret means verification is not configured, which callers
    should treat as "skip" rather than "reject".
    """
    if not secret:
        return True
    p = (provider or "").lower()
    if p == "telegram":
        token = str(headers.get("x-telegram-bot-api-secret-token") or "")
        return bool(token) and hmac.compare_digest(token, secret)
    sig = str(headers.get("x-krama-signature") or "")
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig[len("sha256="):], expected)


def normalize(provider: str, payload: dict) -> dict:
    parser = PARSERS.get((provider or "").lower(), _generic)
    return parser(payload or {})


def _find_workspace_by_chat_token(db: Session, token: str) -> Workspace | None:
    from . import token_index

    return token_index.resolve(db, token)


async def handle(db: Session, provider: str, payload: dict, token: str = "", raw_body: bytes = b"", headers: dict | None = None) -> dict:
    from .inbound import process_inbound

    normalized = normalize(provider, payload)
    token = token or str((payload or {}).get("token") or "")
    ws = _find_workspace_by_chat_token(db, token)
    if ws is None:
        return {"ok": False, "error": "invalid or disabled chat token", "channel": normalized["channel"]}
    secret = _chat_secret(ws)
    if secret and not verify_signature(provider, secret, raw_body, headers or {}):
        return {"ok": False, "error": "invalid webhook signature", "channel": normalized["channel"]}
    result = await process_inbound(
        db,
        ws,
        channel=normalized["channel"],
        external_id=normalized["external_id"],
        message=normalized["message"],
        customer_name=normalized["customer_name"],
    )
    result["ok"] = result.get("intent") != "error"
    return result
