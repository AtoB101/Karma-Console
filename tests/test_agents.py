"""Persistent AI manager (chapter 5) CRUD, isolation and runtime allowlist tests."""
import json
import uuid

import app.multiagent as ma


def _create_agent(client, workspace, **overrides):
    payload = {
        "name": "上架专员",
        "role": "listing",
        "description": "负责商品上架与归类",
        "responsibilities": ["建档", "归类"],
        "allowed_tools": ["create_product_draft", "list_products"],
        "status": "live",
    }
    payload.update(overrides)
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/agents",
        json=payload,
        headers=workspace["headers"],
    )


def test_agent_crud_lifecycle(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    r = _create_agent(client, workspace)
    assert r.status_code == 201
    agent = r.json()
    assert agent["name"] == "上架专员"
    assert agent["role"] == "listing"
    assert agent["status"] == "live"
    assert agent["allowed_tools"] == ["create_product_draft", "list_products"]

    lst = client.get(f"/workspaces/{ws}/agents", headers=h).json()
    assert any(a["id"] == agent["id"] for a in lst["agents"])

    assert client.get(f"/workspaces/{ws}/agents/{agent['id']}", headers=h).status_code == 200

    patch = client.patch(
        f"/workspaces/{ws}/agents/{agent['id']}",
        json={"status": "test", "allowed_tools": ["list_products"]},
        headers=h,
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "test"
    assert patch.json()["allowed_tools"] == ["list_products"]

    assert client.delete(f"/workspaces/{ws}/agents/{agent['id']}", headers=h).status_code == 204
    assert client.get(f"/workspaces/{ws}/agents/{agent['id']}", headers=h).status_code == 404


def test_agent_validation_rejects_unknown_tool_and_status(client, workspace):
    r = _create_agent(client, workspace, allowed_tools=["steal_money"])
    assert r.status_code == 400
    assert "unknown tool" in r.json()["detail"]

    r = _create_agent(client, workspace, status="exploded")
    assert r.status_code == 400


def test_agent_tenant_isolation(client, workspace):
    agent = _create_agent(client, workspace).json()

    email2 = f"o{uuid.uuid4().hex[:8]}@example.com"
    u2 = client.post("/users", json={"email": email2, "display_name": "Other"}).json()
    key2 = client.post(f"/users/{u2['id']}/api-keys").json()["api_key"]
    other_h = {"Authorization": f"Bearer {key2}", "X-Workspace-Id": workspace["workspace_id"]}

    # not a member of this workspace -> 403, agent never exposed
    assert client.get(f"/workspaces/{workspace['workspace_id']}/agents/{agent['id']}", headers=other_h).status_code == 403


def test_agent_allowlist_constrains_multiagent_execution(client, workspace, monkeypatch):
    _create_agent(client, workspace)  # role=listing, allowed_tools=[create_product_draft, list_products]

    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        if "编排器" in system:
            return json.dumps({"subtasks": [{"role": "listing", "instruction": "给商品加库存"}], "summary": "ok"}, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "add_stock", "params": {"quantity": 5}}], "reply": "ok", "done": True}, ensure_ascii=False)

    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我上架并给商品加库存"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    denied = [a for a in body["actions"] if a["tool"] == "add_stock"]
    assert denied and denied[0]["decision"] == "DENIED"
    assert "add_stock" in denied[0]["result"]["error"]
