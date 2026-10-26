"""P35 tenant isolation: one workspace must never read another's business data."""
import uuid


def _make_ws(client):
    email = f"iso{uuid.uuid4().hex[:10]}@example.com"
    uid = client.post("/users", json={"email": email, "display_name": "Owner"}).json()["id"]
    key = client.post(f"/users/{uid}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws_id = client.post(
        "/workspaces", json={"name": "隔离店", "region": "CN", "business_category": "general"}, headers=headers
    ).json()["id"]
    headers["X-Workspace-Id"] = ws_id
    return {"headers": headers, "workspace_id": ws_id}


def test_products_not_shared_between_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)

    created = client.post(
        f"/workspaces/{a['workspace_id']}/products",
        json={"name": "隔离店甲商品", "price": 99.0},
        headers=a["headers"],
    )
    assert created.status_code == 201

    a_list = client.get(f"/workspaces/{a['workspace_id']}/products", headers=a["headers"]).json()
    b_list = client.get(f"/workspaces/{b['workspace_id']}/products", headers=b["headers"]).json()
    a_names = [p["name"] for p in a_list["products"]]
    b_names = [p["name"] for p in b_list["products"]]
    assert "隔离店甲商品" in a_names
    assert "隔离店甲商品" not in b_names


def test_gateway_list_products_is_scoped_to_workspace(client):
    a = _make_ws(client)
    b = _make_ws(client)

    client.post(
        f"/workspaces/{a['workspace_id']}/products",
        json={"name": "网关隔离商品", "price": 12.0},
        headers=a["headers"],
    )
    rb = client.post(
        "/gateway/execute",
        json={"tool": "list_products", "params": {}},
        headers=b["headers"],
    )
    assert rb.json()["decision"] == "ALLOWED"
    names = [p.get("name") for p in (rb.json().get("result") or {}).get("products", [])]
    assert "网关隔离商品" not in names


def test_custom_functions_not_visible_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)

    client.post(
        f"/workspaces/{a['workspace_id']}/functions",
        json={"name": "a_only_reader", "kind": "http_read", "endpoint_url": "https://example.com/a"},
        headers=a["headers"],
    )
    b_fns = client.get(f"/workspaces/{b['workspace_id']}/functions", headers=b["headers"]).json()
    names = [f["name"] for f in b_fns["functions"]]
    assert "a_only_reader" not in names


def _seed_order_and_customer(a_ws):
    from app.db import SessionLocal
    from app.models import Order, Customer

    db = SessionLocal()
    try:
        order = Order(
            workspace_id=a_ws,
            order_reference="ISO-A-001",
            status="CREATED",
            payment_status="unpaid",
            total_amount=88.0,
            customer={"name": "隔离客户甲"},
            items=[{"sku": "A1", "qty": 1}],
        )
        customer = Customer(workspace_id=a_ws, channel="chat", external_id="ext-a-1", name="隔离客户甲", stage="lead")
        db.add(order)
        db.add(customer)
        db.commit()
        return order.id, customer.id
    finally:
        db.close()


def test_order_not_visible_or_accessible_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)
    order_id, _ = _seed_order_and_customer(a["workspace_id"])

    b_orders = client.get(f"/workspaces/{b['workspace_id']}/orders", headers=b["headers"]).json()
    assert all(o["id"] != order_id for o in b_orders["orders"])

    single = client.get(f"/workspaces/{b['workspace_id']}/orders/{order_id}", headers=b["headers"])
    assert single.status_code == 404


def test_customer_not_visible_or_accessible_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)
    _, customer_id = _seed_order_and_customer(a["workspace_id"])

    b_customers = client.get(f"/workspaces/{b['workspace_id']}/customers", headers=b["headers"]).json()
    assert all(c["id"] != customer_id for c in b_customers["customers"])

    single = client.get(f"/workspaces/{b['workspace_id']}/customers/{customer_id}", headers=b["headers"])
    assert single.status_code == 404


def test_pending_approval_not_visible_or_decidable_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)

    client.post(
        f"/workspaces/{a['workspace_id']}/connectors",
        json={"connector_type": "karma_payment", "name": "Karma 收款"},
        headers=a["headers"],
    )
    first = client.post(
        "/gateway/execute",
        json={"tool": "make_payment", "params": {"amount": 10.0}, "idempotency_key": "p37-pay"},
        headers=a["headers"],
    )
    assert first.json()["decision"] == "PENDING"
    approval_id = first.json()["approval_id"]

    b_list = client.get(f"/workspaces/{b['workspace_id']}/approvals", headers=b["headers"]).json()
    assert all(x["id"] != approval_id for x in b_list["approvals"])

    # B cannot approve or reject A's approval; A's request stays PENDING.
    rejected = client.post(
        f"/approvals/{approval_id}/decision",
        json={"approve": False},
        headers=b["headers"],
    )
    assert rejected.status_code == 400

    from app.db import SessionLocal
    from app.models import ApprovalRequest, ApprovalStatus
    db = SessionLocal()
    try:
        req = db.get(ApprovalRequest, approval_id)
        assert req.workspace_id == a["workspace_id"]
        assert req.status == ApprovalStatus.PENDING.value
    finally:
        db.rollback()
        db.close()


def test_execution_records_scoped_to_workspace(client):
    a = _make_ws(client)
    b = _make_ws(client)

    ra = client.post(
        "/gateway/execute",
        json={"tool": "create_product_draft", "params": {"name": "A 执行", "price": 5}, "idempotency_key": "p37-a"},
        headers=a["headers"],
    )
    rb = client.post(
        "/gateway/execute",
        json={"tool": "create_product_draft", "params": {"name": "B 执行", "price": 6}, "idempotency_key": "p37-b"},
        headers=b["headers"],
    )
    assert ra.json()["decision"] == "ALLOWED"
    assert rb.json()["decision"] == "ALLOWED"

    from app.db import SessionLocal
    from app.models import Execution, ExecutionStatus
    db = SessionLocal()
    try:
        a_exec = db.query(Execution).filter(
            Execution.workspace_id == a["workspace_id"],
            Execution.idempotency_key == "p37-a",
            Execution.status == ExecutionStatus.SUCCEEDED.value,
        ).first()
        b_exec = db.query(Execution).filter(
            Execution.workspace_id == b["workspace_id"],
            Execution.idempotency_key == "p37-a",
        ).first()
        assert a_exec is not None
        assert b_exec is None  # B's store never carries A's execution record
    finally:
        db.rollback()
        db.close()


def test_agent_memory_not_visible_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)

    client.put(
        f"/workspaces/{a['workspace_id']}/agent/memory",
        json={"key": "brand", "value": "甲品牌只做汉服", "kind": "fact"},
        headers=a["headers"],
    )
    b_mem = client.get(f"/workspaces/{b['workspace_id']}/agent/memory", headers=b["headers"]).json()
    values = [m.get("value") for m in b_mem["memories"]]
    assert "甲品牌只做汉服" not in values

    # B cannot delete A's memory by key, and same-key write stays separate.
    miss = client.delete(
        f"/workspaces/{b['workspace_id']}/agent/memory/brand", headers=b["headers"]
    )
    assert miss.status_code == 404

    client.put(
        f"/workspaces/{b['workspace_id']}/agent/memory",
        json={"key": "brand", "value": "乙品牌只做咖啡", "kind": "fact"},
        headers=b["headers"],
    )
    a_mem = client.get(f"/workspaces/{a['workspace_id']}/agent/memory", headers=a["headers"]).json()
    a_vals = [m.get("value") for m in a_mem["memories"]]
    assert "甲品牌只做汉服" in a_vals
    assert "乙品牌只做咖啡" not in a_vals
