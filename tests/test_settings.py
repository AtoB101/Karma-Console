import uuid

import app.llm as llm


def test_settings_save_and_mask(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://x.example.com/v1", "api_key": "sk-abcdef123456", "model": "m1"},
              "chat": {"provider": "telegram", "token": "1234567890:bot-token", "enabled": True}},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"]["api_key"] == "sk-a...3456"
    assert body["model"]["model"] == "m1"
    assert body["chat"]["token"] == "1234...oken"
    assert body["chat"]["enabled"] is True


def test_workspace_model_config_runtime(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://x.example.com/v1", "api_key": "sk-test-key", "model": "gpt-x"}},
        headers=h,
    )
    from app.db import SessionLocal
    from app.llm import workspace_model_config

    db = SessionLocal()
    try:
        cfg = workspace_model_config(db, ws)
    finally:
        db.close()
    assert cfg["api_key"] == "sk-test-key"
    assert cfg["model"] == "gpt-x"


def test_generate_uses_model_config(monkeypatch):
    captured = {}

    class FakeProvider:
        def __init__(self, base_url, api_key, model):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["model"] = model

        def generate(self, prompt, context=""):
            return "fake-output"

    monkeypatch.setattr(llm, "OpenAICompatProvider", FakeProvider)
    out = llm.generate("p", "c", model_config={"base_url": "https://b.example.com", "api_key": "k", "model": "mm"})
    assert out == "fake-output"
    assert captured["api_key"] == "k"
    assert captured["model"] == "mm"


def test_chat_inbound_with_valid_token(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"chat": {"provider": "telegram", "token": "bot-secret-1", "enabled": True}},
        headers=h,
    )
    r = client.post("/chat/inbound", json={"token": "bot-secret-1", "message": "写一段棉麻衬衫的营销文案"})
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == ws
    assert "mock" in body["reply"]


def test_chat_inbound_invalid_or_disabled_token(client, workspace):
    r = client.post("/chat/inbound", json={"token": "nope", "message": "hi"})
    assert r.status_code == 401

    ws = workspace["workspace_id"]
    h = workspace["headers"]
    client.put(f"/workspaces/{ws}/settings", json={"chat": {"provider": "telegram", "token": "bot-2", "enabled": False}}, headers=h)
    r = client.post("/chat/inbound", json={"token": "bot-2", "message": "hi"})
    assert r.status_code == 401
def test_settings_versioning_and_rollback(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://a.example.com", "api_key": "sk-first", "model": "m1"}},
        headers=h,
    )
    # second update creates version history for the first state
    r = client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://b.example.com", "api_key": "sk-second", "model": "m2"}},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["model"]["model"] == "m2"

    hist = client.get(f"/workspaces/{ws}/settings/history", headers=h).json()
    versions = [v["version"] for v in hist["versions"]]
    assert len(versions) >= 2
    assert versions[0] > versions[1]

    # rollback to the snapshot that captured the m1 state (newest snapshot)
    r = client.post(f"/workspaces/{ws}/settings/rollback/{versions[0]}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["model"]["model"] == "m1"
    assert body["model"]["api_key"]  # restored, masked

    # rollback is itself versioned: a newer snapshot now holds the m2 state
    hist_after = client.get(f"/workspaces/{ws}/settings/history", headers=h).json()
    assert len(hist_after["versions"]) >= len(versions) + 1


def test_settings_rollback_requires_owner(client, workspace):
    ws = workspace["workspace_id"]
    owner_h = workspace["headers"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "model": "m1", "api_key": "sk-x"}},
        headers=owner_h,
    )
    hist = client.get(f"/workspaces/{ws}/settings/history", headers=owner_h).json()
    version = hist["versions"][-1]["version"]

    # create a member (not owner) via direct membership insert
    from app.db import SessionLocal
    from app.models import Membership, Role

    r = client.post("/users", json={"email": f"member_{uuid.uuid4().hex[:8]}@example.com", "display_name": "Member"})
    member_id = r.json()["id"]
    r = client.post(f"/users/{member_id}/api-keys")
    member_key = r.json()["api_key"]
    member_h = {"Authorization": f"Bearer {member_key}", "X-Workspace-Id": ws}
    db = SessionLocal()
    try:
        db.add(Membership(workspace_id=ws, user_id=member_id, role=Role.MEMBER.value))
        db.commit()
    finally:
        db.close()

    r = client.post(f"/workspaces/{ws}/settings/rollback/{version}", headers=member_h)
    assert r.status_code == 403
