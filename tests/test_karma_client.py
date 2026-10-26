import json

import httpx
import pytest

from app.karma import KarmaClient, KarmaClientError, execute_action, verify_action


def make_client(router, api_key="karma_test_secret"):
    return KarmaClient(
        base_url="https://api.karma.xyz/v1",
        api_key=api_key,
        agent_id="agent-1",
        identity_id="identity-1",
        transport=httpx.MockTransport(router),
    )


def test_token_exchange_uses_agent_id_and_access_token():
    seen = {}

    def router(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        if request.url.path == "/v1/auth/token":
            assert request.headers.get("X-Karma-Api-Key") == "karma_test_secret"
            assert json.loads(request.content) == {"agent_id": "agent-1", "api_key": "karma_test_secret"}
            return httpx.Response(200, json={"access_token": "jwt-abc", "token_type": "bearer"})
        assert request.headers.get("Authorization") == "Bearer jwt-abc"
        return httpx.Response(200, json={"ok": True})

    client = make_client(router)
    token = client.exchange_token()
    assert token == "jwt-abc"
    client.request("GET", "/v1/agents/agent-1")
    assert seen["path"] == "/v1/agents/agent-1"


def test_contract_payload_maps_internal_keys():
    payload = {"amount": 12.5, "currency": "CNY", "buyer": "customer-7", "title": "衬衫订单"}
    body = KarmaClient._contract_payload(payload)
    assert body["escrow_amount"] == 12.5
    assert body["currency"] == "CNY"
    assert body["client_agent_id"] == "customer-7"
    assert body["title"] == "衬衫订单"
    assert body["expected_step_count"] == 1


def test_pay_flow_creates_contract_assigns_worker_and_settlement(monkeypatch):
    calls = []

    def router(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append({"method": request.method, "path": request.url.path, "body": body, "query": dict(request.url.params), "auth": request.headers.get("Authorization")})
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "jwt-pay"})
        if request.url.path == "/v1/contracts":
            return httpx.Response(200, json={"task_id": "task-42", "contract_hash": "0xabc"})
        if request.url.path == "/v1/contracts/task-42/assign":
            return httpx.Response(200, json={"task_id": "task-42", "worker_agent_id": "agent-1"})
        if request.url.path == "/v1/settlement/create":
            return httpx.Response(200, json={"status": "draft"})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr("app.karma._client", lambda: make_client(router))
    result = execute_action("pay", {"amount": 12.5, "currency": "CNY", "buyer": "customer-7"}, "idem-1")

    assert result["provider_reference"] == "task-42"
    assert result["contract_hash"] == "0xabc"
    assert result["receipt_status"] == "DRAFT"
    assert result["settlement_status"] == "DRAFT"

    contract_call = next(c for c in calls if c["path"] == "/v1/contracts")
    assert contract_call["body"]["escrow_amount"] == 12.5
    assert contract_call["body"]["currency"] == "CNY"
    assert contract_call["body"]["client_agent_id"] == "customer-7"
    assert contract_call["auth"] == "Bearer jwt-pay"

    assign_call = next(c for c in calls if c["path"] == "/v1/contracts/task-42/assign")
    assert assign_call["query"] == {"worker_agent_id": "agent-1"}


def test_verify_action_reads_settlement(monkeypatch):
    def router(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "jwt-v"})
        if request.url.path == "/v1/settlement/task-42":
            return httpx.Response(200, json={"status": "settled"})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr("app.karma._client", lambda: make_client(router))
    assert verify_action("pay", "task-42")["status"] == "SUCCEEDED"


def test_refund_calls_regret_endpoint(monkeypatch):
    calls = []

    def router(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "jwt-r"})
        if request.url.path == "/v1/settlement/task-42/regret":
            return httpx.Response(200, json={"status": "settled"})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr("app.karma._client", lambda: make_client(router))
    result = execute_action("refund", {"provider_reference": "task-42"}, "idem-r")

    assert result["action"] == "refund"
    assert result["provider_reference"] == "task-42"
    assert result["receipt_status"] == "SUCCEEDED"
    assert ("POST", "/v1/settlement/task-42/regret") in [(m, p) for m, p, _ in calls]


def test_reconcile_reads_settlement_and_verification(monkeypatch):
    def router(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "jwt-c"})
        if request.url.path == "/v1/settlement/task-42":
            return httpx.Response(200, json={"status": "settled"})
        if request.url.path == "/v1/verify/task-42":
            return httpx.Response(200, json={"decision": "release", "confidence": 0.94})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr("app.karma._client", lambda: make_client(router))
    result = execute_action("reconcile", {"provider_reference": "task-42"}, "idem-c")

    assert result["settlement_status"] == "SUCCEEDED"
    assert result["verification_decision"] == "release"
    assert result["reconciled"] is True
    assert verify_action("reconcile", "task-42")["status"] == "SUCCEEDED"


def test_dispute_uses_settlement_dispute_endpoint(monkeypatch):
    calls = []

    def router(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": "jwt-d"})
        if request.url.path == "/v1/settlement/task-42/dispute":
            return httpx.Response(200, json={"status": "disputed"})
        if request.url.path == "/v1/settlement/task-42":
            return httpx.Response(200, json={"status": "disputed"})
        return httpx.Response(404, json={"error": "unknown"})

    monkeypatch.setattr("app.karma._client", lambda: make_client(router))
    result = execute_action("dispute", {"provider_reference": "task-42", "reason": "not_received"}, "idem-d")

    assert result["dispute_reference"] == "task-42"
    assert result["dispute_status"] == "DISPUTED"
    assert ("POST", "/v1/settlement/task-42/dispute") in [(m, p) for m, p, _ in calls]
    assert verify_action("dispute", "task-42")["status"] == "DISPUTED"


def test_lifecycle_stub_fallback():
    refund = execute_action("refund", {"provider_reference": "karma_txn_x"}, "stub-r")
    assert refund["receipt_status"] == "SUCCEEDED"
    assert refund["refund_reference"].startswith("karma_refund_")

    reconcile = execute_action("reconcile", {"provider_reference": "karma_txn_x"}, "stub-c")
    assert reconcile["reconciled"] is True
    assert reconcile["settlement_status"] == "SETTLED"

    dispute = execute_action("dispute", {"provider_reference": "karma_txn_x"}, "stub-d")
    assert dispute["dispute_status"] == "DISPUTED"
    assert verify_action("dispute", "ref")["status"] == "SUCCEEDED"


def test_api_error_raises_karma_client_error():
    def router(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    client = make_client(router)
    with pytest.raises(KarmaClientError):
        client.exchange_token()


def test_stub_fallback_without_credentials():
    # settings.karma_api_key is empty in the test environment.
    result = execute_action("pay", {"amount": 3.0}, "stub-1")
    assert result["provider"] == "karma"
    assert result["receipt_status"] == "SUCCEEDED"
    assert result["provider_reference"].startswith("karma_txn_")
    assert verify_action("pay", "ref")["status"] == "SUCCEEDED"


@pytest.mark.skipif(
    not __import__("os").getenv("RUN_KARMA_LIVE"),
    reason="set KRAMA_API_KEY and RUN_KARMA_LIVE=1 to run live smoke test",
)
def test_live_karma_smoke():
    from app.karma import KarmaClient

    from app.config import settings

    assert settings.karma_api_key, "KRAMA_API_KEY must be set for the live smoke test"
    client = KarmaClient(settings.karma_api_base_url, settings.karma_api_key, settings.karma_agent_id, settings.karma_identity_id)
    try:
        client.exchange_token()
    finally:
        client.close()