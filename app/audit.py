"""Append-only audit trail."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .integrity import GENESIS, link_hash, verify_chain
from .models import AuditLog, Decision


def default_data_tier(action: str, risk_level: str) -> str:
    """Map an audited action to a data tier when the caller did not set one."""
    if risk_level in ("R4",):
        return "CRITICAL"
    if risk_level in ("R3",) or any(word in action.lower() for word in ("pay", "refund", "dispute", "credential", "key")):
        return "SENSITIVE"
    return "INTERNAL"


def record(
    db: Session,
    *,
    workspace_id: str,
    actor_id: str,
    action: str,
    risk_level: str,
    decision: Decision,
    resource: str = "",
    reason: str = "",
    evidence: dict | None = None,
    data_tier: str | None = None,
    data_labels: str = "",
    request_id: str | None = None,
) -> AuditLog:
    evidence = dict(evidence or {})
    if request_id is None:
        from .request_id import get_request_id

        request_id = get_request_id()
    if request_id:
        evidence["request_id"] = request_id
    payload = {
        "action": action,
        "resource": resource,
        "risk_level": risk_level,
        "decision": decision.value,
        "reason": reason,
        "evidence": evidence,
        "data_tier": data_tier or default_data_tier(action, risk_level),
        "data_labels": data_labels,
    }
    prev = db.execute(
        select(AuditLog.content_hash)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    ).scalar() or GENESIS
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action,
        resource=resource,
        risk_level=risk_level,
        decision=decision.value,
        reason=reason,
        evidence=evidence,
        data_tier=payload["data_tier"],
        data_labels=data_labels,
        prev_hash=prev,
        content_hash=link_hash(prev, payload),
    )
    db.add(entry)
    db.flush()
    return entry


def verify_workspace_chain(db: Session, workspace_id: str) -> dict:
    """Rebuild and verify this workspace's audit hash chain.

    Returns ``{ok, total, broken_index}``; an empty chain reports ok.
    """
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).scalars().all()
    recs = [
        {
            "prev_hash": r.prev_hash,
            "content_hash": r.content_hash,
            "payload": {
                "action": r.action,
                "resource": r.resource,
                "risk_level": r.risk_level,
                "decision": r.decision,
                "reason": r.reason,
                "evidence": r.evidence,
                "data_tier": r.data_tier,
                "data_labels": r.data_labels,
            },
        }
        for r in rows
    ]
    ok, broken = verify_chain(recs)
    return {"ok": ok, "total": len(rows), "broken_index": broken}
