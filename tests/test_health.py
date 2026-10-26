"""P77 liveness/readiness separation: /health always alive, /health/ready checks DB."""


def test_liveness_always_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_ok_when_db_answers(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"


def test_ready_503_when_db_down(client, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "ping_db", lambda: (False, "connection refused"))
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert "db not ready" in r.json()["detail"]


def test_health_metrics_snapshot(client):
    r = client.get("/health/metrics")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
