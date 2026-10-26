"""Outbound chat messaging adapter.

The agent never talks to a chat platform directly; this module is the single
delivery layer. Providers are data-driven from workspace settings so merchants
can connect their own bot token/URL. When no provider is configured the adapter
records the outbound message and returns a deterministic stub, keeping the
business chain complete and testable offline.
"""
from __future__ import annotations

import httpx

from sqlalchemy.orm import Session

from .models import Message, Workspace


def chat_settings(db: Session, workspace_id: str) -> dict:
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        return {}
    cfg = dict((ws.settings or {}).get("chat") or {})
    if cfg.get("token_enc"):
        from . import crypto

        cfg["token"] = crypto.decrypt(cfg["token_enc"])
    return cfg


def record_message(db: Session, workspace_id: str, conversation_id: str, direction: str, content: str, meta: dict | None = None) -> Message:
    msg = Message(
        workspace_id=workspace_id,
        conversation_id=conversation_id or None,
        direction=direction,
        content=content,
        meta=meta or {},
    )
    db.add(msg)
    db.flush()
    return msg


def _deliver(provider: str, cfg: dict, workspace_id: str, channel: str, external_id: str, text: str) -> tuple[bool, dict]:
    token = cfg.get("token") or ""
    outbound_url = cfg.get("outbound_url") or ""

    if provider == "telegram" and token:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = httpx.post(url, json={"chat_id": external_id, "text": text}, timeout=15)
        resp.raise_for_status()
        return True, {"provider": provider, "status_code": resp.status_code}

    if provider == "discord" and outbound_url:
        from .functions import validate_url

        validate_url(outbound_url)
        resp = httpx.post(outbound_url, json={"content": text}, timeout=15)
        resp.raise_for_status()
        return True, {"provider": provider, "status_code": resp.status_code}

    if provider in ("wechat", "wecom", "webhook", "other") and outbound_url:
        from .functions import validate_url

        validate_url(outbound_url)
        if provider in ("wechat", "wecom"):
            body = {"msgtype": "text", "text": {"content": text}}
        else:
            body = {"workspace_id": workspace_id, "channel": channel, "external_id": external_id, "text": text}
        resp = httpx.post(outbound_url, json=body, timeout=15)
        resp.raise_for_status()
        return True, {"provider": provider, "status_code": resp.status_code}

    return True, {"provider": provider, "status": "stubbed", "external_id": external_id}


def send_outbound(db: Session, workspace_id: str, channel: str, external_id: str, text: str, conversation_id: str = "", meta: dict | None = None) -> dict:
    """Deliver one reply to a customer channel and persist it as an out message."""
    cfg = chat_settings(db, workspace_id)
    provider = (cfg.get("provider") or "stub").lower()
    try:
        sent, delivery = _deliver(provider, cfg, workspace_id, channel, external_id, text)
    except Exception as exc:  # noqa: BLE001 - delivery is best-effort and recorded
        sent = False
        delivery = {"provider": provider, "error": str(exc)}

    msg = record_message(
        db,
        workspace_id,
        conversation_id,
        "out",
        text,
        {**(meta or {}), "delivery": delivery, "channel": channel, "external_id": external_id},
    )
    return {
        "sent": sent,
        "message_id": msg.id,
        "delivery": delivery,
        "text": text,
        "channel": channel,
        "external_id": external_id,
    }
