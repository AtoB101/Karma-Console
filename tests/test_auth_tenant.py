import uuid


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_workspace_requires_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    r = client.post("/workspaces", json={"name": "x", "region": "CN"})
    assert r.status_code == 401


def test_cross_tenant_access_denied(client, workspace):
    other = client.post("/users", json={"email": f"o{uuid.uuid4().hex[:8]}@x.com"}).json()
    r = client.post(f"/users/{other['id']}/api-keys")
    key = r.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}", "X-Workspace-Id": workspace["workspace_id"]}
    r = client.get(f"/workspaces/{workspace['workspace_id']}/products", headers=headers)
    assert r.status_code == 403
