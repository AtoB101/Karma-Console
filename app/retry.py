"""Transient-error classification and exponential backoff for task steps.

Only gateway-confirmed transient failures are retried: unresolved receipts
(UNKNOWN/PENDING verification, needs_reconcile) and explicit network
transient errors on tools declared idempotent. Permanent errors (invalid
params, denied policy, unknown tool) fail fast; unclassified errors fail safe
and are never retried, so a side effect can never be blindly duplicated.

Retries reuse the same idempotency key handed to the gateway, which keeps the
execution idempotent at the choke point.
"""
from __future__ import annotations

import asyncio

# Network / service transient signals (matched case-insensitively in error text).
TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "unreachable",
    "transport error",
    "connection reset",
    "connection refused",
    "temporarily",
    "502",
    "503",
    "504",
    "service unavailable",
    "rate limit",
    "econnreset",
    "dns failure",
    "gateway timeout",
    "bad gateway",
)

# Permanent signals: retrying cannot help and may cause harm.
PERMANENT_PATTERNS = (
    "unknown tool",
    "permission",
    "denied",
    "invalid",
    "validation",
    "400",
    "401",
    "403",
    "404",
    "schema",
    "missing required",
    "policy",
)

RETRYABLE = "retryable"
PERMANENT = "permanent"
NONE = "none"


def classify_step_failure(
    result: dict | None,
    *,
    decision: str,
    idempotent: bool,
) -> str:
    """Classify a gateway step result into retryable / permanent / none.

    ``decision`` is the Gateway decision value (ALLOWED/PENDING/DENIED...).
    PENDING (needs approval) and DENIED are never retried here.
    """
    if decision != "ALLOWED":
        return NONE
    if not isinstance(result, dict):
        return NONE

    verification = result.get("verification")
    if isinstance(verification, dict) and verification.get("status") in ("UNKNOWN", "PENDING"):
        return RETRYABLE
    if result.get("needs_reconcile"):
        return RETRYABLE

    error = str(result.get("error") or "").lower()
    if not error:
        return NONE
    if any(p in error for p in PERMANENT_PATTERNS):
        return PERMANENT
    if idempotent and any(p in error for p in TRANSIENT_PATTERNS):
        return RETRYABLE
    # Unknown error: fail safe, do not retry a side effect blindly.
    return PERMANENT


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff for a 0-based attempt: base * 2**attempt, capped."""
    return min(cap, base * (2 ** max(attempt, 0)))


async def sleep_backoff(attempt: int, base: float, cap: float) -> None:
    await asyncio.sleep(backoff_seconds(attempt, base, cap))
