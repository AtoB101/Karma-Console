"""P68 deterministic validation for the self-operated commerce closure.

Bad agent/user input must return a readable {"error": ...} through the gateway
instead of raising a native KeyError/ValueError/TypeError that surfaces as an
opaque message.
"""


def _exec(client, headers, tool, params, key=None):
    body = {"tool": tool, "params": params}
    if key:
        body["idempotency_key"] = key
    return client.post("/gateway/execute", json=body, headers=headers)


def _install(client, ws, headers, ctype, name):
    return client.post(f"/workspaces/{ws}/connectors", json={"connector_type": ctype, "name": name}, headers=headers)


# --- product draft ---------------------------------------------------------

def test_create_draft_requires_name(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    r = _exec(client, h, "create_product_draft", {"price": 9.9})
    assert r.json()["decision"] == "ALLOWED"
    assert "error" in r.json()["result"]
    assert "name" in r.json()["result"]["error"].lower()


def test_create_draft_rejects_negative_and_non_numeric_price(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    for bad in (-5, "abc"):
        r = _exec(client, h, "create_product_draft", {"name": "杯子", "price": bad})
        res = r.json()["result"]
        assert "error" in res, res
        assert "price" in res["error"].lower()


def test_update_product_rejects_bad_status_and_price(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    pid = client.post(f"/workspaces/{ws}/products", json={"name": "杯子", "price": 9.9}, headers=h).json()["id"]
    r = _exec(client, h, "update_product", {"product_id": pid, "status": "bogus"})
    assert "error" in r.json()["result"]
    r = _exec(client, h, "update_product", {"product_id": pid, "price": -1})
    assert "price" in r.json()["result"]["error"].lower()


def test_publish_requires_product_id(client, workspace):
    h = workspace["headers"]
    r = _exec(client, h, "publish_product", {})
    assert "error" in r.json()["result"]
    assert "product_id" in r.json()["result"]["error"]


# --- stock ------------------------------------------------------------------

def test_add_stock_rejects_missing_non_positive_and_non_numeric(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    pid = client.post(f"/workspaces/{ws}/products", json={"name": "杯子", "price": 9.9}, headers=h).json()["id"]
    for bad in (None, 0, -3, "abc"):
        r = _exec(client, h, "add_stock", {"product_id": pid, "quantity": bad})
        res = r.json()["result"]
        assert "error" in res, res
        assert "quantity" in res["error"].lower()


# --- order ------------------------------------------------------------------

def test_create_order_empty_items_passes_through_and_bad_lines_warn(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201
    # Empty items: the channel already built the order; pass through, no error.
    r = _exec(client, h, "create_order", {"channel": "ecommerce_independent"})
    res = r.json()["result"]
    assert "error" not in res
    assert res.get("order_reference")
    # Explicit non-numeric quantity line -> warning, no exception, no local error.
    r = _exec(client, h, "create_order", {
        "channel": "ecommerce_independent",
        "items": [{"name": "杯子", "quantity": "abc"}],
    })
    res = r.json()["result"]
    assert "error" not in res
    assert any("quantity" in w.lower() for w in res.get("warnings", []))


# --- customer ---------------------------------------------------------------

def test_update_customer_rejects_unknown_stage(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    created = _exec(client, h, "upsert_customer", {"external_id": "u-x", "name": "小王"}).json()["result"]
    cid = created["customer_id"]
    r = _exec(client, h, "update_customer", {"customer_id": cid, "stage": "platinum"})
    res = r.json()["result"]
    assert "error" in res and "stage" in res["error"].lower()


def test_upsert_customer_accepts_valid_stage(client, workspace):
    h = workspace["headers"]
    r = _exec(client, h, "upsert_customer", {"external_id": "u-y", "name": "小李", "stage": "lead", "profile": {"tag": "vip"}})
    res = r.json()["result"]
    assert res["stage"] == "lead"
    assert res["profile"] == {"tag": "vip"}


# --- delivery ---------------------------------------------------------------

def test_mark_delivered_requires_an_id(client, workspace):
    h = workspace["headers"]
    r = _exec(client, h, "mark_delivered", {})
    res = r.json()["result"]
    assert "error" in res and "required" in res["error"].lower()


# --- HTTP panel schema (Pydantic 422) ---------------------------------------

def test_http_product_create_rejects_empty_name_and_negative_price(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    r = client.post(f"/workspaces/{ws}/products", json={"name": "  ", "price": 1}, headers=h)
    assert r.status_code == 422
    r = client.post(f"/workspaces/{ws}/products", json={"name": "杯子", "price": -2}, headers=h)
    assert r.status_code == 422


def test_http_product_patch_rejects_unknown_status(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    pid = client.post(f"/workspaces/{ws}/products", json={"name": "杯子", "price": 9.9}, headers=h).json()["id"]
    r = client.patch(f"/workspaces/{ws}/products/{pid}", json={"status": "bogus"}, headers=h)
    assert r.status_code == 422
