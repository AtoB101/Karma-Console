"""P41 virtual-number mapping placeholder: allocation, bind, release, isolation."""
from __future__ import annotations

import uuid


def _make_ws(client):
    email = f"vn{uuid.uuid4().hex[:10]}@example.com"
    uid = client.post("/users", json={"email": email, "display_name": "Owner"}).json()["id"]
    key = client.post(f"/users/{uid}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws = client.post(
        "/workspaces", json={"name": "虚拟号店", "region": "CN", "business_category": "general"}, headers=headers
    ).json()["id"]
    headers["X-Workspace-Id"] = ws
    return {"headers": headers, "workspace_id": ws}


def _make_customer(ws_id, name="客户甲"):
    from app.db import SessionLocal
    from app.models import Customer
    db = SessionLocal()
    try:
        c = Customer(workspace_id=ws_id, channel="chat", external_id=f"ext-{uuid.uuid4().hex[:6]}", name=name)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
    finally:
        db.close()


def test_allocate_sequential_virtual_numbers(client):
    a = _make_ws(client)
    r1 = client.post(f"/workspaces/{a['workspace_id']}/virtual-numbers", json={}, headers=a["headers"])
    r2 = client.post(f"/workspaces/{a['workspace_id']}/virtual-numbers", json={}, headers=a["headers"])
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["virtual_number"] == "#8001"
    assert r2.json()["virtual_number"] == "#8002"
    # list never returns the encrypted real number
    body = client.get(f"/workspaces/{a['workspace_id']}/virtual-numbers", headers=a["headers"]).json()
    nums = [n["virtual_number"] for n in body["numbers"]]
    assert "#8001" in nums and "#8002" in nums
    assert all("real_encrypted" not in n for n in body["numbers"])


def test_bind_then_release_customer(client):
    a = _make_ws(client)
    cid = _make_customer(a["workspace_id"])
    alloc = client.post(f"/workspaces/{a['workspace_id']}/virtual-numbers", json={}, headers=a["headers"]).json()

    bound = client.post(
        f"/workspaces/{a['workspace_id']}/virtual-numbers/bind",
        json={"virtual_number": alloc["virtual_number"], "customer_id": cid, "real_encrypted": "cipher-placeholder"},
        headers=a["headers"],
    )
    assert bound.status_code == 200
    assert bound.json()["status"] == "bound"
    assert bound.json()["has_real"] is True

    rel = client.post(
        f"/workspaces/{a['workspace_id']}/virtual-numbers/release",
        json={"virtual_number": alloc["virtual_number"]},
        headers=a["headers"],
    )
    assert rel.status_code == 200
    assert rel.json()["status"] == "available"


def test_bind_rejects_unknown_customer(client):
    a = _make_ws(client)
    alloc = client.post(f"/workspaces/{a['workspace_id']}/virtual-numbers", json={}, headers=a["headers"]).json()
    bad = client.post(
        f"/workspaces/{a['workspace_id']}/virtual-numbers/bind",
        json={"virtual_number": alloc["virtual_number"], "customer_id": "no-such-customer"},
        headers=a["headers"],
    )
    assert bad.status_code == 409


def test_virtual_numbers_isolated_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)
    client.post(f"/workspaces/{a['workspace_id']}/virtual-numbers", json={}, headers=a["headers"])
    b_list = client.get(f"/workspaces/{b['workspace_id']}/virtual-numbers", headers=b["headers"]).json()
    assert b_list["numbers"] == []
