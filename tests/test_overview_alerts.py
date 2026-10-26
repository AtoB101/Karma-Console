"""P22 overview alert counts: low stock / pending ship / pending approvals are
exposed on the overview payload and computed from live rows."""
from app.db import SessionLocal
from app.models import Product


def test_overview_exposes_alert_counts_when_empty(client, workspace):
    r = client.get(
        f"/workspaces/{workspace['workspace_id']}/overview",
        headers=workspace["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("low_stock", "pending_ship", "pending_approvals"):
        assert key in body
        assert body[key] == 0


def test_overview_counts_low_stock(client, workspace):
    db = SessionLocal()
    try:
        db.add(Product(workspace_id=workspace["workspace_id"], name="缺货款", price=1, stock=2))
        db.add(Product(workspace_id=workspace["workspace_id"], name="充足款", price=1, stock=99))
        db.commit()
        r = client.get(
            f"/workspaces/{workspace['workspace_id']}/overview",
            headers=workspace["headers"],
        )
        assert r.json()["low_stock"] == 1
    finally:
        db.rollback()
        db.close()
