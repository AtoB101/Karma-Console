"""受控开发 Agent（DevOps）变更单：安全扫描、回归门禁、审批流端点测试。"""


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def test_catalog_readonly(workspace):
    w = workspace
    r = w["client"].get(_url(w, "/devops/catalog"), headers=w["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert "allowed" in data and "denied" in data
    blob = " ".join(data["denied"])
    assert "依赖" in blob and "重启" in blob


def test_create_change_safety_scan(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "给客户面板加一个导出按钮"})
    assert r.status_code == 200, r.text
    ch = r.json()["change"]
    assert ch["status"] == "draft"
    assert ch["regression_status"] == "pending"
    assert r.json()["safety"]["forbidden_hits"] == []


def test_forbidden_intent_flagged_r3(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "pip install 新依赖并自动重启服务上线"})
    assert r.status_code == 200, r.text
    ch = r.json()["change"]
    assert ch["risk_level"] == "R3"
    assert ch["forbidden_hits"]


def test_approve_blocked_when_regression_fail(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "调整库存面板配色"})
    cid = r.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{cid}/submit"))
    _post(w, _url(w, f"/devops/changes/{cid}/regression"), {"status": "fail"})
    r = _post(w, _url(w, f"/devops/changes/{cid}/approve"), {})
    assert r.status_code == 409, r.text


def test_full_flow_pass_then_applied(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"title": "补货提醒", "requirement": "做一个库存补货提醒面板"})
    cid = r.json()["change"]["id"]
    assert _post(w, _url(w, f"/devops/changes/{cid}/submit")).status_code == 200
    assert _post(w, _url(w, f"/devops/changes/{cid}/regression"), {"status": "pass"}).status_code == 200
    ap = _post(w, _url(w, f"/devops/changes/{cid}/approve"), {"note": "同意"})
    assert ap.status_code == 200, ap.text
    assert ap.json()["change"]["status"] == "approved"
    done = _post(w, _url(w, f"/devops/changes/{cid}/mark-applied"))
    assert done.status_code == 200, done.text
    assert done.json()["change"]["status"] == "applied"


def test_reject_terminal(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "废弃的试验需求"})
    cid = r.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{cid}/submit"))
    rj = _post(w, _url(w, f"/devops/changes/{cid}/reject"), {"note": "不做"})
    assert rj.status_code == 200
    assert rj.json()["change"]["status"] == "rejected"


def test_approval_and_landed_written_to_audit_chain(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "审计留痕验证需求"})
    cid = r.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{cid}/submit"))
    _post(w, _url(w, f"/devops/changes/{cid}/regression"), {"status": "pass"})
    _post(w, _url(w, f"/devops/changes/{cid}/approve"), {"note": "同意"})
    _post(w, _url(w, f"/devops/changes/{cid}/mark-applied"))
    from app.db import SessionLocal
    from app.models import AuditLog
    db = SessionLocal()
    try:
        actions = [row.action for row in db.query(AuditLog).filter(
            AuditLog.workspace_id == w["workspace_id"]).all()]
    finally:
        db.close()
    assert "devops_change_approved" in actions
    assert "devops_change_marked_applied" in actions
    assert "devops_change_created" in actions


def test_dashboard_counts_pending_change(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "待批计数验证"})
    cid = r.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{cid}/submit"))
    d = w["client"].get(_url(w, "/dashboard"), headers=w["headers"])
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["kpis"]["devops_pending"] == 1
    assert body["devops_pending"] == 1


def test_regression_gate_summary_pure():
    from app.devops_agent import regression_gate_summary
    s = regression_gate_summary([
        {"id": "a", "regression_status": "pass"},
        {"id": "b", "regression_status": "fail"},
        {"id": "c", "regression_status": "pending"},
    ])
    assert s["counts"] == {"pass": 1, "fail": 1, "pending": 1}
    assert s["gate_ok"] is False
    assert [x["id"] for x in s["blocked"]] == ["b"]
    assert [x["id"] for x in s["ready"]] == ["a"]
    clean = regression_gate_summary([{"id": "x", "regression_status": "pass"}])
    assert clean["gate_ok"] is True


def test_regression_summary_endpoint(workspace):
    w = workspace
    r1 = _post(w, _url(w, "/devops/changes"), {"requirement": "通过单"})
    c1 = r1.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{c1}/submit"))
    _post(w, _url(w, f"/devops/changes/{c1}/regression"), {"status": "pass"})
    r2 = _post(w, _url(w, "/devops/changes"), {"requirement": "失败单"})
    c2 = r2.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{c2}/submit"))
    _post(w, _url(w, f"/devops/changes/{c2}/regression"), {"status": "fail"})
    g = w["client"].get(_url(w, "/devops/regression-summary"), headers=w["headers"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["counts"]["pass"] == 1 and body["counts"]["fail"] == 1
    assert body["gate_ok"] is False
    assert any(x["id"] == c2 for x in body["blocked"])
    assert any(x["id"] == c1 for x in body["ready"])


def test_stale_changes_pure():
    from datetime import datetime, timedelta
    from app.devops_agent import stale_changes
    old = datetime.utcnow() - timedelta(hours=30)
    s = stale_changes([{"id": "a", "status": "proposed", "submitted_at": old}],
                      datetime.utcnow(), 24)
    assert s["stale_count"] == 1
    assert s["stale"][0]["waiting_hours"] >= 24
    fresh = stale_changes([{"id": "b", "status": "proposed",
                            "submitted_at": datetime.utcnow()}], datetime.utcnow(), 24)
    assert fresh["stale_count"] == 0


def test_backlog_readonly_fresh_waiting(workspace):
    w = workspace
    r = _post(w, _url(w, "/devops/changes"), {"requirement": "新鲜待批单"})
    cid = r.json()["change"]["id"]
    _post(w, _url(w, f"/devops/changes/{cid}/submit"))
    g = w["client"].get(_url(w, "/devops/backlog?threshold_hours=24"),
                        headers=w["headers"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["threshold_hours"] == 24
    assert body["stale_count"] == 0
    assert any(i["id"] == cid for i in body["waiting"])
