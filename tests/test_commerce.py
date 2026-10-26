"""End-to-end commerce closure: order -> payment -> fulfilment -> delivery."""


def _gateway(client, headers, tool, params, key=None):
    body = {"tool": tool, "params": params}
    if key:
        body["idempotency_key"] = key
    return client.post("/gateway/execute", json=body, headers=headers)


def _install(client, ws_id, headers, ctype, name, config=None):
    return client.post(
        f"/workspaces/{ws_id}/connectors",
        json={"connector_type": ctype, "name": name, "config": config or {}},
        headers=headers,
    )


def _approve(client, headers, body):
    approval_id = body["approval_id"]
    r = client.post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=headers)
    assert r.json()["status"] == "APPROVED"
    return r


def test_order_to_delivery_full_chain(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]

    product = client.post(
        f"/workspaces/{ws}/products", json={"name": "棉麻衬衫", "category": "clothing", "price": 99.0}, headers=h
    ).json()

    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201

    r = _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 5}).json()
    assert r["decision"] == "ALLOWED"
    assert r["result"]["stock"] == 5

    r = _gateway(client, h, "create_order", {"channel": "ecommerce_independent", "items": [{"product_id": product["id"], "quantity": 2}]}).json()
    assert r["decision"] == "ALLOWED"
    order_id = r["result"]["order_id"]

    pay = _gateway(client, h, "make_payment", {"amount": 198.0, "currency": "CNY", "order_id": order_id}, key="pay-1").json()
    assert pay["decision"] == "PENDING"
    _approve(client, h, pay)
    pay_done = _gateway(client, h, "make_payment", {"amount": 198.0, "currency": "CNY", "order_id": order_id}, key="pay-1").json()
    assert pay_done["decision"] == "ALLOWED"

    ship = _gateway(client, h, "fulfill_order", {"order_id": order_id, "carrier": "顺丰", "tracking_number": "SF123"}).json()
    assert ship["decision"] == "ALLOWED"
    shipment_id = ship["result"]["shipment_id"]

    inv = _gateway(client, h, "list_inventory", {}).json()
    assert inv["result"]["inventory"][0]["stock"] == 3

    delivered = _gateway(client, h, "mark_delivered", {"shipment_id": shipment_id}).json()
    assert delivered["decision"] == "ALLOWED"

    orders = client.get(f"/workspaces/{ws}/orders", headers=h).json()
    assert orders["orders"][0]["status"] == "DELIVERED"
    assert orders["orders"][0]["payment_status"] == "paid"
    assert orders["orders"][0]["items"][0]["quantity"] == 2

    shipments = client.get(f"/workspaces/{ws}/shipments", headers=h).json()
    assert shipments["shipments"][0]["status"] == "DELIVERED"


def test_create_order_resolves_product_by_name(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "棉麻衬衫", "price": 99.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201
    _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 4}).json()

    order = _gateway(client, h, "create_order", {"channel": "ecommerce_independent", "items": [{"name": "棉麻衬衫", "quantity": 2}]}).json()
    assert order["decision"] == "ALLOWED"
    order_id = order["result"]["order_id"]
    detail = client.get(f"/workspaces/{ws}/orders/{order_id}", headers=h).json()
    assert detail["items"][0]["product_id"] == product["id"]
    assert detail["total_amount"] == 198.0

    pay = _gateway(client, h, "make_payment", {"amount": 198.0, "order_id": order_id}, key="pay-by-name").json()
    _approve(client, h, pay)
    _gateway(client, h, "make_payment", {"amount": 198.0, "order_id": order_id}, key="pay-by-name").json()

    ship = _gateway(client, h, "fulfill_order", {"order_id": order_id}).json()
    assert ship["decision"] == "ALLOWED"
    inv = _gateway(client, h, "list_inventory", {}).json()
    assert inv["result"]["inventory"][0]["stock"] == 2


def test_fulfill_order_blocks_insufficient_stock(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "牛仔裤", "price": 159.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201

    _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 1}).json()
    order = _gateway(client, h, "create_order", {"channel": "ecommerce_independent", "items": [{"product_id": product["id"], "quantity": 10}]}).json()
    order_id = order["result"]["order_id"]

    pay = _gateway(client, h, "make_payment", {"amount": 1590.0, "order_id": order_id}, key="pay-2").json()
    _approve(client, h, pay)
    _gateway(client, h, "make_payment", {"amount": 1590.0, "order_id": order_id}, key="pay-2").json()

    ship = _gateway(client, h, "fulfill_order", {"order_id": order_id}).json()
    assert ship["decision"] == "ALLOWED"
    assert "insufficient stock" in ship["result"]["error"]


def test_refund_marks_order_refunded(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "T恤", "price": 50.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201

    _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 3}).json()
    order = _gateway(client, h, "create_order", {"channel": "ecommerce_independent", "items": [{"product_id": product["id"], "quantity": 1}]}).json()
    order_id = order["result"]["order_id"]

    pay = _gateway(client, h, "make_payment", {"amount": 50.0, "order_id": order_id}, key="pay-3").json()
    _approve(client, h, pay)
    done = _gateway(client, h, "make_payment", {"amount": 50.0, "order_id": order_id}, key="pay-3").json()
    provider_reference = done["result"]["provider_reference"]

    refund = _gateway(client, h, "make_refund", {"provider_reference": provider_reference}, key="refund-1").json()
    assert refund["decision"] == "PENDING"
    _approve(client, h, refund)
    refund_done = _gateway(client, h, "make_refund", {"provider_reference": provider_reference}, key="refund-1").json()
    assert refund_done["decision"] == "ALLOWED"

    order_detail = client.get(f"/workspaces/{ws}/orders/{order_id}", headers=h).json()
    assert order_detail["status"] == "REFUNDED"
    assert order_detail["payment_status"] == "refunded"

def test_create_order_accepts_product_name_and_qty_variants(client, workspace):
    """Model field-name variance must not break the stock-decrement chain."""
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "卫衣", "price": 129.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201
    _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 6}).json()

    order = _gateway(client, h, "create_order", {"channel": "ecommerce_independent", "items": [{"product_name": "卫衣", "qty": 2}]}).json()
    assert order["decision"] == "ALLOWED"
    order_id = order["result"]["order_id"]
    detail = client.get(f"/workspaces/{ws}/orders/{order_id}", headers=h).json()
    assert detail["items"][0]["product_id"] == product["id"]
    assert detail["items"][0]["quantity"] == 2

    pay = _gateway(client, h, "make_payment", {"amount": 258.0, "order_id": order_id}, key="pay-variant").json()
    _approve(client, h, pay)
    _gateway(client, h, "make_payment", {"amount": 258.0, "order_id": order_id}, key="pay-variant").json()

    ship = _gateway(client, h, "fulfill_order", {"order_id": order_id}).json()
    assert ship["decision"] == "ALLOWED"
    inv = _gateway(client, h, "list_inventory", {}).json()
    assert inv["result"]["inventory"][0]["stock"] == 4


def test_fulfill_order_resolves_product_name_without_id(client, workspace):
    """Fulfilment must still decrement stock when order items lack product_id."""
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "夹克", "price": 299.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    assert _install(client, ws, h, "karma_payment", "Karma").status_code == 201
    _gateway(client, h, "add_stock", {"product_id": product["id"], "quantity": 5}).json()

    # Simulate a legacy order whose item only carries a name (no product_id).
    from app.db import SessionLocal
    from app.models import Order
    db = SessionLocal()
    try:
        order = Order(
            workspace_id=ws,
            channel="ecommerce_independent",
            order_reference="legacy-1",
            status="PAID",
            total_amount=299.0,
            currency="CNY",
            items=[{"name": "夹克", "quantity": 1, "unit_price": 299.0}],
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        order_id = order.id
    finally:
        db.close()

    ship = _gateway(client, h, "fulfill_order", {"order_id": order_id}).json()
    assert ship["decision"] == "ALLOWED"
    inv = _gateway(client, h, "list_inventory", {}).json()
    assert inv["result"]["inventory"][0]["stock"] == 4

def test_create_order_auto_links_customer_by_name(client, workspace):
    """One-shot order with only a customer name must still create a Customer row."""
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "棉麻衬衫", "price": 99.0}, headers=h).json()
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201

    order = _gateway(client, h, "create_order", {
        "channel": "ecommerce_independent",
        "items": [{"name": "棉麻衬衫", "quantity": 2}],
        "customer_name": "张三",
    }).json()
    assert order["decision"] == "ALLOWED"

    customers = client.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]
    assert any(c["name"] == "张三" for c in customers)
