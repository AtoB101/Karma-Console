"""P6 tiered retention: purge expired volatile records, protect audit/business data."""
from datetime import datetime, timedelta, timezone

from app.models import AuditLog, Decision, Message, RiskLevel, Todo
from app.retention import CLEANUP_RULES, PROTECTED, cleanup_workspace


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _old(days: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


def test_expired_messages_purged_recent_kept(workspace):
    db = _db()
    try:
        ws = workspace["workspace_id"]
        old = Message(workspace_id=ws, direction="in", content="old", created_at=_old(40))
        new = Message(workspace_id=ws, direction="in", content="new", created_at=_old(1))
        db.add_all([old, new])
        db.commit()

        report = cleanup_workspace(db, ws)
        db.commit()
        assert report["messages"] == 1
        remaining = db.query(Message).filter(Message.workspace_id == ws).all()
        assert [m.content for m in remaining] == ["new"]
    finally:
        db.rollback()
        db.close()


def test_done_todos_purged_pending_kept(workspace):
    db = _db()
    try:
        ws = workspace["workspace_id"]
        done = Todo(workspace_id=ws, kind="info", title="done", status="done", created_at=_old(40))
        pending = Todo(workspace_id=ws, kind="must_confirm", title="pending", status="pending", created_at=_old(40))
        db.add_all([done, pending])
        db.commit()

        report = cleanup_workspace(db, ws)
        db.commit()
        assert report["todos"] == 1
        remaining = db.query(Todo).filter(Todo.workspace_id == ws).all()
        assert [t.title for t in remaining] == ["pending"]
    finally:
        db.rollback()
        db.close()


def test_audit_and_business_tables_protected():
    assert "audit_logs" in PROTECTED
    assert "orders" in PROTECTED
    assert "customers" in PROTECTED
    assert "agent_memories" in PROTECTED
    # protected tables must never appear in the cleanup schedule
    for table in PROTECTED:
        assert table not in CLEANUP_RULES


def test_policy_endpoint(client, workspace):
    r = client.get(
        f"/workspaces/{workspace['workspace_id']}/retention/policy", headers=workspace["headers"]
    )
    assert r.status_code == 200
    body = r.json()
    assert "rules" in body
    assert "audit_logs" in body["protected_tables"]


def test_run_retention_owner_endpoint(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/retention/run", headers=workspace["headers"]
    )
    assert r.status_code == 200
    assert "purged" in r.json()


def test_audit_log_row_survives_cleanup(workspace):
    db = _db()
    try:
        ws = workspace["workspace_id"]
        entry = AuditLog(
            workspace_id=ws,
            actor_id=workspace["user_id"],
            action="test.action",
            risk_level=RiskLevel.R0.value,
            decision=Decision.ALLOWED,
            created_at=_old(400),
        )
        db.add(entry)
        db.commit()
        cleanup_workspace(db, ws)
        db.commit()
        count = db.query(AuditLog).filter(AuditLog.workspace_id == ws).count()
        assert count >= 1
    finally:
        db.rollback()
        db.close()
