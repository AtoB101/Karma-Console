"""Backup & recovery tests (plan M10).

Covers: create -> audit, owner-only gating, restore round-trip with integrity
check, explicit-confirm requirement, corrupted-backup rejection, retention
pruning, and the opt-in scheduled backup.
"""
from __future__ import annotations

import os
import pathlib
import re
import uuid

import pytest

import app.backup as backup_mod
from app.db import SessionLocal
from app.models import AuditLog, BackupRecord, Decision, Membership, Role

_SQLITE_URL_RE = re.compile(r"^sqlite:///(.*)$")


@pytest.fixture
def backup_env(monkeypatch, tmp_path):
    """Isolate the backup module: temp backup dir + the real (test) DB file."""
    monkeypatch.setattr(backup_mod, "_backup_dir", lambda: tmp_path)
    db_path = _SQLITE_URL_RE.match(os.environ["DATABASE_URL"]).group(1)
    monkeypatch.setattr(backup_mod, "_db_path", lambda: pathlib.Path(db_path))
    return tmp_path


def _create_product(client, ws, headers, name):
    return client.post(
        f"/workspaces/{ws}/products",
        json={"name": name, "price": 99.0, "category": "clothing"},
        headers=headers,
    )


def _product_names(client, ws, headers):
    r = client.get(f"/workspaces/{ws}/products", headers=headers)
    assert r.status_code == 200
    return {p["name"] for p in r.json()["products"]}


def _second_user(client):
    email = f"member{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/users", json={"email": email, "display_name": "Member"})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = client.post(f"/users/{user_id}/api-keys")
    assert r.status_code == 201, r.text
    return user_id, r.json()["api_key"]


def _add_membership(workspace_id: str, user_id: str, role: str) -> None:
    db = SessionLocal()
    try:
        db.add(Membership(workspace_id=workspace_id, user_id=user_id, role=role))
        db.commit()
    finally:
        db.close()


def _backup_rows(db) -> list[BackupRecord]:
    return db.query(BackupRecord).order_by(BackupRecord.created_at.desc()).all()


def _clear_backup_records() -> None:
    """Tests share one session DB; wipe backup records so retention/scheduler
    assertions are deterministic."""
    db = SessionLocal()
    try:
        db.query(BackupRecord).delete()
        db.commit()
    finally:
        db.close()


def test_backup_create_list_and_audit(client, workspace, backup_env):
    r = client.post("/backups", json={"reason": "pre-upgrade"}, headers=workspace["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reason"] == "pre-upgrade"
    assert len(body["checksum"]) == 64
    assert body["filename"].endswith(".db")
    assert (backup_env / body["filename"]).exists()

    r = client.get("/backups", headers=workspace["headers"])
    assert r.status_code == 200
    assert body["id"] in [b["id"] for b in r.json()["backups"]]

    db = SessionLocal()
    try:
        row = db.get(BackupRecord, body["id"])
        assert row is not None and row.status == "ok" and row.size_bytes > 0
        event = (
            db.query(AuditLog)
            .filter(AuditLog.action == "backup.create", AuditLog.resource == f"backup:{row.id}")
            .first()
        )
        assert event is not None and event.decision == Decision.ALLOWED.value
        assert event.data_tier == "SENSITIVE"
    finally:
        db.close()


def test_backup_requires_auth(client, workspace, backup_env):
    assert client.get("/backups").status_code == 401
    assert client.post("/backups", json={}).status_code == 401


def test_backup_requires_owner(client, workspace, backup_env):
    ws = workspace["workspace_id"]
    user_id, key = _second_user(client)
    _add_membership(ws, user_id, Role.MEMBER.value)
    member_headers = {
        "Authorization": f"Bearer {key}",
        "X-Workspace-Id": ws,
    }
    assert client.get("/backups", headers=member_headers).status_code == 403
    assert client.post("/backups", json={}, headers=member_headers).status_code == 403
    assert client.post("/backups/whatever/restore", json={"confirm": True}, headers=member_headers).status_code == 403
    assert client.post("/backups/prune", headers=member_headers).status_code == 403


def test_restore_round_trip(client, workspace, backup_env):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    _create_product(client, ws, h, "衬衫")
    backup = client.post("/backups", json={"reason": "snapshot"}, headers=h)
    assert backup.status_code == 201, backup.text
    backup_id = backup.json()["id"]

    _create_product(client, ws, h, "牛仔裤")
    assert _product_names(client, ws, h) == {"衬衫", "牛仔裤"}

    r = client.post(f"/backups/{backup_id}/restore", json={"confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["integrity"] == "ok"
    safety_id = r.json()["safety_backup_id"]
    assert safety_id != backup_id

    assert _product_names(client, ws, h) == {"衬衫"}

    db = SessionLocal()
    try:
        row = db.get(BackupRecord, backup_id)
        assert row is not None and row.status == "restored" and row.restored_at is not None
        safety = db.get(BackupRecord, safety_id)
        assert safety is not None and "pre-restore" in safety.reason
        event = (
            db.query(AuditLog)
            .filter(AuditLog.action == "backup.restore", AuditLog.resource == f"backup:{backup_id}")
            .first()
        )
        assert event is not None and event.decision == Decision.ALLOWED.value
        assert event.risk_level == "R4" and event.data_tier == "CRITICAL"
    finally:
        db.close()


def test_restore_requires_confirm(client, workspace, backup_env):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    _create_product(client, ws, h, "衬衫")
    backup = client.post("/backups", json={}, headers=h)
    assert backup.status_code == 201
    backup_id = backup.json()["id"]

    _create_product(client, ws, h, "牛仔裤")

    r = client.post(f"/backups/{backup_id}/restore", json={"confirm": False}, headers=h)
    assert r.status_code == 409
    assert _product_names(client, ws, h) == {"衬衫", "牛仔裤"}  # unchanged

    db = SessionLocal()
    try:
        assert db.get(BackupRecord, backup_id).status == "ok"  # not consumed
    finally:
        db.close()


def test_restore_rejects_corrupted_backup(client, workspace, backup_env):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    _create_product(client, ws, h, "衬衫")
    backup = client.post("/backups", json={}, headers=h)
    assert backup.status_code == 201
    backup_id = backup.json()["id"]
    filename = backup.json()["filename"]

    with open(backup_env / filename, "ab") as fh:
        fh.write(b"corrupt")

    r = client.post(f"/backups/{backup_id}/restore", json={"confirm": True}, headers=h)
    assert r.status_code == 409
    assert "checksum" in r.text

    db = SessionLocal()
    try:
        assert db.get(BackupRecord, backup_id).status == "failed"
        denied = (
            db.query(AuditLog)
            .filter(AuditLog.action == "backup.restore", AuditLog.resource == f"backup:{backup_id}")
            .first()
        )
        assert denied is not None and denied.decision == Decision.DENIED.value
    finally:
        db.close()


def test_restore_unknown_backup(client, workspace, backup_env):
    r = client.post(f"/backups/{uuid.uuid4().hex}/restore", json={"confirm": True}, headers=workspace["headers"])
    assert r.status_code == 409


def test_prune_retention_keeps_newest(client, workspace, backup_env):
    _clear_backup_records()
    ids = []
    for i in range(3):
        r = client.post("/backups", json={"reason": f"batch-{i}"}, headers=workspace["headers"])
        assert r.status_code == 201
        ids.append(r.json()["id"])

    db = SessionLocal()
    try:
        pruned = backup_mod.prune_backups(db, keep=2)
        assert pruned == 1
        rows = {b.id: b for b in _backup_rows(db)}
        assert rows[ids[0]].status == "pruned"  # oldest
        assert rows[ids[1]].status == "ok"
        assert rows[ids[2]].status == "ok"  # newest kept
        assert not (backup_env / rows[ids[0]].filename).exists()
        assert (backup_env / rows[ids[2]].filename).exists()
    finally:
        db.close()


def test_scheduled_backup_opt_in_and_interval(client, workspace, backup_env, monkeypatch):
    import types

    monkeypatch.setattr(
        backup_mod,
        "settings",
        types.SimpleNamespace(
            backup_enabled=True, backup_interval_hours=24, backup_retention=7, backup_dir=str(backup_env)
        ),
    )
    ws = workspace["workspace_id"]
    _create_product(client, ws, workspace["headers"], "衬衫")

    db = SessionLocal()
    try:
        _clear_backup_records()
        # Disabled -> no-op
        backup_mod.settings.backup_enabled = False
        assert backup_mod.maybe_auto_backup(db) is None
        backup_mod.settings.backup_enabled = True

        first = backup_mod.maybe_auto_backup(db)
        db.commit()
        assert first is not None and first.reason == "scheduled"

        # Fresh backup -> no second backup
        assert backup_mod.maybe_auto_backup(db) is None

        # Stale backup -> new one is created
        first.created_at = first.created_at.replace(year=2020)
        db.commit()
        second = backup_mod.maybe_auto_backup(db)
        db.commit()
        assert second is not None and second.id != first.id
    finally:
        db.close()


def test_backup_dir_must_be_configured(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(backup_mod, "settings", SimpleNamespace(backup_dir=""))
    with pytest.raises(backup_mod.BackupError, match="not configured"):
        backup_mod._backup_dir()


def test_backup_dir_rejects_unwritable_target(monkeypatch):
    from types import SimpleNamespace
    # A drive/path that cannot be created on Windows must fail loudly, not silently.
    monkeypatch.setattr(backup_mod, "settings", SimpleNamespace(backup_dir="Z:\\krama_no_such_drive\\snapshots"))
    with pytest.raises(backup_mod.BackupError, match="not writable"):
        backup_mod._backup_dir()


def test_backup_dir_accepts_writable_target(monkeypatch, tmp_path):
    from types import SimpleNamespace
    target = tmp_path / "vault"
    monkeypatch.setattr(backup_mod, "settings", SimpleNamespace(backup_dir=str(target)))
    resolved = backup_mod._backup_dir()
    assert resolved == target
    assert resolved.exists()
