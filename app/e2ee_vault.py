"""Owner-only E2EE ciphertext vault (server stores, never decrypts).

The server holds no key and exposes no decrypt routine. Clients encrypt with a
device-held key (Windows Hello / Touch ID / Passkey unlocked) and send the
ciphertext envelope here; the server persists it and returns it for the client
to decrypt locally. ``blind_index`` (a keyed HMAC from the client) enables
exact-equality lookup only; range/fuzzy search is intentionally unsupported.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import E2EEBlob

ALLOWED_FIELDS = {"real_name", "real_phone", "funding", "credential", "direct_message"}


class VaultError(Exception):
    """Raised on invalid store/lookup; mapped to HTTP 400/409 by routes."""


def store(
    db: Session,
    actor,
    *,
    owner_ref: str,
    field: str,
    ciphertext: str,
    nonce: str,
    alg: str = "aes-gcm",
    blind_index: str = "",
) -> E2EEBlob:
    if field not in ALLOWED_FIELDS:
        raise VaultError(f"field not encryptable: {field}")
    if not owner_ref.strip() or not ciphertext.strip():
        raise VaultError("owner_ref and ciphertext are required")

    row = db.execute(
        select(E2EEBlob).where(
            E2EEBlob.workspace_id == actor.workspace_id,
            E2EEBlob.owner_ref == owner_ref,
            E2EEBlob.field == field,
        )
    ).scalars().first()
    if row is None:
        row = E2EEBlob(workspace_id=actor.workspace_id, owner_ref=owner_ref, field=field)
        db.add(row)
    row.ciphertext = ciphertext
    row.nonce = nonce
    row.alg = alg
    row.blind_index = blind_index or ""
    db.flush()
    return row


def fetch(db: Session, actor, *, owner_ref: str, field: str) -> E2EEBlob | None:
    return db.execute(
        select(E2EEBlob).where(
            E2EEBlob.workspace_id == actor.workspace_id,
            E2EEBlob.owner_ref == owner_ref,
            E2EEBlob.field == field,
        )
    ).scalars().first()


def find_by_blind_index(db: Session, actor, *, field: str, blind_index: str) -> list[E2EEBlob]:
    if not blind_index.strip():
        raise VaultError("blind_index is required for exact lookup")
    if field not in ALLOWED_FIELDS:
        raise VaultError(f"field not encryptable: {field}")
    return db.execute(
        select(E2EEBlob).where(
            E2EEBlob.workspace_id == actor.workspace_id,
            E2EEBlob.field == field,
            E2EEBlob.blind_index == blind_index,
        )
    ).scalars().all()


def delete(db: Session, actor, *, owner_ref: str, field: str) -> bool:
    """Erase one ciphertext envelope; returns True when a row was removed."""
    row = fetch(db, actor, owner_ref=owner_ref, field=field)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def delete_owner(db: Session, actor, *, owner_ref: str) -> int:
    """Erase every encrypted field for one owner_ref (data-minimization)."""
    rows = db.execute(
        select(E2EEBlob).where(
            E2EEBlob.workspace_id == actor.workspace_id,
            E2EEBlob.owner_ref == owner_ref,
        )
    ).scalars().all()
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


def list_fields(db: Session, actor, *, owner_ref: str) -> list[dict]:
    """List stored field names/alg for an owner_ref WITHOUT ciphertext/nonce."""
    rows = db.execute(
        select(E2EEBlob).where(
            E2EEBlob.workspace_id == actor.workspace_id,
            E2EEBlob.owner_ref == owner_ref,
        )
    ).scalars().all()
    return [{"field": r.field, "alg": r.alg} for r in rows]


def envelope(row: E2EEBlob) -> dict:
    return {
        "owner_ref": row.owner_ref,
        "field": row.field,
        "alg": row.alg,
        "nonce": row.nonce,
        "ciphertext": row.ciphertext,
        "blind_index": row.blind_index,
    }
