"""P4 module configuration isolation: whitelist kinds, SSRF guard, no code-exec
fields, strict input schema, http_read GET-only. Reject at create and install."""
import uuid


def _add_member(client, workspace_id, role="member"):
    user = client.post("/users", json={"email": f"s{uuid.uuid4().hex[:8]}@x.com"}).json()
    key = client.post(f"/users/{user['id']}/api-keys").json()["api_key"]
    from app.db import SessionLocal
    from app.models import Membership
    db = SessionLocal()
    try:
        db.add(Membership(workspace_id=workspace_id, user_id=user["id"], role=role))
        db.commit()
    finally:
        db.close()
    return {"Authorization": f"Bearer {key}", "X-Workspace-Id": workspace_id}


def _post(client, workspace, payload):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/modules",
        json=payload,
        headers=workspace["headers"],
    )


_BASE = {
    "name": "sandbox_widget",
    "kind": "http_read",
    "category": "receipt",
    "endpoint_url": "https://shop.example.com/r",
    "min_role": "member",
}


def test_unknown_kind_rejected(client, workspace):
    payload = {**_BASE, "name": "bad_kind", "kind": "arbitrary_code"}
    r = _post(client, workspace, payload)
    assert r.status_code == 400
    assert "kind" in r.json()["detail"]


def test_code_exec_field_rejected(client, workspace):
    payload = {**_BASE, "name": "code_field", "config": {"code": "os.system('id')"}}
    r = _post(client, workspace, payload)
    assert r.status_code == 400
    assert "execution field" in r.json()["detail"]


def test_nested_shell_field_rejected(client, workspace):
    payload = {
        **_BASE,
        "name": "nested_shell",
        "auth": {"type": "bearer", "nested": {"shell": "rm -rf /"}},
    }
    r = _post(client, workspace, payload)
    assert r.status_code == 400


def test_loose_input_schema_rejected(client, workspace):
    payload = {
        **_BASE,
        "name": "loose_schema",
        "input_schema": {"type": "object", "$schema": "http://x", "format": "evil"},
    }
    r = _post(client, workspace, payload)
    assert r.status_code == 400


def test_http_read_post_rejected(client, workspace):
    payload = {**_BASE, "name": "read_post", "method": "POST"}
    r = _post(client, workspace, payload)
    assert r.status_code == 400


def test_private_url_blocked_ssrf(client, workspace):
    payload = {**_BASE, "name": "ssrf", "endpoint_url": "http://127.0.0.1:8080/admin"}
    r = _post(client, workspace, payload)
    assert r.status_code == 400
    assert "SSRF" in r.json()["detail"] or "private" in r.json()["detail"]


def test_legit_module_still_created(client, workspace):
    r = _post(client, workspace, _BASE)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"


def test_patch_to_illegal_config_rejected(client, workspace):
    r = _post(client, workspace, _BASE)
    mid = r.json()["id"]
    ws = workspace["workspace_id"]
    resp = client.patch(
        f"/workspaces/{ws}/modules/{mid}",
        json={"config": {"script": "while(1){}"}},
        headers=workspace["headers"],
    )
    assert resp.status_code == 400
