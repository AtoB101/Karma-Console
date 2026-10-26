"""P72 M03 panel data bus: declarative read-only aggregation (count/sum/avg,
where filter, field projection, limit), workspace-isolated, no side effects.
"""
from app.db import SessionLocal
from app.data_bus import resolve
from app.models import Order, Product, Todo


def _seed(ws: str) -> SessionLocal:
    db = SessionLocal()
    db.add_all([
        Product(workspace_id=ws, name="低库存衫", price=39.0, status="published", stock=2),
        Product(workspace_id=ws, name="常备裤", price=59.0, status="published", stock=20),
        Order(workspace_id=ws, channel="ecommerce_independent", order_reference="O-1",
              status="paid", payment_status="paid", total_amount=100.0, currency="CNY"),
        Order(workspace_id=ws, channel="ecommerce_independent", order_reference="O-2",
              status="created", payment_status="unpaid", total_amount=50.0, currency="CNY"),
        Todo(workspace_id=ws, kind="needs_reply", title="待回客户", status="pending", ref_type="monitor"),
    ])
    db.commit()
    return db


def test_select_count_where_and_sum(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    db = _seed(ws)
    try:
        out = resolve(db, ws, {
            "select": {
                "low_stock": {"source": "products", "fn": "count",
                              "where": {"field": "stock", "op": "lte", "value": 5}},
                "paid_count": {"source": "orders", "fn": "count",
                               "where": {"field": "payment_status", "op": "eq", "value": "paid"}},
                "paid_total": {"source": "orders", "fn": "sum", "field": "total_amount",
                               "where": {"field": "payment_status", "op": "eq", "value": "paid"}},
                "todo_count": {"source": "todos", "fn": "count"},
            }
        })
    finally:
        db.close()
    assert out["errors"] == []
    assert out["data"]["low_stock"] == 1
    assert out["data"]["paid_count"] == 1
    assert out["data"]["paid_total"] == 100.0
    assert out["data"]["todo_count"] == 1


def test_items_projection_where_limit(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    db = _seed(ws)
    try:
        out = resolve(db, ws, {
            "items": {
                "paid_orders": {
                    "source": "orders", "limit": 1,
                    "where": {"field": "payment_status", "op": "eq", "value": "paid"},
                    "project": {"ref": "order_reference", "total": "total_amount"},
                }
            }
        })
    finally:
        db.close()
    assert out["errors"] == []
    rows = out["data"]["paid_orders"]
    assert len(rows) == 1
    assert rows[0]["ref"] == "O-1"
    assert rows[0]["total"] == 100.0
    # projection drops non-selected fields
    assert "status" not in rows[0]


def test_unknown_source_reported_not_raised(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    db = _seed(ws)
    try:
        out = resolve(db, ws, {"select": {"x": {"source": "nope", "fn": "count"}}})
    finally:
        db.close()
    assert any("unknown source: nope" in e for e in out["errors"])
    assert "x" not in out["data"]


def test_bad_spec_does_not_raise(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    db = _seed(ws)
    try:
        out = resolve(db, ws, {"select": {"x": "not-an-object"}})
    finally:
        db.close()
    assert any("select.x must be an object" in e for e in out["errors"])
    assert out["data"] == {}


def test_workspace_isolation(client, workspace):
    ws = workspace["workspace_id"]
    db = _seed(ws)
    try:
        # A row owned by a different workspace must never appear here.
        db.add(Product(workspace_id="other-ws", name="外人货", price=1.0, stock=999))
        db.commit()
        out = resolve(db, ws, {"select": {"all_products": {"source": "products", "fn": "count"}}})
    finally:
        db.close()
    assert out["data"]["all_products"] == 2


def test_http_endpoint_roundtrip(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    db = _seed(ws)
    db.close()
    r = client.post(f"/workspaces/{ws}/bus/resolve", json={
        "select": {"paid_total": {"source": "orders", "fn": "sum", "field": "total_amount",
                                  "where": {"field": "payment_status", "op": "eq", "value": "paid"}}}
    }, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["paid_total"] == 100.0
    assert body["errors"] == []
