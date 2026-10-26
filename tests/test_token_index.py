"""P8 chat-token index: O(1) lookup, replacement on rotation, disabled rejection."""
from app import token_index
from app.db import SessionLocal
from app.models import ChatTokenIndex


def _save(client, workspace, chat):
    return client.put(
        f"/workspaces/{workspace['workspace_id']}/settings",
        json={"chat": chat},
        headers=workspace["headers"],
    )


def test_token_indexed_and_resolves(client, workspace):
    r = _save(client, workspace, {"provider": "telegram", "token": "alpha-1", "enabled": True})
    assert r.status_code == 200

    db = SessionLocal()
    try:
        rows = db.query(ChatTokenIndex).filter(
            ChatTokenIndex.workspace_id == workspace["workspace_id"]
        ).all()
        assert len(rows) == 1
        ws = token_index.resolve(db, "alpha-1")
        assert ws is not None and ws.id == workspace["workspace_id"]
    finally:
        db.rollback()
        db.close()


def test_rotated_token_replaces_index(client, workspace):
    _save(client, workspace, {"token": "old-tok", "enabled": True})
    _save(client, workspace, {"token": "new-tok", "enabled": True})

    db = SessionLocal()
    try:
        rows = db.query(ChatTokenIndex).filter(
            ChatTokenIndex.workspace_id == workspace["workspace_id"]
        ).all()
        assert len(rows) == 1
        assert token_index.resolve(db, "old-tok") is None
        ws = token_index.resolve(db, "new-tok")
        assert ws is not None and ws.id == workspace["workspace_id"]
    finally:
        db.rollback()
        db.close()


def test_disabled_token_does_not_resolve(client, workspace):
    _save(client, workspace, {"token": "off-tok", "enabled": False})
    db = SessionLocal()
    try:
        assert token_index.resolve(db, "off-tok") is None
    finally:
        db.rollback()
        db.close()


def test_inbound_valid_token_still_works(client, workspace):
    _save(client, workspace, {"provider": "telegram", "token": "bot-secret-1", "enabled": True})
    r = client.post(
        "/chat/inbound",
        json={"token": "bot-secret-1", "message": "写一段棉麻衬衫的营销文案"},
    )
    assert r.status_code == 200


def test_invalid_token_rejected(client, workspace):
    r = client.post("/chat/inbound", json={"token": "nope", "message": "hi"})
    assert r.status_code == 401
