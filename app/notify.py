"""Owner notification: numeric confirmation codes and email delivery.

Important actions generate a 6-digit code that is emailed to the owner
(or returned as a stub notice when SMTP is not configured). The owner
confirms by entering the correct number; permission never leaves the owner.
"""
from __future__ import annotations

import random
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings
from .models import Workspace


def generate_code(length: int = 6) -> str:
    return "".join(str(random.SystemRandom().randint(0, 9)) for _ in range(length))


def _default_smtp() -> dict:
    return {
        "host": settings.smtp_host,
        "port": int(settings.smtp_port),
        "user": settings.smtp_user,
        "password": settings.smtp_password,
        "from": settings.smtp_from,
        "tls": settings.smtp_tls,
    }


def resolve_smtp(db, workspace_id: str) -> dict:
    """Per-workspace SMTP config, falling back to process-level env vars."""
    cfg = _default_smtp()
    ws = db.get(Workspace, workspace_id) if db is not None else None
    if ws is not None:
        raw = dict((ws.settings or {}).get("smtp") or {})
        if raw.get("host"):
            cfg["host"] = raw["host"]
        if raw.get("port"):
            cfg["port"] = int(raw["port"])
        if raw.get("user"):
            cfg["user"] = raw["user"]
        password = ""
        if raw.get("password_enc"):
            from . import crypto

            password = crypto.decrypt(raw["password_enc"])
        elif raw.get("password"):
            password = raw["password"]
        if password:
            cfg["password"] = password
        if raw.get("from"):
            cfg["from"] = raw["from"]
        if "tls" in raw:
            cfg["tls"] = bool(raw["tls"])
    return cfg


def owner_email_for(db, workspace_id: str) -> str:
    ws = db.get(Workspace, workspace_id) if db is not None else None
    if ws is not None:
        email = (ws.settings or {}).get("owner_email") or ""
        if email:
            return email
    return settings.owner_email


def send_email(cfg: dict, to: str, subject: str, body: str) -> dict:
    if not (cfg.get("host") and cfg.get("from")):
        return {"sent": False, "provider": "stub", "note": "SMTP 未配置（host/from 缺失），确认码仅在操作台展示", "body": body}
    if not to:
        return {"sent": False, "provider": "stub", "note": "未设置收件邮箱（OWNER_EMAIL）", "body": body}
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["from"]
        msg["To"] = to
        msg.set_content(body)
        if cfg.get("tls"):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port") or 465), context=context, timeout=15) as server:
                if cfg.get("user"):
                    server.login(cfg["user"], cfg.get("password") or "")
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], int(cfg.get("port") or 25), timeout=15) as server:
                if cfg.get("user"):
                    server.starttls()
                    server.login(cfg["user"], cfg.get("password") or "")
                server.send_message(msg)
        return {"sent": True, "provider": "smtp", "to": to}
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        return {"sent": False, "provider": "smtp", "error": str(exc), "body": body}


def send_confirmation_email(to: str, subject: str, body: str) -> dict:
    return send_email(_default_smtp(), to, subject, body)


def notify_owner(workspace_id: str, subject: str, body: str, owner_email: str = "") -> dict:
    to = owner_email or settings.owner_email
    if not to:
        return {"sent": False, "provider": "stub", "note": "OWNER_EMAIL 未配置", "body": body}
    return send_email(_default_smtp(), to, subject, body)


def notify_owner_for_workspace(db, workspace_id: str, subject: str, body: str) -> dict:
    return send_email(resolve_smtp(db, workspace_id), owner_email_for(db, workspace_id), subject, body)
