"""P20 customer panel: list exposes stage; stage advance flows through the
gateway (update_customer, R1 idempotent, no forced approval) and persists."""
from app.db import SessionLocal
from app.models import Customer


def test_list_customers_exposes_stage(client, workspace):
    db = SessionLocal()
    try:
        c = Customer(
            workspace_id=workspace["workspace_id"],
            channel="chat",
            external_id="u-1",
            name="测试客户",
            stage="lead",
            profile={"tag": "vip"},
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        r = client.get(
            f"/workspaces/{workspace['workspace_id']}/customers",
            headers=workspace["headers"],
        )
        assert r.status_code == 200
        row = next(x for x in r.json()["customers"] if x["id"] == c.id)
        assert row["stage"] == "lead"
        assert row["profile"] == {"tag": "vip"}
    finally:
        db.rollback()
        db.close()


def test_advance_customer_stage_via_gateway(client, workspace):
    db = SessionLocal()
    try:
        c = Customer(
            workspace_id=workspace["workspace_id"],
            channel="chat",
            external_id="u-2",
            name="成交客户",
            stage="new",
        )
        db.add(c)
        db.commit()
        db.refresh(c)

        r = client.post(
            "/gateway/execute",
            json={
                "tool": "update_customer",
                "params": {"customer_id": c.id, "stage": "won"},
                "idempotency_key": f"update_customer:{c.id}:won",
            },
            headers=workspace["headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "ALLOWED"

        db.expire_all()
        assert db.get(Customer, c.id).stage == "won"
    finally:
        db.rollback()
        db.close()
