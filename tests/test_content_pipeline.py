"""内容流水线：状态机闸门、CEO 拍板流、未批准禁发布、审计留痕测试。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


# ---------- 纯函数 ----------

def test_transitions_and_gate_pure():
    from app import content_pipeline as cp
    assert cp.can_transition("draft", "review")
    assert cp.can_transition("review", "approved")
    assert cp.can_transition("approved", "ready_to_publish")
    assert not cp.can_transition("draft", "approved")
    assert not cp.can_transition("review", "ready_to_publish")
    ok, _ = cp.gate_publish("ready_to_publish")
    assert ok
    for st in ("draft", "review", "rejected", "approved"):
        ok2, reason = cp.gate_publish(st)
        assert not ok2 and reason
    assert cp.publishable("ready_to_publish")
    assert not cp.publishable("approved")
    assert cp.valid_kind("copy") and cp.valid_kind("video")
    assert not cp.valid_kind("audio")


# ---------- 端点全流程 ----------

def test_draft_submit_approve_ready_flow(workspace):
    w = workspace
    r = _post(w, _url(w, "/content"), {
        "kind": "copy", "title": "棉麻衬衫卖点文案",
        "topic": "面向 25-35 岁女性，夏季透气", "body": "待生成正文",
    })
    assert r.status_code == 200, r.text
    cid = r.json()["item"]["id"]
    item = r.json()["item"]
    assert item["status"] == "draft"

    # draft 不能直接批准/交接待发布
    assert _post(w, _url(w, f"/content/{cid}/to-ready")).status_code == 409
    # 提交拍板
    s = _post(w, _url(w, f"/content/{cid}/submit"))
    assert s.status_code == 200, s.text
    assert s.json()["item"]["status"] == "review"
    # review 不能直接交接待发布
    assert _post(w, _url(w, f"/content/{cid}/to-ready")).status_code == 409
    # CEO 批准
    a = _post(w, _url(w, f"/content/{cid}/approve"), {"note": "可用"})
    assert a.status_code == 200, a.text
    assert a.json()["item"]["status"] == "approved"
    # 交接待发布
    rd = _post(w, _url(w, f"/content/{cid}/to-ready"))
    assert rd.status_code == 200, rd.text
    assert rd.json()["item"]["status"] == "ready_to_publish"


def test_reject_flow_and_audit(workspace):
    w = workspace
    r = _post(w, _url(w, "/content"), {"kind": "video", "title": "短视频脚本", "topic": "产品展示"})
    cid = r.json()["item"]["id"]
    _post(w, _url(w, f"/content/{cid}/submit"))
    j = _post(w, _url(w, f"/content/{cid}/reject"), {"note": "卖点不够突出，重写"})
    assert j.status_code == 200, j.text
    assert j.json()["item"]["status"] == "rejected"
    assert j.json()["item"]["reviewer_note"] == "卖点不够突出，重写"
    # 驳回后可重新提交
    s2 = _post(w, _url(w, f"/content/{cid}/submit"))
    assert s2.status_code == 200, s2.text

    from app.db import SessionLocal
    from app.models import AuditLog
    db = SessionLocal()
    try:
        actions = [x.action for x in db.query(AuditLog).filter(
            AuditLog.workspace_id == w["workspace_id"]).all()]
    finally:
        db.close()
    assert "content_drafted" in actions
    assert "content_rejected" in actions
    assert "content_submitted" in actions


def test_list_filter(workspace):
    w = workspace
    _post(w, _url(w, "/content"), {"kind": "copy", "title": "A"})
    _post(w, _url(w, "/content"), {"kind": "video", "title": "B"})
    lst = w["client"].get(_url(w, "/content?kind=copy"), headers=w["headers"]).json()
    assert all(i["kind"] == "copy" for i in lst["items"])
    st = w["client"].get(_url(w, "/content?status=draft"), headers=w["headers"]).json()
    assert all(i["status"] == "draft" for i in st["items"])


def test_unknown_kind_rejected(workspace):
    w = workspace
    r = _post(w, _url(w, "/content"), {"kind": "audio", "title": "x"})
    assert r.status_code == 400, r.text


def test_review_queue_aggregates_both_kinds(workspace):
    """CEO 审批台：status=review 跨文案/视频聚合，且只含待拍板项。"""
    w = workspace
    c = _post(w, _url(w, "/content"), {"kind": "copy", "title": "文案待拍板"}).json()["item"]["id"]
    v = _post(w, _url(w, "/content"), {"kind": "video", "title": "视频待拍板"}).json()["item"]["id"]
    _post(w, _url(w, f"/content/{c}/submit"))
    _post(w, _url(w, f"/content/{v}/submit"))
    # 另起一条保持 draft，不应进 review 队列
    d = _post(w, _url(w, "/content"), {"kind": "copy", "title": "草稿留档"}).json()["item"]["id"]
    q = w["client"].get(_url(w, "/content?status=review"), headers=w["headers"]).json()["items"]
    ids = {i["id"] for i in q}
    assert c in ids and v in ids and d not in ids
    assert all(i["status"] == "review" for i in q)

