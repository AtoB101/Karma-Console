"""O(1) chat-token lookup index with a safe full-scan fallback.

Saving a chat token upserts its hash here; inbound authentication first probes
this unique index. A miss falls back to scanning workspace settings so older
records (index not yet populated) keep working. The settings copy remains the
source of truth; the index is only an accelerator and is re-verified against
settings before use.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import crypto
from .models import ChatTokenIndex, Workspace


def upsert(db: Session, workspace_id: str, token_hash: str) -> None:
    """Replace any existing index row for this workspace; keep token_hash unique."""
    # Explicit DELETE statements (flushed before insert) so a repeated save under
    # the same token never trips the token_hash UNIQUE constraint.
    db.execute(delete(ChatTokenIndex).where(ChatTokenIndex.workspace_id == workspace_id))
    db.execute(delete(ChatTokenIndex).where(ChatTokenIndex.token_hash == token_hash))
    db.flush()
    db.add(ChatTokenIndex(workspace_id=workspace_id, token_hash=token_hash))
    db.flush()


def _settings_match(ws: Workspace, token: str, computed_hash: str) -> bool:
    chat_cfg = (ws.settings or {}).get("chat") or {}
    if not chat_cfg.get("enabled"):
        return False
    if chat_cfg.get("token_hash"):
        return crypto.compare_hashes(computed_hash, chat_cfg["token_hash"])
    if chat_cfg.get("token_enc"):
        return crypto.decrypt(chat_cfg["token_enc"]) == token
    if chat_cfg.get("token"):
        return chat_cfg["token"] == token
    return False


def resolve(db: Session, token: str) -> Workspace | None:
    if not token:
        return None
    computed_hash = crypto.hash_value(token)

    indexed = db.execute(
        select(ChatTokenIndex).where(ChatTokenIndex.token_hash == computed_hash)
    ).scalars().first()
    if indexed is not None:
        ws = db.get(Workspace, indexed.workspace_id)
        if ws is not None and _settings_match(ws, token, computed_hash):
            return ws

    # Fallback: scan settings for records not yet indexed (backward compatible).
    for ws in db.execute(select(Workspace)).scalars().all():
        if _settings_match(ws, token, computed_hash):
            return ws
    return None
