"""P3 module review state machine: draft -> pending_review -> approved/rejected
-> published, content fingerprint tamper check, owner gate, marketplace gating."""
import uuid


def _add_member(client, workspace_id, role="member"):
    user = client.post("/users", json={"email": f"r{uuid.uuid4().hex[:8]}@x.com"}).json()
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


def _create(client, workspace, **overrides):
    payload = {
        "name": "review_widget",
        "kind": "http_read",
        "category": "receipt",
        "endpoint_url": "https://shop.example.com/r",
        "auth": {"type": "bearer", "token": "secret"},
        "min_role": "member",
    }
    payload.update(overrides)
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/modules", json=payload, headers=workspace["headers"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_submit_review_approve_publish_flow(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create(client, workspace)

    r = client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["config"]["content_hash"]

    r = client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review",
        json={"decision": "approve", "note": "looks good"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    r = client.post(f"/workspaces/{ws}/modules/{m['id']}/publish", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert r.json()["visibility"] == "published"

    listed = client.get("/marketplace", headers=h).json()["modules"]
    assert any(x["id"] == m["id"] for x in listed)


def test_reject_then_resubmit(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create(client, workspace)
    client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)

    r = client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review",
        json={"decision": "reject", "note": "endpoint unverified"},
        headers=h,
    )
    assert r.json()["status"] == "rejected"
    assert r.json()["config"]["review_note"] == "endpoint unverified"

    r = client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)
    assert r.json()["status"] == "pending_review"


def test_changed_after_submit_blocked_on_review(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create(client, workspace)
    client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)
    # change configuration after submission -> fingerprint mismatch
    client.patch(
        f"/workspaces/{ws}/modules/{m['id']}",
        json={"endpoint_url": "https://evil.example.com/x"},
        headers=h,
    )
    r = client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review",
        json={"decision": "approve"},
        headers=h,
    )
    assert r.status_code == 409
    assert "resubmit" in r.json()["detail"]


def test_approved_not_published_not_in_marketplace(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create(client, workspace)
    client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)
    client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review", json={"decision": "approve"}, headers=h
    )
    listed = client.get("/marketplace", headers=h).json()["modules"]
    assert all(x["id"] != m["id"] for x in listed)


def test_non_owner_cannot_submit_or_review(client, workspace):
    ws = workspace["workspace_id"]
    m = _create(client, workspace)
    member = _add_member(client, ws, role="member")

    r = client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=member)
    assert r.status_code == 403

    client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=workspace["headers"])
    r = client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review",
        json={"decision": "approve"},
        headers=member,
    )
    assert r.status_code == 403


def test_review_invalid_decision_rejected(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    m = _create(client, workspace)
    client.post(f"/workspaces/{ws}/modules/{m['id']}/submit-review", headers=h)
    r = client.post(
        f"/workspaces/{ws}/modules/{m['id']}/review",
        json={"decision": "maybe"},
        headers=h,
    )
    assert r.status_code == 409
