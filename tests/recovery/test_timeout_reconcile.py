"""TOOL-001 acceptance: external timeout must not auto-fail; reconcile first."""
import asyncio

import httpx

from app.connectors import ReservedConnector
from app.models import ConnectorType


def test_ecommerce_timeout_returns_unknown_and_needs_reconcile(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
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
    assert r.status_code == 201

    async def timeout_request(method, url, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("app.functions.request_http", timeout_request)

    r = client.post(
        "/gateway/execute",
        json={"tool": "create_order", "params": {"channel": "ecommerce_independent", "items": [{"sku": "A1"}]}, "idempotency_key": "ord-timeout"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "ALLOWED"
    result = body["result"]
    assert result["receipt_status"] == "UNKNOWN"
    assert result["needs_reconcile"] is True
    assert result["verification"]["status"] == "UNKNOWN"


def test_reserved_connector_timeout_returns_unknown(monkeypatch):
    async def boom(method, url, **kwargs):
        raise httpx.TransportError("unreachable")

    monkeypatch.setattr("app.functions.request_http", boom)
    conn = ReservedConnector(ConnectorType.LOGISTICS.value, config={"endpoint_url": "https://logi.example.com/ship"})
    result = asyncio.run(conn.execute("ship", {"idempotency_key": "ship-timeout"}))
    assert result["receipt_status"] == "UNKNOWN"
    assert result["needs_reconcile"] is True
    assert result["verification"]["status"] == "UNKNOWN"


def test_duplicate_submit_is_controlled(client, workspace):
    """R3 payment with the same idempotency key must not create two approvals."""
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    payload = {"tool": "make_payment", "params": {"amount": 10}, "idempotency_key": "pay-dup"}
    r1 = client.post("/gateway/execute", json=payload, headers=h)
    r2 = client.post("/gateway/execute", json=payload, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["decision"] == "PENDING"
    assert r2.json()["decision"] == "PENDING"
    assert r1.json()["approval_id"] == r2.json()["approval_id"]