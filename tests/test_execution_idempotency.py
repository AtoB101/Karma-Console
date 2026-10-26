"""P8 execution-side idempotent replay: a reused key replays the prior
SUCCEEDED result instead of re-running the side effect."""
from app.db import SessionLocal
from app.models import ApprovalRequest, ApprovalStatus, Execution, ExecutionStatus


def _exec(client, headers, tool, params=None, key=None):
    body = {"tool": tool, "params": params or {}}
    if key:
        body["idempotency_key"] = key
    return client.post("/gateway/execute", json=body, headers=headers)


def test_reused_key_replays_success(client, workspace):
    h = workspace["headers"]
    first = _exec(client, h, "create_product_draft", {"name": "复盘甲", "price": 10}, key="draft-1")
    assert first.json()["decision"] == "ALLOWED"

    second = _exec(client, h, "create_product_draft", {"name": "复盘甲", "price": 10}, key="draft-1")
    assert second.json()["decision"] == "ALLOWED"
    assert second.json()["reason"] == "idempotent replay"

    db = SessionLocal()
    try:
        rows = db.query(Execution).filter(
            Execution.workspace_id == workspace["workspace_id"],
            Execution.idempotency_key == "draft-1",
            Execution.status == ExecutionStatus.SUCCEEDED.value,
        ).all()
        assert len(rows) == 1
    finally:
        db.rollback()
        db.close()


def test_different_keys_execute_separately(client, workspace):
    h = workspace["headers"]
    a = _exec(client, h, "list_products", {}, key="k-a")
    b = _exec(client, h, "list_products", {}, key="k-b")
    assert a.json()["decision"] == "ALLOWED"
    assert b.json()["decision"] == "ALLOWED"
    assert a.json().get("reason") != "idempotent replay"
    assert b.json().get("reason") != "idempotent replay"


def test_payment_approved_then_replayed_not_charged_twice(client, workspace):
    h = workspace["headers"]
    client.post(
        f"/workspaces/{workspace['workspace_id']}/connectors",
        json={"connector_type": "karma_payment", "name": "Karma 收款"},
        headers=h,
    )
    pend = _exec(client, h, "make_payment", {"amount": 10.0}, key="pay-x")
    approval_id = pend.json()["approval_id"]
    client.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)

    once = _exec(client, h, "make_payment", {"amount": 10.0}, key="pay-x")
    assert once.json()["decision"] == "ALLOWED"

    replay = _exec(client, h, "make_payment", {"amount": 10.0}, key="pay-x")
    assert replay.json()["decision"] == "ALLOWED"
    assert replay.json()["reason"] == "idempotent replay"

    db = SessionLocal()
    try:
        rows = db.query(Execution).filter(
            Execution.workspace_id == workspace["workspace_id"],
            Execution.idempotency_key == "pay-x",
            Execution.status == ExecutionStatus.SUCCEEDED.value,
        ).all()
        assert len(rows) == 1
    finally:
        db.rollback()
        db.close()


def test_pending_same_key_reuses_approval_no_duplicate(client, workspace):
    h = workspace["headers"]
    client.post(
        f"/workspaces/{workspace['workspace_id']}/connectors",
        json={"connector_type": "karma_payment", "name": "Karma 收款"},
        headers=h,
    )
    first = _exec(client, h, "make_payment", {"amount": 10.0}, key="pay-pending")
    second = _exec(client, h, "make_payment", {"amount": 10.0}, key="pay-pending")

    assert first.json()["decision"] == "PENDING"
    assert second.json()["decision"] == "PENDING"
    # reused approval, not a fresh one
    assert second.json()["approval_id"] == first.json()["approval_id"]

    db = SessionLocal()
    try:
        pending = db.query(ApprovalRequest).filter(
            ApprovalRequest.workspace_id == workspace["workspace_id"],
            ApprovalRequest.idempotency_key == "pay-pending",
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        ).all()
        assert len(pending) == 1
    finally:
        db.rollback()
        db.close()


def test_burst_same_key_executes_once(client, workspace):
    h = workspace["headers"]
    key = "burst-draft"
    responses = [
        _exec(client, h, "create_product_draft", {"name": "连发商品", "price": 7}, key=key)
        for _ in range(5)
    ]
    decisions = [r.json()["decision"] for r in responses]
    assert all(d == "ALLOWED" for d in decisions)
    # first call runs; the remaining 4 replay the prior SUCCEEDED result
    reasons = [r.json().get("reason") for r in responses]
    assert reasons[0] != "idempotent replay"
    assert reasons[1:] == ["idempotent replay"] * 4

    db = SessionLocal()
    try:
        rows = db.query(Execution).filter(
            Execution.workspace_id == workspace["workspace_id"],
            Execution.idempotency_key == key,
            Execution.status == ExecutionStatus.SUCCEEDED.value,
        ).all()
        assert len(rows) == 1
    finally:
        db.rollback()
        db.close()
