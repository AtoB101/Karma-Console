import uuid

import pytest

from app.functions import effective_risk, is_private_host, validate_name, validate_url
from app.models import RiskLevel


def _create_function(workspace, **overrides):
    payload = {
        "name": "read_receipt",
        "kind": "http_read",
        "category": "receipt",
        "endpoint_url": "https://shop.example.com/orders/{order_id}/receipt",
        "method": "GET",
        "verification": {"type": "http", "status_path": "receipt_status", "reference_path": "provider_reference"},
    }
    payload.update(overrides)
    r = workspace["client"].post(
        f"/workspaces/{workspace['workspace_id']}/functions", json=payload, headers=workspace["headers"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_function_lifecycle(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    fn = _create_function(workspace)
    assert fn["effective_risk"] == "R0"

    r = client.get(f"/workspaces/{ws}/functions", headers=h)
    assert [f["name"] for f in r.json()["functions"]] == ["read_receipt"]

    r = client.get(f"/workspaces/{ws}/functions/{fn['id']}", headers=h)
    assert r.json()["category"] == "receipt"

    r = client.patch(
        f"/workspaces/{ws}/functions/{fn['id']}",
        json={"description": "读取订单回执", "enabled": True},
        headers=h,
    )
    assert r.json()["description"] == "读取订单回执"

    r = client.delete(f"/workspaces/{ws}/functions/{fn['id']}", headers=h)
    assert r.status_code == 204
    r = client.get(f"/workspaces/{ws}/functions", headers=h)
    assert r.json()["functions"] == []


def test_function_name_conflicts_with_builtin(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/functions",
        json={"name": "list_products", "kind": "http_read"},
        headers=workspace["headers"],
    )
    assert r.status_code == 400


def test_function_duplicate_name_rejected(client, workspace):
    _create_function(workspace)
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/functions",
        json={"name": "read_receipt", "kind": "http_read"},
        headers=workspace["headers"],
    )
    assert r.status_code == 409


def test_http_read_function_executes_and_verifies(client, workspace, monkeypatch):
    captured = {}

    async def fake_request_http(method, url, *, headers=None, json_body=None, params=None, timeout=None):
        captured["url"] = url
        return {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "json": {"receipt_status": "SUCCEEDED", "provider_reference": "ref-123", "order": {"id": "9"}},
            "text": "{}",
        }

    monkeypatch.setattr("app.functions.request_http", fake_request_http)
    _create_function(workspace)
    r = client.post(
        "/gateway/execute",
        json={"tool": "read_receipt", "params": {"order_id": "9"}},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["status_code"] == 200
    assert body["result"]["verification"]["status"] == "SUCCEEDED"
    assert body["result"]["verification"]["reference"] == "ref-123"
    assert "order_id=9" in captured["url"] or "/9/" in captured["url"]


def test_http_read_blocks_private_host(client, workspace):
    _create_function(workspace, endpoint_url="http://127.0.0.1:8000/internal", method="GET")
    r = client.post(
        "/gateway/execute", json={"tool": "read_receipt", "params": {}}, headers=workspace["headers"]
    )
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert "private" in body["result"]["error"]


def test_karma_function_requires_approval(client, workspace):
    _create_function(
        workspace,
        name="karma_collect",
        kind="karma",
        category="payment",
        endpoint_url="",
        verification={"type": "none"},
    )
    r = client.post(
        "/gateway/execute",
        json={"tool": "karma_collect", "params": {"action": "pay", "amount": 10.0}, "idempotency_key": "kf-1"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["decision"] == "PENDING"
    assert body["reason"] == "approval required"


def test_model_function_runs_without_approval(client, workspace):
    _create_function(
        workspace,
        name="write_copy",
        kind="model",
        endpoint_url="",
        config={"prompt": "写一段商品文案"},
        verification={"type": "none"},
    )
    r = client.post(
        "/gateway/execute", json={"tool": "write_copy", "params": {"prompt": "棉麻衬衫"}}, headers=workspace["headers"]
    )
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["provider"] == "mock"
    assert "mock" in body["result"]["text"]


def test_function_not_visible_in_other_workspace(client, workspace):
    _create_function(workspace)
    other_ws = client.post(
        "/workspaces", json={"name": "第二家店", "region": "CN", "business_category": "food"}, headers=workspace["headers"]
    ).json()
    h2 = {**workspace["headers"], "X-Workspace-Id": other_ws["id"]}
    r = client.post("/gateway/execute", json={"tool": "read_receipt", "params": {}}, headers=h2)
    assert r.status_code == 400


def test_layout_references_function(client, workspace):
    fn = _create_function(workspace)
    ws = workspace["workspace_id"]
    r = client.post(
        f"/workspaces/{ws}/layout",
        json={"name": "订单回执", "kind": "function", "target_type": "function", "target_id": fn["id"], "config": {"icon": "receipt"}},
        headers=workspace["headers"],
    )
    assert r.status_code == 201
    r = client.get(f"/workspaces/{ws}/layout", headers=workspace["headers"])
    area = r.json()["areas"][0]
    assert area["target_type"] == "function"
    assert area["target_id"] == fn["id"]


def test_viewer_denied_on_http_write(client, workspace):
    from app.db import SessionLocal
    from app.models import Membership, Role

    _create_function(workspace, name="push_order", kind="http_write", endpoint_url="https://shop.example.com/orders", method="POST")
    other = client.post("/users", json={"email": f"v{uuid.uuid4().hex[:8]}@x.com"}).json()
    r = client.post(f"/users/{other['id']}/api-keys")
    key = r.json()["api_key"]
    db = SessionLocal()
    try:
        db.add(Membership(workspace_id=workspace["workspace_id"], user_id=other["id"], role=Role.VIEWER.value))
        db.commit()
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {key}", "X-Workspace-Id": workspace["workspace_id"]}
    r = client.post("/gateway/execute", json={"tool": "push_order", "params": {}}, headers=headers)
    assert r.json()["decision"] == "DENIED"


def test_unit_risk_floor_and_url_guard():
    class FakeFn:
        kind = "http_write"
        risk_level = "R0"

    assert effective_risk(FakeFn()) is RiskLevel.R2

    assert validate_name("receipt_reader_1") == "receipt_reader_1"
    with pytest.raises(Exception):
        validate_name("1bad-name")

    assert is_private_host("127.0.0.1") is True
    assert is_private_host("192.168.1.5") is True
    assert is_private_host("localhost") is True
    assert is_private_host("shop.example.com") is False

    assert validate_url("https://shop.example.com/x") == "https://shop.example.com/x"
    with pytest.raises(Exception):
        validate_url("file:///etc/passwd")
    with pytest.raises(Exception):
        validate_url("http://10.0.0.1/admin")