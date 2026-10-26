
def test_draft_copy_task_completes(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.post(f"/workspaces/{ws}/tasks", json={"goal": "写一段棉麻衬衫的营销文案"}, headers=h)
    assert r.status_code == 201
    task_id = r.json()["id"]

    r = client.post(f"/tasks/{task_id}/run", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "COMPLETED"
    assert any(s["tool"] == "generate_marketing_draft" for s in body["result"]["steps"])


def test_payment_task_requires_approval_then_completes(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.post(f"/workspaces/{ws}/connectors", json={"connector_type": "karma_payment", "name": "Karma"}, headers=h)

    r = client.post(f"/workspaces/{ws}/tasks", json={"goal": "支付 10 元", "steps": [{"tool": "make_payment", "params": {"amount": 10.0}}]}, headers=h)
    task_id = r.json()["id"]

    r = client.post(f"/tasks/{task_id}/run", headers=h)
    assert r.json()["status"] == "PENDING_APPROVAL"
    approval_id = r.json()["result"]["approval_id"]

    client.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)

    r = client.post(f"/tasks/{task_id}/run", headers=h)
    body = r.json()
    assert body["status"] == "COMPLETED"
    assert body["result"]["steps"][0]["result"]["receipt_status"] == "SUCCEEDED"


def test_task_resumes_without_repeating_completed_steps(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.post(f"/workspaces/{ws}/connectors", json={"connector_type": "karma_payment", "name": "Karma"}, headers=h)

    steps = [
        {"tool": "list_products", "params": {}},
        {"tool": "make_payment", "params": {"amount": 10.0}},
    ]
    r = client.post(f"/workspaces/{ws}/tasks", json={"goal": "先查商品再支付", "steps": steps}, headers=h)
    task_id = r.json()["id"]

    first = client.post(f"/tasks/{task_id}/run", headers=h).json()
    assert first["status"] == "PENDING_APPROVAL"
    first_execution = first["result"]["steps"][0]["execution_id"]

    client.post(f"/approvals/{first['result']['approval_id']}/decision", json={"approve": True}, headers=h)
    second = client.post(f"/tasks/{task_id}/run", headers=h).json()

    assert second["status"] == "COMPLETED"
    assert len(second["result"]["steps"]) == 2
    assert second["result"]["steps"][0]["execution_id"] == first_execution


def test_stale_task_recovers_and_resumes(client, workspace, monkeypatch):
    from datetime import timedelta

    from app.workflow import _is_stale
    from app.models import Task, TaskStatus, utcnow

    task = Task(
        workspace_id=workspace["workspace_id"],
        actor_id=workspace["user_id"],
        intent_id="intent-x",
        status=TaskStatus.EXECUTING.value,
        plan=[{"tool": "list_products", "params": {}}],
        result={},
    )
    task.updated_at = utcnow() - timedelta(seconds=99999)
    assert _is_stale(task) is True

    task.status = TaskStatus.COMPLETED.value
    assert _is_stale(task) is False


def test_verification_failure_compensates_cancelling_order(client, workspace, monkeypatch):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.post(
        f"/workspaces/{ws}/connectors",
        json={
            "connector_type": "ecommerce_independent",
            "name": "独立站",
            "config": {
                "endpoint_url": "https://shop.example.com/orders",
                "method": "POST",
                "verification": {"status_path": "status", "reference_path": "order_id"},
            },
        },
        headers=h,
    )

    async def fake_request(method, url, *, headers=None, json_body=None, params=None, timeout=None):
        return {"status": 200, "headers": {}, "json": {"status": "FAILED", "order_id": "ord-fail"}, "text": ""}

    monkeypatch.setattr("app.functions.request_http", fake_request)

    r = client.post(
        f"/workspaces/{ws}/tasks",
        json={"goal": "创建订单", "steps": [{"tool": "create_order", "params": {"channel": "ecommerce_independent"}}]},
        headers=h,
    )
    task_id = r.json()["id"]

    body = client.post(f"/tasks/{task_id}/run", headers=h).json()
    assert body["status"] == "UNKNOWN"
    comp = body["result"]["compensation"]
    assert comp["status"] == "DONE"
    assert any(item["tool"] == "create_order" and item["status"] == "DONE" for item in comp["items"])


def test_payment_is_compensated_with_refund_after_later_block(client, workspace):
    from app.db import SessionLocal
    from app.models import RegionPolicyRule

    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.post(f"/workspaces/{ws}/connectors", json={"connector_type": "karma_payment", "name": "Karma"}, headers=h)

    db = SessionLocal()
    rule = RegionPolicyRule(
        region="CN",
        rule_type="block",
        config={"actions": ["bulk_update_prices"]},
        note="test: block bulk update in CN",
    )
    db.add(rule)
    db.commit()
    try:
        steps = [
            {"tool": "make_payment", "params": {"amount": 10.0}},
            {"tool": "bulk_update_prices", "params": {"delta": 1.0}},
        ]
        task_id = client.post(
            f"/workspaces/{ws}/tasks", json={"goal": "支付后改价", "steps": steps}, headers=h
        ).json()["id"]

        first = client.post(f"/tasks/{task_id}/run", headers=h).json()
        assert first["status"] == "PENDING_APPROVAL"
        client.post(f"/approvals/{first['result']['approval_id']}/decision", json={"approve": True}, headers=h)

        second = client.post(f"/tasks/{task_id}/run", headers=h).json()
        assert second["status"] == "PENDING_APPROVAL"
        comp = second["result"]["compensation"]
        assert comp["status"] == "PENDING"
        approval_id = comp["pending"][0]["approval_id"]
        client.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)

        final = client.post(f"/tasks/{task_id}/run", headers=h).json()
        assert final["status"] == "BLOCKED"
        comp = final["result"]["compensation"]
        assert comp["status"] == "DONE"
        assert any(item["tool"] == "make_payment" and item["status"] == "DONE" for item in comp["items"])
    finally:
        db.delete(rule)
        db.commit()
        db.close()
