from app.brain import preflight_hints


def test_preflight_funds_hint():
    hints = preflight_hints("make_payment", {"amount": 10})
    codes = [h["code"] for h in hints]
    assert "funds_terminal" in codes


def test_preflight_irreversible_delete_hint():
    hints = preflight_hints("delete_product", {"product_id": "p1"})
    codes = [h["code"] for h in hints]
    assert "irreversible_delete" in codes


def test_preflight_sensitive_to_external_hint():
    hints = preflight_hints("reply_customer", {"phone": "13800000000", "text": "hi"})
    codes = [h["code"] for h in hints]
    assert "sensitive_to_external" in codes


def test_preflight_credential_exposure_hint():
    hints = preflight_hints("sync", {"api_key": "sk-secret"})
    codes = [h["code"] for h in hints]
    assert "credential_exposure" in codes


def test_preflight_clean_action_has_no_hints():
    hints = preflight_hints("list_products", {"limit": 10})
    assert hints == []


def test_preflight_mass_scope_hint():
    hints = preflight_hints("batch_update_prices", {"items": []})
    codes = [h["code"] for h in hints]
    assert "mass_scope" in codes
def test_brain_events_endpoint(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post("/gateway/execute", json={"tool": "list_products", "params": {}}, headers=h)
    assert r.status_code == 200
    r = client.get(f"/workspaces/{ws}/brain/events?limit=20", headers=h)
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["action"] == "list_products" for e in events)


def test_brain_preflight_endpoint(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(f"/workspaces/{ws}/brain/preflight", json={"action": "make_payment", "params": {"amount": 10}}, headers=h)
    assert r.status_code == 200
    codes = [x["code"] for x in r.json()["hints"]]
    assert "funds_terminal" in codes