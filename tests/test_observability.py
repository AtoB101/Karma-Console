"""Tests for the observability registry, JSON logging and metric endpoints."""
import json

from app.observability import (
    JsonFormatter,
    MetricsRegistry,
    record_chat,
    record_request,
    record_tool,
    record_webhook,
    registry,
)


def test_registry_counters_and_gauge():
    reg = MetricsRegistry()
    reg.counter("requests", 2, labels={"status": "ok"})
    reg.counter("requests", 1, labels={"status": "err"})
    reg.gauge("uptime_probe", 7)
    snap = reg.snapshot()
    assert snap["metrics"]["requests"]["kind"] == "counter"
    assert snap["metrics"]["requests"]["value"] == 3
    assert snap["metrics"]["requests"]["labels"]["status:ok"] == 2
    assert snap["metrics"]["requests"]["labels"]["status:err"] == 1
    assert snap["metrics"]["uptime_probe"]["value"] == 7
    assert snap["uptime_seconds"] >= 0


def test_record_chat_and_tool_increment_global_registry(client, workspace):
    before = registry.snapshot()["metrics"].get("chat_requests", {}).get("value", 0)
    record_chat(workspace["workspace_id"], status="ok", duration_ms=1.5)
    after = registry.snapshot()["metrics"]["chat_requests"]["value"]
    assert after == before + 1

    record_tool("list_products", workspace["workspace_id"], status="error", duration_ms=2.0)
    snap = registry.snapshot()["metrics"]
    assert snap["tool_executions"]["labels"]["tool:list_products"] >= 1
    assert snap["tool_errors"]["labels"]["tool:list_products"] >= 1


def test_record_webhook_and_request():
    before = registry.snapshot()["metrics"].get("webhook_requests", {}).get("value", 0)
    record_webhook("telegram", "", status="ok", duration_ms=0.5)
    record_request("POST", "/chat/inbound", 200, 3.2)
    snap = registry.snapshot()["metrics"]
    assert snap["webhook_requests"]["value"] == before + 1
    assert snap["http_requests"]["value"] >= 1


def test_json_formatter_emits_json():
    import logging

    record = logging.LogRecord(
        name="krama", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    record.event = "chat"
    record.workspace_id = "ws1"
    output = JsonFormatter().format(record)
    data = json.loads(output)
    assert data["message"] == "hello world"
    assert data["event"] == "chat"
    assert data["workspace_id"] == "ws1"


def test_metrics_endpoint(client, workspace):
    r = client.get("/health/metrics")
    assert r.status_code == 200
    assert "metrics" in r.json()

    r = client.get(f"/workspaces/{workspace['workspace_id']}/metrics", headers=workspace["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == workspace["workspace_id"]
    assert "metrics" in body
