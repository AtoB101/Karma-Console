"""P21 inventory panel: restock flows through the gateway add_stock and
persists the new stock level with a receive movement."""
from app.db import SessionLocal
from app.models import Product, StockMovement


def test_restock_via_gateway_increases_stock(client, workspace):
    db = SessionLocal()
    try:
        p = Product(
            workspace_id=workspace["workspace_id"],
            name="测试商品",
            price=9.9,
            stock=3,
            status="published",
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        r = client.post(
            "/gateway/execute",
            json={
                "tool": "add_stock",
                "params": {"product_id": p.id, "quantity": 10},
                "idempotency_key": f"add_stock:{p.id}:10",
            },
            headers=workspace["headers"],
        )
        assert r.status_code == 200
        assert r.json()["decision"] == "ALLOWED"

        db.expire_all()
        refreshed = db.get(Product, p.id)
        assert refreshed.stock == 13
        move = db.execute(
            StockMovement.__table__.select().where(
                StockMovement.product_id == p.id,
                StockMovement.reason == "receive",
            )
        ).first()
        assert move is not None and move.delta == 10
    finally:
        db.rollback()
        db.close()
