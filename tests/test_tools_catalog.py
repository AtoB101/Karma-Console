"""Tests for the unified HTTP tool discovery catalog (GET /workspaces/{id}/tools).

Built-ins and this workspace's enabled functions are listed together, aligned
with the MCP surface; credentials are never advertised; other workspaces'
functions stay invisible.
"""
import uuid


def _make_workspace(client, name="目录店"):
    email = f"cat{uuid.uuid4().hex[:10]}@example.com"
    user_id = client.post("/users", json={"email": email, "display_name": "Owner"}).json()["id"]
    key = client.post(f"/users/{user_id}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws_id = client.post(
        "/workspaces",
        json={"name": name, "region": "CN", "business_category": "clothing"},
        headers=headers,
    ).json()["id"]
    headers["X-Workspace-Id"] = ws_id
    return {"headers": headers, "workspace_id": ws_id}


def test_catalog_lists_builtin(client, workspace):
    r = client.get(f"/workspaces/{workspace['workspace_id']}/tools", headers=workspace["headers"])
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    assert "list_products" in names
    item = next(t for t in r.json()["tools"] if t["name"] == "list_products")
    assert item["source"] == "builtin"
    assert item["risk_level"]


def test_catalog_includes_enabled_function_without_secrets(client, workspace):
    secret = f"cat-secret-{uuid.uuid4().hex}"
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/functions",
        json={
            "name": "cat_reader",
            "kind": "http_read",
            "endpoint_url": "https://example.com/data",
            "auth": {"type": "bearer", "token": secret},
            "headers": {"X-Token": "hush"},
        },
        headers=workspace["headers"],
    )
    assert r.status_code == 201, r.text

    r = client.get(f"/workspaces/{workspace['workspace_id']}/tools", headers=workspace["headers"])
    raw = r.text
    assert secret not in raw
    assert "hush" not in raw
    tools = r.json()["tools"]
    fn = next(t for t in tools if t["name"] == "cat_reader")
    assert fn["source"] == "function"
    assert fn["kind"] == "http_read"
    assert "auth" not in fn
    assert "headers" not in fn
    assert "endpoint_url" not in fn


def test_catalog_isolated_per_workspace(client):
    ws_a = _make_workspace(client, "A")
    ws_b = _make_workspace(client, "B")
    secret = f"iso-{uuid.uuid4().hex}"
    client.post(
        f"/workspaces/{ws_a['workspace_id']}/functions",
        json={"name": "a_private_tool", "kind": "http_read", "auth": {"type": "bearer", "token": secret}},
        headers=ws_a["headers"],
    )
    r = client.get(f"/workspaces/{ws_b['workspace_id']}/tools", headers=ws_b["headers"])
    raw = r.text
    assert "a_private_tool" not in raw
    assert secret not in raw
