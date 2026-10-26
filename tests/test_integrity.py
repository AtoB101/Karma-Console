"""P32 hash-chain integrity: intact chain verifies, tampering breaks it."""
from app.audit import record
from app.integrity import GENESIS, link_hash, verify_chain
from app.models import Decision


def _build(payloads):
    prev = GENESIS
    recs = []
    for p in payloads:
        h = link_hash(prev, p)
        recs.append({"prev_hash": prev, "content_hash": h, "payload": p})
        prev = h
    return recs


def test_intact_chain_verifies():
    recs = _build([{"a": 1}, {"a": 2}, {"a": 3}])
    ok, broken = verify_chain(recs)
    assert ok is True
    assert broken is None


def test_tampered_payload_breaks_chain():
    recs = _build([{"a": 1}, {"a": 2}, {"a": 3}])
    recs[1]["payload"] = {"a": 99}  # mutate stored business field
    ok, broken = verify_chain(recs)
    assert ok is False
    assert broken == 1


def test_rewritten_hash_breaks_chain():
    recs = _build([{"a": 1}, {"a": 2}])
    recs[0]["content_hash"] = "f" * 64
    ok, broken = verify_chain(recs)
    assert ok is False
    assert broken == 0


def test_audit_records_form_a_chain(client, workspace, db=None):
    from app.db import SessionLocal
    from app.models import AuditLog
    ws = workspace["workspace_id"]
    db = SessionLocal()
    try:
        e1 = record(db, workspace_id=ws, actor_id=workspace["user_id"],
                    action="alpha", risk_level="R0", decision=Decision.ALLOWED,
                    evidence={"n": 1})
        e2 = record(db, workspace_id=ws, actor_id=workspace["user_id"],
                    action="beta", risk_level="R0", decision=Decision.ALLOWED,
                    evidence={"n": 2})
        db.commit()
        rows = db.execute(
            __import__("sqlalchemy").select(AuditLog)
            .where(AuditLog.workspace_id == ws)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars().all()
        recs = [
            {"prev_hash": r.prev_hash, "content_hash": r.content_hash,
             "payload": {"action": r.action, "resource": r.resource,
                         "risk_level": r.risk_level, "decision": r.decision,
                         "reason": r.reason, "evidence": r.evidence,
                         "data_tier": r.data_tier, "data_labels": r.data_labels}}
            for r in rows
        ]
        ok, broken = verify_chain(recs)
        assert ok is True
        assert e2.prev_hash == e1.content_hash
    finally:
        db.rollback()
        db.close()


def test_integrity_endpoint_reports_intact_chain(client, workspace):
    from app.db import SessionLocal
    ws = workspace["workspace_id"]
    db = SessionLocal()
    record(db, workspace_id=ws, actor_id=workspace["user_id"],
           action="alpha", risk_level="R0", decision=Decision.ALLOWED, evidence={"n": 1})
    record(db, workspace_id=ws, actor_id=workspace["user_id"],
           action="beta", risk_level="R0", decision=Decision.ALLOWED, evidence={"n": 2})
    db.commit()
    db.close()

    r = client.get(f"/workspaces/{ws}/audit/integrity", headers=workspace["headers"])
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["total"] >= 2


def test_integrity_endpoint_detects_tampering(client, workspace):
    from app.db import SessionLocal
    from app.models import AuditLog
    ws = workspace["workspace_id"]
    db = SessionLocal()
    record(db, workspace_id=ws, actor_id=workspace["user_id"],
           action="orig", risk_level="R0", decision=Decision.ALLOWED, evidence={"n": 1})
    db.commit()
    row = db.query(AuditLog).filter(AuditLog.workspace_id == ws).first()
    row.action = "tampered"  # mutate stored business field after hashing
    db.commit()
    db.close()

    r = client.get(f"/workspaces/{ws}/audit/integrity", headers=workspace["headers"])
    body = r.json()
    assert body["ok"] is False
    assert body["broken_index"] is not None
