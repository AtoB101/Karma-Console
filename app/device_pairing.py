"""Device pairing registration for multi-device key sync (server-side only).

The server stores the device name and the Passkey/credential handle; it never
stores the master key. A pending pairing holds a one-time numeric code that an
already-authorized device (or the owner) presents to authorize the new device.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DevicePairing


class PairingError(Exception):
    """Raised on pairing request/authorize failures; mapped to HTTP 400/404/409."""


def request_pairing(db: Session, actor, *, device_name: str) -> DevicePairing:
    if not device_name.strip():
        raise PairingError("device_name is required")
    row = DevicePairing(
        workspace_id=actor.workspace_id,
        device_name=device_name.strip(),
        pair_code=f"{secrets.randbelow(1_000_000):06d}",
        status="pending",
    )
    db.add(row)
    db.flush()
    return row


def authorize(db: Session, actor, *, pair_code: str, device_name: str, credential_handle: str) -> DevicePairing:
    row = db.execute(
        select(DevicePairing).where(
            DevicePairing.workspace_id == actor.workspace_id,
            DevicePairing.pair_code == pair_code,
            DevicePairing.status == "pending",
        )
    ).scalars().first()
    if row is None:
        raise PairingError("invalid or already-used pairing code")
    if device_name.strip():
        row.device_name = device_name.strip()
    row.credential_handle = credential_handle or ""
    row.status = "authorized"
    row.pair_code = ""
    row.authorized_at = datetime.utcnow()
    db.flush()
    return row


def revoke(db: Session, actor, *, pairing_id: str) -> DevicePairing:
    row = db.get(DevicePairing, pairing_id)
    if row is None or row.workspace_id != actor.workspace_id:
        raise PairingError("pairing not found")
    row.status = "revoked"
    row.revoked_at = datetime.utcnow()
    db.flush()
    return row


def list_devices(db: Session, actor) -> list[dict]:
    rows = db.execute(
        select(DevicePairing)
        .where(DevicePairing.workspace_id == actor.workspace_id)
        .order_by(DevicePairing.created_at)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "device_name": r.device_name,
            "status": r.status,
            "has_credential": bool(r.credential_handle),
            "authorized_at": r.authorized_at.isoformat() if r.authorized_at else None,
        }
        for r in rows
    ]
