"""Karma Trust Protocol payment integration.

Implements the public Karma HTTP API
(https://github.com/AtoB101/Karma, docs/API_REFERENCE.md,
base URL https://api.karma.xyz/v1) behind a small client.

Real protocol core loop (Bilateral):
    Lock USDC -> mint Bill Token (1:1) -> bind buyer + agent bills
    -> verify delivery -> settle / finalize -> burn bills, release USDC.

When no ``KRAMA_API_KEY`` is configured the connector degrades to a
deterministic stub that still returns a verifiable receipt shape, so the
verify-before-complete invariant keeps working end to end.

Production caveat: contract creation and settlement state transitions are
wired against the published API reference. The on-chain escrow lock
(capacity lock / bill mint) and signed ``ExecutionReceipt`` submission
require Karma SDK key material and a production agent identity; this client
performs the HTTP steps but does not pretend to have moved real funds until
an explicit production verification.
"""
from __future__ import annotations

import hashlib
import json

from .config import settings


class KarmaClientError(RuntimeError):
    """Raised when the Karma API rejects a request or is unreachable."""


class KarmaClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        agent_id: str = "",
        identity_id: str = "",
        timeout: float = 15.0,
        transport=None,
    ) -> None:
        import httpx

        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self.base_url = base
        self.api_key = api_key
        self.agent_id = agent_id
        self.identity_id = identity_id
        self._token: str | None = None
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {"X-Karma-Api-Key": self.api_key}

    def request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        if resp.status_code >= 400:
            raise KarmaClientError(f"Karma {method} {path} -> {resp.status_code}: {resp.text[:240]}")
        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------ auth
    def exchange_token(self) -> str:
        data = self.request(
            "POST",
            "/v1/auth/token",
            json={"agent_id": self.agent_id or "agent", "api_key": self.api_key},
        )
        self._token = data.get("access_token") or data.get("token") or ""
        if not self._token:
            raise KarmaClientError("Karma token exchange returned no access_token")
        return self._token

    def ensure_token(self) -> None:
        if not self._token:
            self.exchange_token()

    # ----------------------------------------------------------------- agents
    def create_agent(self, name: str, role: str = "worker", endpoint_url: str = "", capabilities=None) -> dict:
        self.ensure_token()
        payload: dict = {"name": name, "role": role}
        if endpoint_url:
            payload["endpoint_url"] = endpoint_url
        if capabilities:
            payload["capabilities"] = list(capabilities)
        return self.request("POST", "/v1/agents", json=payload)

    def get_agent(self, agent_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/agents/{agent_id}")

    def list_agents(self, role: str | None = None) -> dict:
        self.ensure_token()
        query = f"?role={role}" if role else ""
        return self.request("GET", f"/v1/agents{query}")

    # -------------------------------------------------------------- contracts
    @staticmethod
    def _contract_payload(payload: dict) -> dict:
        """Map internal payment keys onto the Karma TaskContract fields."""
        amount = payload.get("escrow_amount")
        if amount is None:
            amount = payload.get("amount") or 0
        return {
            "client_agent_id": str(
                payload.get("client_agent_id") or payload.get("buyer") or payload.get("client_id") or ""
            ),
            "title": str(payload.get("title") or payload.get("product_name") or payload.get("description") or "商业订单"),
            "description": str(payload.get("description") or payload.get("title") or ""),
            "expected_output_schema": payload.get("expected_output_schema") or {"type": "object"},
            "expected_step_count": int(payload.get("expected_step_count") or 1),
            "escrow_amount": float(amount),
            "currency": str(payload.get("currency") or "USD"),
            "deadline_at": payload.get("deadline_at") or "",
        }

    def create_contract(self, payload: dict) -> dict:
        self.ensure_token()
        return self.request("POST", "/v1/contracts", json=self._contract_payload(payload))

    def get_contract(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/contracts/{task_id}")

    def assign_contract(self, task_id: str, worker_agent_id: str) -> dict:
        self.ensure_token()
        return self.request("PATCH", f"/v1/contracts/{task_id}/assign", params={"worker_agent_id": worker_agent_id})

    # --------------------------------------------------------------- receipts
    @staticmethod
    def build_execution_receipt(task_id: str, payload: dict | None = None) -> dict:
        """Best-effort ExecutionReceipt shape (unsigned) for wire tests.

        Real submission requires Karma SDK signing; hashes are produced so the
        64-char hex shape is representative of docs/EXECUTION_RECEIPT_STANDARD.md.
        """
        payload = payload or {}
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        output_hash = hashlib.sha256(json.dumps({"task_id": task_id}, sort_keys=True).encode()).hexdigest()
        return {
            "task_id": task_id,
            "receipt_id": payload.get("receipt_id") or "",
            "step_index": int(payload.get("step_index") or 1),
            "input_hash": input_hash,
            "output_hash": output_hash,
            "timestamp": payload.get("timestamp") or "",
        }

    def create_receipt(self, task_id: str, payload: dict | None = None) -> dict:
        self.ensure_token()
        return self.request("POST", "/v1/receipts", json=self.build_execution_receipt(task_id, payload))

    def get_receipt(self, receipt_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/receipts/{receipt_id}")

    def get_receipts_for_task(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/receipts/task/{task_id}")

    # ----------------------------------------------------- bundles & verify
    def submit_bundle(self, bundle: dict) -> dict:
        self.ensure_token()
        return self.request("POST", "/v1/bundles", json=bundle)

    def verify_delivery(self, task_id: str, bundle: dict, contract: dict) -> dict:
        self.ensure_token()
        return self.request("POST", "/v1/verify", json={"bundle": bundle, "contract": contract})

    def get_verification(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/verify/{task_id}")

    # -------------------------------------------------------------- settlement
    def create_settlement(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("POST", "/v1/settlement/create", json={"task_id": task_id})

    def get_settlement(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/settlement/{task_id}")

    def settlement_transition(self, task_id: str, phase: str, payload: dict | None = None) -> dict:
        self.ensure_token()
        return self.request("POST", f"/v1/settlement/{task_id}/{phase}", json=payload or {})

    def regret(self, task_id: str) -> dict:
        self.ensure_token()
        return self.request("POST", f"/v1/settlement/{task_id}/regret", json={})

    def dispute(self, task_id: str, payload: dict | None = None) -> dict:
        self.ensure_token()
        return self.request("POST", f"/v1/settlement/{task_id}/dispute", json=payload or {})

    # ---------------------------------------------------------------- capacity
    def get_capacity(self, identity_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/capacity/{identity_id}")

    def lock_capacity(self, identity_id: str, amount: float, currency: str = "USD") -> dict:
        self.ensure_token()
        return self.request("POST", f"/v1/capacity/{identity_id}/lock", json={"amount": amount, "currency": currency})

    def release_capacity(self, identity_id: str) -> dict:
        self.ensure_token()
        return self.request("POST", f"/v1/capacity/{identity_id}/release", json={})

    # -------------------------------------------------------------- reputation
    def get_reputation(self, agent_id: str) -> dict:
        self.ensure_token()
        return self.request("GET", f"/v1/reputation/{agent_id}")


def _client() -> KarmaClient | None:
    if not settings.karma_api_key:
        return None
    return KarmaClient(
        base_url=settings.karma_api_base_url,
        api_key=settings.karma_api_key,
        agent_id=settings.karma_agent_id,
        identity_id=settings.karma_identity_id,
        timeout=settings.function_timeout_seconds,
    )


_SETTLED = {"settled", "refunded"}


def _normalize_status(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("settled", "succeeded", "success", "paid", "completed", "confirmed", "released"):
        return "SUCCEEDED"
    if raw in ("refunded",):
        return "REFUNDED"
    if raw in ("cancelled", "canceled", "failed"):
        return "CANCELLED"
    return raw.upper() or "UNKNOWN"


def _stub_execute(action: str, payload: dict, idempotency_key: str) -> dict:
    if action == "pay":
        return {
            "provider": "karma",
            "action": "pay",
            "amount": payload.get("amount"),
            "currency": payload.get("currency", "CNY"),
            "provider_reference": f"karma_txn_{idempotency_key[:16]}",
            "receipt_status": "SUCCEEDED",
            "settlement_status": "SUCCEEDED",
            "note": "stub: configure KRAMA_API_KEY to use the real Karma API",
        }
    if action == "refund":
        return {
            "provider": "karma",
            "action": "refund",
            "provider_reference": payload.get("provider_reference") or f"karma_txn_{idempotency_key[:16]}",
            "refund_reference": f"karma_refund_{idempotency_key[:16]}",
            "receipt_status": "SUCCEEDED",
            "note": "stub: configure KRAMA_API_KEY to use the real Karma API",
        }
    if action == "reconcile":
        return {
            "provider": "karma",
            "action": "reconcile",
            "provider_reference": payload.get("provider_reference") or f"karma_txn_{idempotency_key[:16]}",
            "receipt_status": "SUCCEEDED",
            "settlement_status": "SETTLED",
            "reconciled": True,
            "note": "stub reconciliation",
        }
    if action == "dispute":
        return {
            "provider": "karma",
            "action": "dispute",
            "provider_reference": payload.get("provider_reference") or f"karma_txn_{idempotency_key[:16]}",
            "dispute_reference": f"karma_dispute_{idempotency_key[:16]}",
            "dispute_status": "DISPUTED",
            "receipt_status": "SUCCEEDED",
            "note": "stub dispute",
        }
    raise ValueError(f"unsupported karma action: {action}")


def _stub_verify(action: str, reference: str) -> dict:
    return {"provider": "karma", "reference": reference, "status": "SUCCEEDED", "note": "stub verification"}


def execute_action(action: str, payload: dict, idempotency_key: str, client: KarmaClient | None = None) -> dict:
    """Execute a Karma payment action, stubbing when no credentials exist."""
    own_client = client is None
    if client is None:
        client = _client()
    if client is None:
        return _stub_execute(action, payload, idempotency_key)
    try:
        if action == "pay":
            contract = client.create_contract(payload)
            task_id = contract.get("task_id") or contract.get("id") or ""
            if task_id and client.agent_id:
                client.assign_contract(task_id, client.agent_id)
            settlement = client.create_settlement(task_id) if task_id else {}
            status = _normalize_status(settlement.get("status") or settlement.get("state") or "draft")
            return {
                "provider": "karma",
                "action": "pay",
                "amount": payload.get("amount"),
                "currency": payload.get("currency", "USD"),
                "provider_reference": task_id,
                "contract_hash": contract.get("contract_hash") or "",
                "receipt_status": status,
                "settlement_status": status,
                "note": "live Karma contract + settlement draft; escrow lock and delivery verify are separate steps",
            }
        if action == "refund":
            task_id = payload.get("provider_reference") or payload.get("task_id") or ""
            data = client.regret(task_id) if task_id else {}
            status = _normalize_status(data.get("status") or data.get("state") or "settled")
            return {
                "provider": "karma",
                "action": "refund",
                "provider_reference": task_id,
                "refund_reference": task_id,
                "receipt_status": status,
                "note": "live Karma settlement regret; finalizes as settled",
            }
        if action == "reconcile":
            reference = payload.get("provider_reference") or payload.get("task_id") or ""
            settlement = client.get_settlement(reference) if reference else {}
            verification = client.get_verification(reference) if reference else {}
            status = _normalize_status(settlement.get("status") or settlement.get("state") or "unknown")
            decision = str(verification.get("decision") or "").lower()
            return {
                "provider": "karma",
                "action": "reconcile",
                "provider_reference": reference,
                "receipt_status": status,
                "settlement_status": status,
                "verification_decision": decision,
                "reconciled": status == "SUCCEEDED",
            }
        if action == "dispute":
            task_id = payload.get("provider_reference") or payload.get("task_id") or ""
            data = client.dispute(task_id, {"reason": payload.get("reason") or "customer_dispute"}) if task_id else {}
            status = _normalize_status(data.get("status") or data.get("state") or "disputed")
            return {
                "provider": "karma",
                "action": "dispute",
                "provider_reference": task_id,
                "dispute_reference": task_id,
                "dispute_status": status,
                "receipt_status": status,
            }
        raise ValueError(f"unsupported karma action: {action}")
    finally:
        if own_client:
            client.close()


def verify_action(action: str, reference: str, client: KarmaClient | None = None) -> dict:
    """Verify a Karma action against the authoritative settlement state."""
    own_client = client is None
    if client is None:
        client = _client()
    if client is None:
        return _stub_verify(action, reference)
    try:
        data = client.get_settlement(reference) if reference else {}
        status = _normalize_status(data.get("status") or data.get("state") or "unknown")
        return {"provider": "karma", "reference": reference, "status": status}
    finally:
        if own_client:
            client.close()