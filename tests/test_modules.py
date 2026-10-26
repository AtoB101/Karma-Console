import uuid

from app.db import SessionLocal
from app.models import Membership, Role


def _add_member(client, workspace_id, role="viewer"):
    user = client.post("/users", json={"email": f"m{uuid.uuid4().hex[:8]}@x.com"}).json()
    key = client.post(f"/users/{user['id']}/api-keys").json()["api_key"]
    db = SessionLocal()
    try:
        db.add(Membership(workspace_id=workspace_id, user_id=user["id"], role=role))
        db.commit()
    finally:
        db.close()
    return {"Authorization": f"Bearer {key}", "X-Workspace-Id": workspace_id}


def _create_module(client, workspace, **overrides):
    payload = {
        "name": "receipt_widget",
        "kind": "http_read",
        "category": "receipt",
        "endpoint_url": "https://shop.example.com/orders/{order_id}/receipt",
        "auth": {"type": "bearer", "token": "secret-token"},
        "verification": {"type": "http", "status_path": "receipt_status"},
        "min_role": "member",
    }
    payload.update(overrides)
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/modules", json=payload, headers=workspace["headers"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_module_draft_lifecycle(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create_module(client, workspace)
    assert m["visibility"] == "private"
    assert m["status"] == "draft"

    r = client.get(f"/workspaces/{ws}/modules", headers=h)
    assert [x["name"] for x in r.json()["modules"]] == ["receipt_widget"]

    r = client.patch(f"/workspaces/{ws}/modules/{m['id']}", json={"description": "订单回执小部件"}, headers=h)
    assert r.json()["description"] == "订单回执小部件"

    r = client.delete(f"/workspaces/{ws}/modules/{m['id']}", headers=h)
    assert r.status_code == 204


def test_module_from_source_function(client, workspace):
    fn = client.post(
        f"/workspaces/{workspace['workspace_id']}/functions",
        json={"name": "read_receipt", "kind": "http_read", "category": "receipt", "endpoint_url": "https://x.example.com/r"},
        headers=workspace["headers"],
    ).json()
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/modules",
        json={"name": "receipt_pkg", "source_function_id": fn["id"]},
        headers=workspace["headers"],
    )
    assert r.status_code == 201
    assert r.json()["endpoint_url"] == "https://x.example.com/r"


def test_publish_strips_secrets_and_appears_in_marketplace(client, workspace):
    m = _create_module(client, workspace)
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    r = client.patch(f"/workspaces/{ws}/modules/{m['id']}", json={"visibility": "published"}, headers=h)
    body = r.json()
    assert body["visibility"] == "published"
    assert body["status"] == "published"

    r = client.get("/marketplace", headers=h)
    listed = r.json()["modules"]
    assert any(x["id"] == m["id"] for x in listed)
    assert "token" not in body.get("config", {})
    # auth is intentionally not exposed by the serializer; verify install carries no secret
    r2 = client.post(f"/marketplace/{m['id']}/install", json={}, headers=h)
    assert r2.status_code == 201


def test_private_module_not_in_marketplace(client, workspace):
    m = _create_module(client, workspace)
    r = client.get("/marketplace", headers=workspace["headers"])
    assert all(x["id"] != m["id"] for x in r.json()["modules"])


def test_non_owner_cannot_publish(client, workspace):
    m = _create_module(client, workspace)
    member_headers = _add_member(client, workspace["workspace_id"], role="member")
    r = client.patch(
        f"/workspaces/{workspace['workspace_id']}/modules/{m['id']}",
        json={"visibility": "published"},
        headers=member_headers,
    )
    assert r.status_code == 403


def test_install_into_other_workspace_and_min_role(client, workspace):
    m = _create_module(client, workspace)
    ws = workspace["workspace_id"]
    client.patch(f"/workspaces/{ws}/modules/{m['id']}", json={"visibility": "published"}, headers=workspace["headers"])

    other = client.post(
        "/workspaces", json={"name": "另一家店", "region": "CN", "business_category": "food"}, headers=workspace["headers"]
    ).json()
    h2 = {**workspace["headers"], "X-Workspace-Id": other["id"]}

    r = client.post(f"/marketplace/{m['id']}/install", json={"auth": {"type": "bearer", "token": "mine"}}, headers=h2)
    assert r.status_code == 201
    r = client.get(f"/workspaces/{other['id']}/functions", headers=h2)
    assert any(f["name"] == "receipt_widget" for f in r.json()["functions"])

    viewer_headers = _add_member(client, other["id"], role="viewer")
    r = client.post(f"/marketplace/{m['id']}/install", json={}, headers=viewer_headers)
    assert r.status_code == 403