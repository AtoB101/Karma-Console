"""Deterministic hash-chain integrity helpers (no external dependency).

Each audited record carries a ``content_hash`` chained from the previous
record's hash, so any later edit/deletion of an entry breaks the chain and
``verify_chain`` detects it. This proves the trail was not tampered with
*without* relying on a trusted server; it is not a zero-knowledge proof —
that layer (zkSNARK setup/prover) is tracked separately in
``docs/EXTERNAL_DEPENDENCIES.md``.
"""
from __future__ import annotations

import hashlib
import json

GENESIS = "0" * 64


def canonical(obj) -> str:
    """Stable JSON serialization (sorted keys, no extra whitespace)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def link_hash(prev_hash: str, payload: dict) -> str:
    """Content hash that binds ``payload`` to ``prev_hash``."""
    return digest({"prev": prev_hash, "payload": payload})


def verify_chain(records: list[dict]) -> tuple[bool, int | None]:
    """Verify a hash chain.

    Each record dict must carry ``prev_hash``, ``content_hash`` and the plain
    business fields used to rebuild the hash (``payload``). Returns
    ``(ok, broken_index)``; ``broken_index`` is None when the chain is intact.
    """
    prev = GENESIS
    for i, r in enumerate(records):
        expected = link_hash(prev, r.get("payload", {}))
        if r.get("prev_hash") != prev or r.get("content_hash") != expected:
            return False, i
        prev = r.get("content_hash", "")
    return True, None
