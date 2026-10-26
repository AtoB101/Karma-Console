"""统一生意域洞察端点（生成式意图面板数据源）测试：11 域全部只读真实聚合，不越权写。"""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    ContentItem,
    Customer,
    Order,
    Product,
    Ticket,
    Todo,
)


DOMAINS = ["report", "orders", "finance", "products", "stock", "customers", "service", "marketing", "content", "tasks", "system"]


def _seed_ctx(w):
    c, hdrs, ws = w["client"], w["headers"], w["workspace_id"]
    assert c.post(f"/workspaces/{ws}/products", headers=hdrs, json={"name": "洞察棉麻衬衫", "price": 199.0, "stock": 3}).status_code in (200, 201)
    db = SessionLocal()
    try:
        db.add(Order(
            workspace_id=ws,
            channel="ecommerce_independent",
            order_reference="INS-001",
            status="PAID",
            total_amount=199.0,
            currency="CNY",
            customer={"name": "洞察王先生"},
            items=[{"name": "洞察棉麻衬衫", "quantity": 1, "price": 199.0}],
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.add(Customer(
            workspace_id=ws, channel="chat", external_id="ins_c1",
            name="洞察客户甲", stage="negotiate", source="广告",
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        ))
        db.add(Todo(workspace_id=ws, kind="info", title="洞察待办：跟进SOP", status="pending"))
        db.add(ApprovalRequest(
            workspace_id=ws, action="洞察批量调价", risk_level="R1",
            status=ApprovalStatus.PENDING.value,
            reason="测试待拍板", idempotency_key="ins_appr_1",
        ))
        db.add(Ticket(
            workspace_id=ws, title="洞察售后工单", type="aftersale", priority="P1",
            status="processing", description="测试工单",
        ))
        db.add(ContentItem(
            workspace_id=ws, kind="copy", title="洞察文案条目", status="proposed",
            topic="测试选题", body="测试文案正文",
        ))
        db.commit()
    finally:
        db.close()


def test_insight_all_domains(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    _seed_ctx(workspace)
    for dom in DOMAINS:
        r = c.get(f"/workspaces/{ws}/analytics/insight?domain={dom}&days=30&offset=0", headers=hdrs)
        assert r.status_code == 200, f"{dom}: {r.text}"
        body = r.json()
        assert body["domain"] == dom
        assert body["meta"]["title"]
        assert isinstance(body["kpis"], list) and body["kpis"]
        assert body["caveat"]
        if body["trend"]:
            assert body["trend"]["buckets"]
        if body["dist"]:
            assert body["dist"]["items"]
        if body["list"]:
            assert body["list"]["cols"] and body["list"]["rows"]


def test_insight_real_values(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    _seed_ctx(workspace)
    # 财务域真实回款：PAID 199 元
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=finance", headers=hdrs)
    body = r.json()
    kpi = {k["label"]: k for k in body["kpis"]}
    assert kpi["本地营收"]["value"] >= 199.0
    assert body["list"]["rows"]
    # 客户域：详谈 1 位
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=customers", headers=hdrs)
    body = r.json()
    kpi = {k["label"]: k for k in body["kpis"]}
    assert kpi["详谈客户"]["value"] >= 1
    # 客服域：处理中 1 单
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=service", headers=hdrs)
    body = r.json()
    kpi = {k["label"]: k for k in body["kpis"]}
    assert kpi["进行中"]["value"] >= 1
    # 内容域：待拍板 1 条
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=content", headers=hdrs)
    body = r.json()
    kpi = {k["label"]: k for k in body["kpis"]}
    assert kpi["待CEO拍板"]["value"] >= 1
    # 任务域：待办/待拍板
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=tasks", headers=hdrs)
    body = r.json()
    kpi = {k["label"]: k for k in body["kpis"]}
    assert kpi["待办任务"]["value"] >= 1 and kpi["待你拍板"]["value"] >= 1
    # 昨日窗口标签
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=report&days=1&offset=1", headers=hdrs)
    assert r.json()["meta"]["label"] == "昨日"


def test_insight_unknown_domain(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    r = c.get(f"/workspaces/{ws}/analytics/insight?domain=nope", headers=hdrs)
    assert r.status_code == 200
    assert "error" in r.json()
