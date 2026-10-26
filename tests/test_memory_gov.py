"""Tests for durable-memory capacity governance (app/memory_gov.py).

Only free-form ``note`` rows are reclaimed past the ceiling; ``preference`` and
``fact`` are protected, and reclamation never crosses workspaces.
"""
from app.db import SessionLocal
from app.models import AgentMemory


def _seed(ws_id: str, kind: str, count: int, prefix: str) -> None:
    db = SessionLocal()
    try:
        for i in range(count):
            db.add(AgentMemory(workspace_id=ws_id, key=f"{prefix}{i}", value="v", kind=kind))
        db.commit()
    finally:
        db.close()


def _keys(ws_id: str) -> set[str]:
    db = SessionLocal()
    try:
        rows = db.query(AgentMemory).filter(AgentMemory.workspace_id == ws_id).all()
        return {r.key for r in rows}
    finally:
        db.close()


def test_enforce_reclaims_oldest_notes_keeps_protected(workspace):
    from app.memory_gov import enforce_capacity

    ws = workspace["workspace_id"]
    _seed(ws, "preference", 1, "pref_")
    _seed(ws, "fact", 1, "fact_")
    _seed(ws, "note", 5, "note_")

    db = SessionLocal()
    try:
        report = enforce_capacity(db, ws, cap=3)
        db.commit()
    finally:
        db.close()

    assert report["removed"] == 4
    assert report["remaining"] == 3
    keys = _keys(ws)
    # Protected rows survive regardless of age.
    assert "pref_0" in keys
    assert "fact_0" in keys
    # Oldest notes reclaimed first; newest note kept.
    assert "note_4" in keys
    assert "note_0" not in keys


def test_enforce_noop_under_cap(workspace):
    from app.memory_gov import enforce_capacity

    ws = workspace["workspace_id"]
    _seed(ws, "note", 3, "keep_")
    db = SessionLocal()
    try:
        report = enforce_capacity(db, ws, cap=50)
        db.commit()
    finally:
        db.close()
    assert report["removed"] == 0
    assert report["remaining"] == 3


def test_gc_endpoint_structure(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agent/memory/gc",
        headers=workspace["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("cap", "total", "removed", "remaining", "protected_kept"):
        assert field in body


def test_reclamation_isolated_per_workspace(client, workspace):
    from app.memory_gov import enforce_capacity

    ws_a = workspace["workspace_id"]
    # Second workspace.
    email = "gc_other@example.com"
    u = client.post("/users", json={"email": email, "display_name": "Other"}).json()
    key = client.post(f"/users/{u['id']}/api-keys").json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    ws_b = client.post(
        "/workspaces", json={"name": "B店", "region": "CN", "business_category": "clothing"}, headers=h
    ).json()["id"]

    _seed(ws_a, "note", 6, "a_")
    _seed(ws_b, "note", 6, "b_")

    db = SessionLocal()
    try:
        enforce_capacity(db, ws_a, cap=2)
        db.commit()
    finally:
        db.close()

    b_keys = _keys(ws_b)
    # B's notes are untouched by A's reclamation.
    assert {f"b_{i}" for i in range(6)}.issubset(b_keys)
