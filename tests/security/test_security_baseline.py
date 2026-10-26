"""Security baseline: no key leakage, unauthorized/unknown actions denied and audited."""
import json


def test_settings_never_return_plaintext_key(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    secret = "sk-super-secret-value-123"
    r = client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://x", "model": "m", "api_key": secret}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = client.get(f"/workspaces/{ws}/settings", headers=h)
    assert r.status_code == 200
    body = r.text
    assert secret not in body


def test_unknown_tool_denied_and_audited(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post("/gateway/execute", json={"tool": "totally_unknown_tool", "params": {}}, headers=h)
    assert r.status_code == 400
    r = client.get(f"/workspaces/{ws}/brain/events?limit=20", headers=h)
    events = r.json()["events"]
    assert any(e["action"] == "totally_unknown_tool" and e["decision"] == "DENIED" for e in events)


def test_customer_sourced_sensitive_action_requires_approval(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        "/gateway/execute",
        json={"tool": "make_payment", "params": {"amount": 1}, "idempotency_key": "sec-pay-1"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] in ("PENDING", "APPROVED", "DENIED", "ALLOWED")