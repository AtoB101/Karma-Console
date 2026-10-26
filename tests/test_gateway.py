def _exec(client, headers, tool, params=None, key=None):
    body = {"tool": tool, "params": params or {}}
    if key:
        body["idempotency_key"] = key
    return client.post("/gateway/execute", json=body, headers=headers)


def test_r0_allowed_without_approval(client, workspace):
    r = _exec(workspace["client"], workspace["headers"], "list_products")
    assert r.status_code == 200
    assert r.json()["decision"] == "ALLOWED"
    assert "products" in r.json()["result"]


def test_r1_create_draft_allowed(client, workspace):
    r = _exec(workspace["client"], workspace["headers"], "create_product_draft", {"name": "棉麻衬衫", "price": 99})
    assert r.json()["decision"] == "ALLOWED"
    assert r.json()["result"]["status"] == "draft"


def test_unknown_tool_denied(client, workspace):
    r = _exec(workspace["client"], workspace["headers"], "steal_money")
    assert r.status_code == 400


def test_payment_requires_approval_then_executes(client, workspace):
    h = workspace["headers"]
    client.post("/workspaces/x/connectors".replace("x", workspace["workspace_id"]),
                json={"connector_type": "karma_payment", "name": "Karma 收款"}, headers=h)
    first = _exec(workspace["client"], h, "make_payment", {"amount": 10.0}, key="pay-1")
    assert first.json()["decision"] == "PENDING"
    approval_id = first.json()["approval_id"]

    again = _exec(workspace["client"], h, "make_payment", {"amount": 10.0}, key="pay-1")
    assert again.json()["decision"] == "PENDING"
    assert again.json()["approval_id"] == approval_id

    r = workspace["client"].post(f"/approvals/{approval_id}/decision", json={"approve": True}, headers=h)
    assert r.json()["status"] == "APPROVED"

    done = _exec(workspace["client"], h, "make_payment", {"amount": 10.0}, key="pay-1")
    assert done.json()["decision"] == "ALLOWED"
    assert done.json()["result"]["receipt_status"] == "SUCCEEDED"
    assert done.json()["result"]["verification"]["status"] == "SUCCEEDED"


def test_payment_blocked_without_connector(client, workspace):
    r = _exec(workspace["client"], workspace["headers"], "make_payment", {"amount": 5.0})
    assert r.json()["decision"] == "PENDING"  # approval required first, connector checked at execution


def test_neural_escalate_blocks_policy_change_without_owner(client, workspace):
    """set_commerce_policy is R1 but the neural dispatcher marks it ESCALATE
    (policy-threshold change): the CEO must not self-execute it; it enters
    PENDING owner confirmation instead of running directly."""
    r = _exec(workspace["client"], workspace["headers"], "set_commerce_policy", {"max_discount_percent": 5})
    assert r.json()["decision"] == "PENDING"
    assert r.json().get("approval_id")
    assert "老板拍板" in r.json()["reason"] or "approval" in r.json()["reason"]


def test_neural_dispatch_allows_routine_readonly(client, workspace):
    """A routine AUTO/POLICY action without neural escalation still passes
    through the gateway directly."""
    r = _exec(workspace["client"], workspace["headers"], "list_products")
    assert r.json()["decision"] == "ALLOWED"

