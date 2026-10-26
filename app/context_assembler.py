"""Deterministic relevance recall for durable memory (lightweight RAG, no deps).

The planner used to dump every memory into the prompt; as memory grows that
dilutes the relevant preference/fact and raises hallucination. This module scores
each memory by token overlap with the current instruction and surfaces only the
top-K relevant entries. It is pure and deterministic, so it is unit-testable
without a model or network.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentMemory

_EN = re.compile(r"[a-z0-9]+")


def _bigrams(text: str) -> set[str]:
    """Token set: english/lowercase alnum words + Chinese character bigrams."""
    toks: set[str] = set(_EN.findall((text or "").lower()))
    for chunk in re.findall(r"[^\x00-\x7f]+", text or ""):
        cleaned = re.sub(r"[^\w]", "", chunk)
        for i in range(len(cleaned) - 1):
            toks.add(cleaned[i : i + 2])
    return toks


def rank_memories(memories, query: str, k: int = 5, min_hits: int = 1) -> list:
    """Return top-K memories by token-overlap score, highest relevance first."""
    q = _bigrams(query)
    scored: list[tuple[int, object]] = []
    for m in memories:
        overlap = len(q & _bigrams(f"{m.key} {m.value}"))
        if overlap >= min_hits:
            scored.append((overlap, m))
    scored.sort(key=lambda x: (-x[0], str(getattr(x[1], "key", ""))))
    return [m for _, m in scored[:k]]


def assemble_relevant_facts(db: Session, workspace_id: str, query: str, k: int = 5) -> list:
    """Fetch workspace memories and return those most relevant to ``query``."""
    rows = db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == workspace_id)
    ).scalars().all()
    return rank_memories(rows, query, k=k)


def format_facts(rows) -> str:
    if not rows:
        return ""
    body = "\n".join(f"- {m.key}: {m.value}" for m in rows)
    return "与本次指令最相关的持久记忆（已按相关度筛选，优先遵循）：\n" + body


def trim_feedback(feedback: list[str], max_items: int = 8) -> list[str]:
    """Keep only the most recent ``max_items`` execution-feedback lines.

    The agent loop accumulates one line per executed step; without a cap the
    prompt grows unbounded across iterations. Deterministic and pure.
    """
    if max_items <= 0:
        return []
    return list(feedback[-max_items:])
