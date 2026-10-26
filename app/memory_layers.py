"""M07/M04 layered long-term memory with deterministic compression.

Layers (by kind on the existing ``agent_memories`` table, no migration):

* **Core layer** ``preference`` / ``fact`` — protected, always kept in full,
  never compressed or deleted here.
* **Digest layer** ``digest`` — batched summaries of old notes; protected from
  re-compression so batches cannot recursively grow.
* **Event layer** ``note`` — free-form chronological events; compressible.

When the note count exceeds ``compact_threshold``, the oldest batch of notes is
merged into one deterministic ``digest`` row (no LLM call) instead of being
silently dropped. ``build_memory_block`` assembles a context string under a
character budget: core first, then digests, then recent notes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentMemory

CORE_KINDS = {"preference", "fact"}
DIGEST_KIND = "digest"
COMPRESSIBLE_KINDS = {"note"}

DEFAULT_COMPACT_THRESHOLD = 40
DEFAULT_BATCH = 20
DEFAULT_CONTEXT_CHARS = 1800
_DIGEST_LINE_MAX = 80


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def compact_notes(
    db: Session,
    workspace_id: str,
    threshold: int = DEFAULT_COMPACT_THRESHOLD,
    batch: int = DEFAULT_BATCH,
) -> dict:
    """Merge the oldest batch of notes over ``threshold`` into one digest row.

    Core rows (preference/fact) and existing digests are never touched. Returns
    a report; when under threshold it is a no-op.
    """
    if threshold <= 0:
        threshold = DEFAULT_COMPACT_THRESHOLD
    if batch <= 0:
        batch = DEFAULT_BATCH

    notes = db.execute(
        select(AgentMemory)
        .where(AgentMemory.workspace_id == workspace_id, AgentMemory.kind.in_(COMPRESSIBLE_KINDS))
        .order_by(AgentMemory.created_at.asc(), AgentMemory.key.asc())
    ).scalars().all()

    excess = len(notes) - threshold
    if excess <= 0:
        return {
            "compressed": False,
            "notes_before": len(notes),
            "threshold": threshold,
            "merged": 0,
            "digest_key": None,
        }

    take = min(batch, excess)
    picked = notes[:take]
    lines = [f"- {m.key}: {(m.value or '')[:_DIGEST_LINE_MAX]}" for m in picked]
    digest_value = f"历史记录合并（共 {len(picked)} 条旧记录）：\n" + "\n".join(lines)
    digest_key = f"digest:compact-{_now_stamp()}-{len(picked)}"

    for row in picked:
        db.delete(row)
    db.add(AgentMemory(
        workspace_id=workspace_id,
        key=digest_key,
        value=digest_value,
        kind=DIGEST_KIND,
    ))
    db.flush()

    remaining_notes = db.execute(
        select(AgentMemory).where(
            AgentMemory.workspace_id == workspace_id,
            AgentMemory.kind.in_(COMPRESSIBLE_KINDS),
        )
    ).scalars().all()

    return {
        "compressed": True,
        "notes_before": len(notes),
        "threshold": threshold,
        "merged": len(picked),
        "digest_key": digest_key,
        "remaining_notes": len(remaining_notes),
    }


def build_memory_block(db: Session, workspace_id: str, max_chars: int = DEFAULT_CONTEXT_CHARS) -> str:
    """Assemble a budgeted memory block: core -> digest -> recent notes.

    Core rows are always emitted in full; digests then recent notes fill the
    remaining character budget. Never raises; returns a placeholder when empty.
    """
    if max_chars <= 0:
        max_chars = DEFAULT_CONTEXT_CHARS

    core = db.execute(
        select(AgentMemory)
        .where(AgentMemory.workspace_id == workspace_id, AgentMemory.kind.in_(CORE_KINDS))
        .order_by(AgentMemory.updated_at.desc())
    ).scalars().all()
    digests = db.execute(
        select(AgentMemory)
        .where(AgentMemory.workspace_id == workspace_id, AgentMemory.kind == DIGEST_KIND)
        .order_by(AgentMemory.updated_at.desc())
    ).scalars().all()
    notes = db.execute(
        select(AgentMemory)
        .where(AgentMemory.workspace_id == workspace_id, AgentMemory.kind.in_(COMPRESSIBLE_KINDS))
        .order_by(AgentMemory.updated_at.desc())
    ).scalars().all()

    lines: list[str] = []
    used = 0

    def _add(label: str, rows: list[AgentMemory]) -> None:
        nonlocal used
        for m in rows:
            line = f"- [{label}] {m.key}: {m.value}"
            if used + len(line) > max_chars and lines:
                return
            lines.append(line)
            used += len(line)

    _add("偏好/事实", core)
    _add("历史摘要", digests)
    _add("近期记录", notes)

    return "\n".join(lines) if lines else "（暂无长期记忆）"
