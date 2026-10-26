"""P43 device pairing: request code, one-time authorize, revoke, isolation; server stores no master key."""
from __future__ import annotations

import uuid


def _make_ws(client):
    email = f"dev{uuid.uuid4().hex[:10]}@example.com"
    uid = client.post("/users", json={"email": email, "display_name": "Owner"}).json()["id"]
    key = client.post(f"/users/{uid}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws = client.post(
        "/workspaces", json={"name": "配对店", "region": "CN", "business_category": "general"}, headers=headers
    ).json()["id"]
    headers["X-Workspace-Id"] = ws
    return {"headers": headers, "workspace_id": ws}


def test_server_stores_no_master_key():
    from app.models import DevicePairing
    cols = {c.name for c in DevicePairing.__table__.columns}
    assert "master_key" not in cols
    assert "private_key" not in cols


def test_request_then_authorize_one_time(client):
    a = _make_ws(client)
    req = client.post(
        f"/workspaces/{a['workspace_id']}/devices/request",
        json={"device_name": "iPad"}, headers=a["headers"],
    )
    assert req.status_code == 201
    code = req.json()["pair_code"]
    assert len(code) == 6 and code.isdigit()

    auth = client.post(
        f"/workspaces/{a['workspace_id']}/devices/authorize",
        json={"pair_code": code, "device_name": "iPad", "credential_handle": "cred-1"},
        headers=a["headers"],
    )
    assert auth.status_code == 200
    assert auth.json()["status"] == "authorized"

    reused = client.post(
        f"/workspaces/{a['workspace_id']}/devices/authorize",
        json={"pair_code": code, "device_name": "iPad"}, headers=a["headers"],
    )
    assert reused.status_code == 409


def test_revoke_and_isolation(client):
    a = _make_ws(client)
    b = _make_ws(client)
    req = client.post(
        f"/workspaces/{a['workspace_id']}/devices/request",
        json={"device_name": "Phone"}, headers=a["headers"],
    ).json()
    client.post(
        f"/workspaces/{a['workspace_id']}/devices/authorize",
        json={"pair_code": req["pair_code"], "credential_handle": "c"}, headers=a["headers"],
    )
    devices = client.get(f"/workspaces/{a['workspace_id']}/devices", headers=a["headers"]).json()["devices"]
    target = next(d for d in devices if d["status"] == "authorized")

    rev = client.post(f"/workspaces/{a['workspace_id']}/devices/{target['id']}/revoke", headers=a["headers"])
    assert rev.status_code == 200 and rev.json()["status"] == "revoked"

    cross = client.post(f"/workspaces/{b['workspace_id']}/devices/{target['id']}/revoke", headers=b["headers"])
    assert cross.status_code == 404
    b_list = client.get(f"/workspaces/{b['workspace_id']}/devices", headers=b["headers"]).json()["devices"]
    assert b_list == []


def test_request_requires_device_name(client):
    a = _make_ws(client)
    r = client.post(f"/workspaces/{a['workspace_id']}/devices/request", json={}, headers=a["headers"])
    assert r.status_code == 400
