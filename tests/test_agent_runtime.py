"""P11 autonomous manager run: read-only patrol, allowlist + limits, memory, audit."""
from app.db import SessionLocal
from app.models import AgentMemory, Product


def _create(client, workspace, **overrides):
    payload = {
        "name": "巡检专员",
        "role": "inspector",
        "description": "只读巡检",
        "responsibilities": ["盘点"],
        "allowed_tools": ["list_products", "business_overview"],
        "status": "live",
    }
    payload.update(overrides)
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/agents",
        json=payload,
        headers=workspace["headers"],
    )


def test_live_agent_patrols_and_writes_memory(client, workspace):
    agent = _create(client, workspace).json()
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agents/{agent['id']}/run",
        json={}, headers=workspace["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["call_count"] >= 1
    assert body["allowed_count"] >= 1
    assert body["memory_key"] == f"agent:{agent['id']}:last_report"

    db = SessionLocal()
    try:
        row = db.execute(
            AgentMemory.__table__.select().where(
                AgentMemory.workspace_id == workspace["workspace_id"],
                AgentMemory.key == body["memory_key"],
            )
        ).first()
        assert row is not None
    finally:
        db.rollback(); db.close()


def test_write_tool_in_allowlist_is_not_auto_executed(client, workspace):
    agent = _create(
        client, workspace,
        allowed_tools=["create_product_draft", "list_products"],
    ).json()
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agents/{agent['id']}/run",
        json={}, headers=workspace["headers"],
    )
    assert r.status_code == 200
    tools_called = [c["tool"] for c in r.json()["calls"]]
    assert "list_products" in tools_called
    assert "create_product_draft" not in tools_called

    db = SessionLocal()
    try:
        count = db.query(Product).filter(
            Product.workspace_id == workspace["workspace_id"]
        ).count()
        assert count == 0  # no draft created by the patrol
    finally:
        db.rollback(); db.close()


def test_draft_agent_cannot_run(client, workspace):
    agent = _create(client, workspace, status="draft").json()
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agents/{agent['id']}/run",
        json={}, headers=workspace["headers"],
    )
    assert r.status_code == 409


def test_explicit_step_outside_allowlist_denied(client, workspace):
    agent = _create(client, workspace, allowed_tools=["list_products"]).json()
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agents/{agent['id']}/run",
        json={"steps": [{"tool": "list_orders", "params": {}}]},
        headers=workspace["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["call_count"] == 0
    assert body["denied"] and body["denied"][0]["tool"] == "list_orders"


def test_unknown_agent_404(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/agents/nonexistent/run",
        json={}, headers=workspace["headers"],
    )
    assert r.status_code == 404
