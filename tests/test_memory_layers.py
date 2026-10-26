"""P75 M07/M04 layered memory: deterministic note compression into protected
digests, core preference/fact protection, budgeted context block."""
from sqlalchemy import select

from app.db import SessionLocal
from app.memory_layers import build_memory_block, compact_notes
from app.models import AgentMemory


def _seed_notes(ws: str, n: int, prefix: str = "note"):
    db = SessionLocal()
    for i in range(n):
        db.add(AgentMemory(workspace_id=ws, key=f"{prefix}_{i:03d}", value=f"事件{i}", kind="note"))
    db.commit()
    db.close()


def test_compact_merges_oldest_notes_into_digest(workspace):
    ws = workspace["workspace_id"]
    _seed_notes(ws, 8)
    db = SessionLocal()
    try:
        db.add(AgentMemory(workspace_id=ws, key="pay_method", value="只收微信", kind="preference"))
        db.commit()
        report = compact_notes(db, ws, threshold=5, batch=3)
        db.commit()
    finally:
        db.close()

    assert report["compressed"] is True
    assert report["merged"] == 3
    db = SessionLocal()
    try:
        notes = db.execute(select(AgentMemory).where(
            AgentMemory.workspace_id == ws, AgentMemory.kind == "note")).scalars().all()
        digests = db.execute(select(AgentMemory).where(
            AgentMemory.workspace_id == ws, AgentMemory.kind == "digest")).scalars().all()
        prefs = db.execute(select(AgentMemory).where(
            AgentMemory.workspace_id == ws, AgentMemory.kind == "preference")).scalars().all()
    finally:
        db.close()
    assert len(notes) == 5
    assert len(digests) == 1
    # oldest notes (note_000..002) merged; newest kept
    keys = {n.key for n in notes}
    assert "note_000" not in keys and "note_007" in keys
    assert "note_000" in digests[0].value  # preserved inside digest
    # core preference untouched
    assert len(prefs) == 1 and prefs[0].key == "pay_method"


def test_compact_under_threshold_is_noop(workspace):
    ws = workspace["workspace_id"]
    _seed_notes(ws, 3)
    db = SessionLocal()
    try:
        report = compact_notes(db, ws, threshold=5, batch=2)
    finally:
        db.close()
    assert report["compressed"] is False
    assert report["merged"] == 0


def test_core_facts_never_counted_or_removed(workspace):
    ws = workspace["workspace_id"]
    db = SessionLocal()
    db.add_all([
        AgentMemory(workspace_id=ws, key="f1", value="事实", kind="fact"),
        AgentMemory(workspace_id=ws, key="p1", value="偏好", kind="preference"),
    ])
    db.commit()
    report = compact_notes(db, ws, threshold=1, batch=1)
    db.close()
    assert report["compressed"] is False
    db = SessionLocal()
    try:
        kinds = {m.kind for m in db.execute(select(AgentMemory).where(
            AgentMemory.workspace_id == ws)).scalars().all()}
    finally:
        db.close()
    assert {"fact", "preference"} <= kinds


def test_build_block_budget_keeps_core_first(workspace):
    ws = workspace["workspace_id"]
    db = SessionLocal()
    db.add(AgentMemory(workspace_id=ws, key="brand", value="品牌名ABC", kind="preference"))
    db.add(AgentMemory(workspace_id=ws, key="n1", value="x" * 500, kind="note"))
    db.commit()
    block = build_memory_block(db, ws, max_chars=60)
    db.close()
    assert "品牌名ABC" in block
    # oversized note dropped under budget, core retained
    assert "xxxxxx" not in block


def test_http_compact_endpoint(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    _seed_notes(ws, 10)
    r = client.post(f"/workspaces/{ws}/agent/memory/compact", headers=h,
                    json={"threshold": 5, "batch": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compressed"] is True
    assert body["merged"] == 4


def test_compact_isolation(client, workspace):
    ws = workspace["workspace_id"]
    _seed_notes(ws, 9, prefix="mine")
    db = SessionLocal()
    db.add(AgentMemory(workspace_id="other-ws", key="theirs", value="x", kind="note"))
    db.commit()
    report = compact_notes(db, ws, threshold=5, batch=3)
    db.commit()
    db.close()
    assert report["merged"] == 3
    db = SessionLocal()
    try:
        theirs = db.execute(select(AgentMemory).where(
            AgentMemory.workspace_id == "other-ws", AgentMemory.kind == "note")).scalars().all()
    finally:
        db.close()
    assert len(theirs) == 1
