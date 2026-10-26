"""Owner triage: todo creation, dashboard counts, code-confirmed approvals."""
from app.models import ConnectorType


def _gw(client, headers, tool, params):
    return client.post("/gateway/execute", json={"tool": tool, "params": params}, headers=headers).json()


def test_create_todo_tool_and_list(client, workspace):
    h = workspace["headers"]
    r = _gw(client, h, "create_todo", {"kind": "needs_reply", "title": "请确认是否补货", "detail": "牛仔裤库存低于 5"})
    assert r["decision"] == "ALLOWED"
    assert r["result"]["kind"] == "needs_reply"

    r2 = _gw(client, h, "create_todo", {"kind": "info", "title": "日报已生成"})
    assert r2["result"]["kind"] == "info"

    data = client.get(f"/workspaces/{workspace['workspace_id']}/todos?status=pending", headers=h).json()
    titles = {t["title"] for t in data["todos"]}
    assert "请确认是否补货" in titles
    assert "日报已生成" in titles


def test_dashboard_counts(client, workspace):
    h = workspace["headers"]
    _gw(client, h, "create_todo", {"kind": "needs_reply", "title": "a"})
    _gw(client, h, "create_todo", {"kind": "info", "title": "b"})
    dash = client.get(f"/workspaces/{workspace['workspace_id']}/dashboard", headers=h).json()
    assert dash["todos"]["needs_reply"] >= 1
    assert dash["todos"]["info"] >= 1


def test_must_confirm_code_approval_flow(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _gw(client, h, "make_payment", {"amount": 10.0, "currency": "CNY"})
    assert r["decision"] == "PENDING"
    approval_id = r["approval_id"]
    assert approval_id

    todos = client.get(f"/workspaces/{ws}/todos?status=pending", headers=h).json()["todos"]
    must = [t for t in todos if t["kind"] == "must_confirm" and t["ref_type"] == "approval" and t["ref_id"] == approval_id]
    assert must, "a must_confirm todo should be linked to the pending approval"
    todo = must[0]
    assert todo["confirm_code"].isdigit() and len(todo["confirm_code"]) == 6

    wrong = client.post(f"/workspaces/{ws}/todos/{todo['id']}/confirm", json={"code": "000000"}, headers=h)
    assert wrong.status_code == 400

    ok = client.post(f"/workspaces/{ws}/todos/{todo['id']}/confirm", json={"code": todo["confirm_code"]}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"

    approvals = client.get(f"/workspaces/{ws}/approvals", headers=h).json()["approvals"]
    assert any(a["id"] == approval_id and a["status"] == "APPROVED" for a in approvals)


def test_console_approve_syncs_linked_todo(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _gw(client, h, "make_payment", {"amount": 20.0, "currency": "CNY"})
    approval_id = r["approval_id"]
    decision = client.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)
    assert decision.status_code == 200
    todo = client.get(f"/workspaces/{ws}/todos?status=done", headers=h).json()["todos"]
    assert any(t["ref_type"] == "approval" and t["ref_id"] == approval_id for t in todo)


def test_console_reject_syncs_linked_todo(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _gw(client, h, "make_payment", {"amount": 30.0, "currency": "CNY"})
    approval_id = r["approval_id"]
    decision = client.post(f"/approvals/{approval_id}/decision", json={"approve": False}, headers=h)
    assert decision.status_code == 200
    todo = client.get(f"/workspaces/{ws}/todos?status=cancelled", headers=h).json()["todos"]
    assert any(t["ref_type"] == "approval" and t["ref_id"] == approval_id for t in todo)


def test_todo_status_marks_done(client, workspace):
    h = workspace["headers"]
    todo = _gw(client, h, "create_todo", {"kind": "info", "title": "通知"})["result"]
    done = client.post(
        f"/workspaces/{workspace['workspace_id']}/todos/{todo['todo_id']}/status",
        json={"status": "done"},
        headers=h,
    )
    assert done.status_code == 200
    assert done.json()["status"] == "done"


def test_super_app_connector_taxonomy_installed(client, workspace):
    ws = workspace["workspace_id"]
    types = {
        row["connector_type"]
        for row in client.get(f"/workspaces/{ws}/connectors", headers=workspace["headers"]).json()["connectors"]
    }
    assert ConnectorType.KARMA_PAYMENT.value in types
    assert ConnectorType.TAKEOUT_DELIVERY.value in types
    assert ConnectorType.ECOMMERCE_MARKETPLACE.value in types
    assert ConnectorType.FOREIGN_TRADE.value in types
    assert ConnectorType.LOGISTICS.value in types
    assert ConnectorType.CROSS_BORDER.value in types
