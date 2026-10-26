"""P5 observability: request-id correlation headers, alert rule engine, alerts API."""
from app.alerts import DEFAULT_THRESHOLDS, evaluate
from app.models import Decision, RiskLevel


# --- pure rule engine -------------------------------------------------------

def test_tool_error_rate_alert_when_over_threshold():
    metrics = {
        "tool_executions": {"kind": "counter", "value": 20, "labels": {"status:error": 6, "status:ok": 14}},
    }
    alerts = evaluate(metrics)
    assert any(a.rule == "tool_error_rate" for a in alerts)


def test_no_alert_below_min_total():
    metrics = {
        "tool_executions": {"kind": "counter", "value": 3, "labels": {"status:error": 3}},
    }
    assert evaluate(metrics) == []


def test_http_5xx_alert_above_min_total():
    metrics = {
        "http_5xx": {"kind": "counter", "value": 7, "labels": {"path:/x": 7}},
    }
    alerts = evaluate(metrics)
    assert any(a.rule == "http_5xx" for a in alerts)


def test_error_burst_alert():
    metrics = {
        "errors": {"kind": "counter", "value": 9, "labels": {"scope:gateway": 9}},
    }
    alerts = evaluate(metrics, {"error_burst_count": 8})
    assert any(a.rule == "error_burst" for a in alerts)


def test_approval_pending_warns():
    metrics = {
        "approval_pending": {"kind": "gauge", "value": 1, "labels": {}},
    }
    alerts = evaluate(metrics, DEFAULT_THRESHOLDS)
    assert any(a.rule == "approval_pending" for a in alerts)


# --- HTTP endpoints --------------------------------------------------------

def test_metrics_endpoint(client, workspace):
    r = client.get("/health/metrics")
    assert r.status_code == 200
    assert "metrics" in r.json()


def test_alerts_list_endpoint(client, workspace):
    r = client.get(f"/workspaces/{workspace['workspace_id']}/alerts", headers=workspace["headers"])
    assert r.status_code == 200
    assert "alerts" in r.json()


def test_check_alerts_owner_endpoint(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/alerts/check", headers=workspace["headers"]
    )
    assert r.status_code == 200
    body = r.json()
    assert "triggered" in body and "deliveries" in body


# --- request-id correlation ------------------------------------------------

def test_request_id_echoed_from_header(client, workspace):
    r = client.get("/health", headers={"X-Request-ID": "corr-abc123"})
    assert r.headers.get("X-Request-ID") == "corr-abc123"


def test_request_id_generated_when_absent(client, workspace):
    r = client.get("/health")
    assert r.headers.get("X-Request-ID")


def test_audit_record_carries_request_id(workspace):
    from app.audit import record
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        entry = record(
            db,
            workspace_id=workspace["workspace_id"],
            actor_id=workspace["user_id"],
            action="test.action",
            risk_level=RiskLevel.R0.value,
            decision=Decision.ALLOWED,
            request_id="rid-123",
        )
        db.commit()
        assert entry.evidence["request_id"] == "rid-123"
    finally:
        db.rollback()
        db.close()
