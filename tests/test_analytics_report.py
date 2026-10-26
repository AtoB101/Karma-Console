"""经营报表聚合端点（生成式窗口数据源）冒烟测试：30天/昨日窗口只读，不越权写。"""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Customer, Order, Product


def _seed_ctx(w):
    c, hdrs, ws = w["client"], w["headers"], w["workspace_id"]
    assert c.post(f"/workspaces/{ws}/products", headers=hdrs, json={"name": "报表棉麻衬衫", "price": 99.0, "stock": 12}).status_code in (200, 201)
    db = SessionLocal()
    try:
        db.add(Order(
            workspace_id=ws,
            channel="ecommerce_independent",
            order_reference="RPT-001",
            status="PAID",
            total_amount=99.0,
            currency="CNY",
            customer={"name": "报表王先生"},
            items=[{"name": "报表棉麻衬衫", "quantity": 1, "price": 99.0}],
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))
        db.commit()
    finally:
        db.close()


def test_analytics_report_smoke(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    _seed_ctx(workspace)
    r = c.get(f"/workspaces/{ws}/analytics/report?days=30&offset=0", headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert len(body["buckets"]) == 30
    assert body["summary"]["orders"] >= 1
    assert body["summary"]["revenue"] >= 99.0
    assert body["summary"]["total_products"] >= 1
    assert body["caveat"]
    dates = [b["date"] for b in body["buckets"]]
    assert dates == sorted(dates)
    hit = next(b for b in body["buckets"] if b["orders"] >= 1)
    assert hit["revenue"] >= 99.0


def test_analytics_report_yesterday_window(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    r = c.get(f"/workspaces/{ws}/analytics/report?days=1&offset=1", headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert body["range"]["label"] == "昨日"
    assert len(body["buckets"]) == 1


def test_analytics_report_bounds(workspace):
    c, hdrs, ws = workspace["client"], workspace["headers"], workspace["workspace_id"]
    r = c.get(f"/workspaces/{ws}/analytics/report?days=999&offset=-3", headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert len(body["buckets"]) == 180
    assert body["range"]["offset"] == 0
