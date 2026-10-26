"""Durable-memory capacity governance (bounded, safety-first).

The RAG recall already surfaces only the top-K relevant entries, but the
underlying ``agent_memories`` rows had no size ceiling: free-form ``note`` rows
accumulate unbounded. This module enforces a per-workspace ceiling and reclaims
oldest ``note`` rows only. ``preference`` and ``fact`` rows are protected and are
never deleted here (they carry durable owner preferences and verified facts).

No table migration is required: ordering uses the existing ``created_at``.
"""
from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import AgentMemory

DEFAULT_CAP = 200
PROTECTED_KINDS = {"preference", "fact"}


def _count(db: Session, workspace_id: str) -> int:
    return db.execute(
        select(func.count(AgentMemory.id)).where(AgentMemory.workspace_id == workspace_id)
    ).scalar_one()


def enforce_capacity(db: Session, workspace_id: str, cap: int = DEFAULT_CAP) -> dict:
    """Reclaim oldest ``note`` rows until the workspace is within ``cap``.

    Returns a governance report. Protected kinds are never removed; if they
    alone exceed the cap the workspace simply stays over rather than dropping
    durable preferences/facts.
    """
    if cap <= 0:
        cap = DEFAULT_CAP

    rows = db.execute(
        select(AgentMemory)
        .where(AgentMemory.workspace_id == workspace_id)
        .order_by(AgentMemory.created_at.asc(), AgentMemory.key.asc())
    ).scalars().all()
    total = len(rows)
    removed = 0
    excess = total - cap
    if excess > 0:
        # Oldest reclaimable first: only free-form notes are eligible.
        candidates = [r for r in rows if r.kind not in PROTECTED_KINDS]
        for row in candidates:
            if excess <= 0:
                break
            db.delete(row)
            removed += 1
            excess -= 1
    db.flush()
    remaining = _count(db, workspace_id)
    return {
        "workspace_id": workspace_id,
        "cap": cap,
        "total": total,
        "removed": removed,
        "remaining": remaining,
        "protected_kept": sum(1 for r in rows if r.kind in PROTECTED_KINDS),
    }
