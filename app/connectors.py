"""Connector layer: reserved interfaces with stub implementations.

Karma payment and e-commerce channels (independent site, social shop) are
exposed through a common contract so real integrations can replace the
stubs without changing the execution gateway. The Karma connector now calls
the real API when `KRAMA_API_KEY` is configured and falls back to a
deterministic stub otherwise. Stubs return verifiable receipts so the
verify-before-complete invariant is exercised end to end.
"""
from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ConnectorInstance, ConnectorType, Workspace
from . import signing


class Connector(Protocol):
    connector_type: str

    def execute(self, action: str, payload: dict) -> dict: ...

    def verify(self, action: str, reference: str) -> dict: ...


@dataclass
class KarmaPaymentConnector:
    """Payment via Karma. Uses the real API client when configured, else stubs."""

    connector_type = ConnectorType.KARMA_PAYMENT.value
    config: dict = field(default_factory=dict)

    def execute(self, action: str, payload: dict) -> dict:
        from .config import settings
        from .karma import KarmaClient, execute_action

        cfg = self.config or {}
        api_key = cfg.get("api_key") or settings.karma_api_key
        if not api_key:
            return execute_action(action, payload, payload.get("idempotency_key", ""))
        client = KarmaClient(
            base_url=cfg.get("base_url") or settings.karma_api_base_url,
            api_key=api_key,
            agent_id=cfg.get("agent_id") or "",
            identity_id=cfg.get("identity_id") or "",
            timeout=settings.function_timeout_seconds,
        )
        try:
            return execute_action(action, payload, payload.get("idempotency_key", ""), client=client)
        finally:
            client.close()

    def verify(self, action: str, reference: str) -> dict:
        from .config import settings
        from .karma import KarmaClient, verify_action

        cfg = self.config or {}
        api_key = cfg.get("api_key") or settings.karma_api_key
        if not api_key:
            return verify_action(action, reference)
        client = KarmaClient(
            base_url=cfg.get("base_url") or settings.karma_api_base_url,
            api_key=api_key,
            agent_id=cfg.get("agent_id") or "",
            identity_id=cfg.get("identity_id") or "",
            timeout=settings.function_timeout_seconds,
        )
        try:
            return verify_action(action, reference, client=client)
        finally:
            client.close()


def _headers_from_config(config: dict) -> dict:
    headers = dict((config or {}).get("headers") or {})
    auth = (config or {}).get("auth") or {}
    atype = (auth.get("type") or "none").lower()
    if atype == "bearer":
        token = auth.get("token") or (os.environ.get(auth.get("env", "")) if auth.get("env") else "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif atype == "api_key":
        token = auth.get("token") or os.environ.get(auth.get("env", ""), "")
        if token:
            headers[auth.get("header", "X-Api-Key")] = token
    elif atype == "basic":
        import base64

        raw = f"{auth.get('username', '')}:{auth.get('password', '')}"
        headers["Authorization"] = f"Basic {base64.b64encode(raw.encode()).decode()}"
    return headers


_SECRET_KEYS = ("app_secret", "checkword", "access_token")


def encrypt_config_secrets(config: dict | None) -> dict:
    """Fernet-encrypt channel credentials before persisting a connector config."""
    from . import crypto

    out = dict(config or {})
    for key in _SECRET_KEYS:
        if out.get(key):
            out[f"{key}_enc"] = crypto.encrypt(str(out.pop(key)))
    return out


def decrypt_config_secrets(config: dict | None) -> dict:
    """Decrypt channel credentials at request time (in memory only)."""
    from . import crypto

    out = dict(config or {})
    for key in _SECRET_KEYS:
        token = out.get(f"{key}_enc")
        if token:
            out[key] = crypto.decrypt(token)
    return out


@dataclass
class EcommerceConnector:
    """Independent site or social shop channel.

    When the connector instance carries an ``endpoint_url`` the order and
    cancel actions are executed as real SSRF-guarded HTTP calls; otherwise the
    deterministic stub with a verifiable receipt shape is used.
    """

    connector_type: str = ConnectorType.ECOMMERCE_INDEPENDENT.value
    config: dict = field(default_factory=dict)

    def _stub(self, action: str, payload: dict) -> dict:
        if action == "create_order":
            return {
                "channel": self.connector_type,
                "action": "create_order",
                "order_reference": f"ord_{payload.get('idempotency_key', '')[:16]}",
                "receipt_status": "SUCCEEDED",
                "note": "stub: replace with real channel API",
            }
        if action == "cancel_order":
            return {
                "channel": self.connector_type,
                "action": "cancel_order",
                "order_reference": payload.get("order_reference") or f"ord_{payload.get('idempotency_key', '')[:16]}",
                "receipt_status": "SUCCEEDED",
                "note": "stub: replace with real channel API",
            }
        if action == "ship":
            return {"channel": self.connector_type, "action": "ship", "receipt_status": "SUCCEEDED"}
        raise ValueError(f"unsupported e-commerce action: {action}")

    async def execute(self, action: str, payload: dict) -> dict:
        config = self.config or {}
        endpoint = config.get("endpoint_url") or ""
        if not endpoint or action not in ("create_order", "cancel_order"):
            return self._stub(action, payload)

        from .functions import _dot_get, _interpolate_url, recheck_url, request_http_safe, validate_url

        method = str(config.get("method") or ("GET" if action == "cancel_order" else "POST")).upper()
        url, remaining = _interpolate_url(endpoint, payload)
        allow_private = bool(config.get("allow_private"))
        url = validate_url(url, allow_private=allow_private)
        recheck_url(url, allow_private=allow_private)
        headers = _headers_from_config(config)
        json_body = remaining if method in ("POST", "PUT", "PATCH") else None
        query = None if method in ("POST", "PUT", "PATCH") else (remaining or None)
        resp = await request_http_safe(method, url, headers=headers, json_body=json_body, params=query)
        if resp.get("error"):
            return {
                "channel": self.connector_type,
                "action": action,
                "order_reference": payload.get("order_reference") or f"ord_{payload.get('idempotency_key', '')[:16]}",
                "receipt_status": "UNKNOWN",
                "needs_reconcile": True,
                "note": "外部接口超时/不可达，请先回查真实状态再决定重试",
                "verification": {"status": "UNKNOWN", "reference": None, "observed_status": None, "reason": "external_unreachable"},
            }
        body = resp.get("json") or {}

        rules = config.get("verification") or {}
        status_path = rules.get("status_path", "receipt_status")
        reference_path = rules.get("reference_path", "provider_reference")
        success_values = rules.get("success_values") or ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1")
        observed = _dot_get(body, status_path)
        reference = _dot_get(body, reference_path) or body.get("order_reference") or body.get("id")
        normalized = str(observed).upper() if observed is not None else "UNKNOWN"
        success = normalized in {str(v).upper() for v in success_values}
        return {
            "channel": self.connector_type,
            "action": action,
            "order_reference": reference or f"ord_{payload.get('idempotency_key', '')[:16]}",
            "receipt_status": normalized if success else "UNKNOWN",
            "http_status": resp.get("status"),
            "verification": {
                "status": "SUCCEEDED" if success else "UNKNOWN",
                "reference": reference,
                "observed_status": observed,
            },
        }

    def verify(self, action: str, reference: str) -> dict:
        return {"channel": self.connector_type, "reference": reference, "status": "SUCCEEDED"}


@dataclass
class ReservedConnector:
    """Reserved business-domain channel (takeout, marketplace, foreign trade,
    logistics, cross-border). These start as deterministic stubs with a
    verifiable receipt shape; the real API can be added later without
    changing the gateway contract.
    """

    connector_type: str = ConnectorType.LOGISTICS.value
    config: dict = field(default_factory=dict)

    def _stub(self, action: str, payload: dict) -> dict:
        reference = (
            payload.get("reference")
            or payload.get("order_reference")
            or payload.get("provider_reference")
            or f"{self.connector_type}_{payload.get('idempotency_key', '')[:16]}"
        )
        return {
            "channel": self.connector_type,
            "action": action,
            "provider_reference": reference,
            "receipt_status": "SUCCEEDED",
            "note": "stub: 预留接口，接入真实渠道后替换",
        }

    async def execute(self, action: str, payload: dict) -> dict:
        config = self.config or {}
        endpoint = config.get("endpoint_url") or ""
        if not endpoint:
            return self._stub(action, payload)
        from .functions import _dot_get, _interpolate_url, recheck_url, request_http_safe, validate_url

        method = str(config.get("method") or "POST").upper()
        url, remaining = _interpolate_url(endpoint, payload)
        allow_private = bool(config.get("allow_private"))
        url = validate_url(url, allow_private=allow_private)
        recheck_url(url, allow_private=allow_private)
        headers = _headers_from_config(config)
        json_body = remaining if method in ("POST", "PUT", "PATCH") else None
        query = None if method in ("POST", "PUT", "PATCH") else (remaining or None)
        resp = await request_http_safe(method, url, headers=headers, json_body=json_body, params=query)
        if resp.get("error"):
            return {
                "channel": self.connector_type,
                "action": action,
                "provider_reference": payload.get("provider_reference") or f"{self.connector_type}_{payload.get('idempotency_key', '')[:16]}",
                "receipt_status": "UNKNOWN",
                "needs_reconcile": True,
                "note": "外部接口超时/不可达，请先回查真实状态再决定重试",
                "verification": {"status": "UNKNOWN", "reference": None, "observed_status": None, "reason": "external_unreachable"},
            }
        body = resp.get("json") or {}
        rules = config.get("verification") or {}
        status_path = rules.get("status_path", "receipt_status")
        reference_path = rules.get("reference_path", "provider_reference")
        success_values = rules.get("success_values") or ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1")
        observed = _dot_get(body, status_path)
        reference = _dot_get(body, reference_path) or body.get("provider_reference") or body.get("id")
        normalized = str(observed).upper() if observed is not None else "UNKNOWN"
        success = normalized in {str(v).upper() for v in success_values}
        return {
            "channel": self.connector_type,
            "action": action,
            "provider_reference": reference or f"{self.connector_type}_{payload.get('idempotency_key', '')[:16]}",
            "receipt_status": normalized if success else "UNKNOWN",
            "http_status": resp.get("status"),
            "verification": {
                "status": "SUCCEEDED" if success else "UNKNOWN",
                "reference": reference,
                "observed_status": observed,
            },
        }

    def verify(self, action: str, reference: str) -> dict:
        return {"channel": self.connector_type, "reference": reference, "status": "SUCCEEDED"}


_SIGNED_INTERNAL_KEYS = frozenset(
    {"channel", "connector_type", "connector_id", "idempotency_key", "action", "signer", "verification"}
)


@dataclass
class SignedApiConnector:
    """Signed platform connector for Taobao TOP, Douyin and SF.

    Selected by a ``signer`` discriminator in the instance config; it reuses
    the existing reserved connector types so no DB/seed changes are needed.
    Credentials are Fernet-encrypted at rest and decrypted only at request
    time. Every response is verified through the same ``verification.*``
    rules as the other connectors.
    """

    connector_type: str
    signer: str
    config: dict = field(default_factory=dict)

    def _credentials(self) -> dict:
        return {
            "app_key": self.config.get("app_key") or "",
            "app_id": self.config.get("app_id") or "",
            "partner_id": self.config.get("partner_id") or "",
            "app_secret": self.config.get("app_secret") or "",
            "checkword": self.config.get("checkword") or "",
            "access_token": self.config.get("access_token") or "",
        }

    def _actions(self) -> dict:
        return self.config.get("actions") or {}

    def _api_method(self, action: str) -> str:
        entry = self._actions().get(action) or {}
        return str(entry.get("method") or self.config.get("api_method") or "").strip()

    def _service_code(self, action: str) -> str:
        entry = self._actions().get(action) or {}
        return str(entry.get("service_code") or self.config.get("service_code") or "").strip()

    def _read_configured(self) -> bool:
        if self.signer == "sfexpress":
            return bool(self._service_code("read"))
        return bool(self._api_method("read"))

    def _business(self, payload: dict) -> dict:
        out: dict = {}
        for key, value in (payload or {}).items():
            if key.startswith("_") or key in _SIGNED_INTERNAL_KEYS:
                continue
            out[key] = value
        return out

    def _build_request(self, action: str, payload: dict) -> tuple[str, str, dict]:
        import json
        import uuid

        creds = self._credentials()
        endpoint = str(self.config.get("endpoint_url") or "").strip()
        if not endpoint:
            raise ValueError("signed connector requires endpoint_url")
        business = self._business(payload)

        if self.signer == "taobao":
            api_method = self._api_method(action)
            if not api_method:
                raise ValueError("taobao connector requires actions.<action>.method")
            params = {
                "method": api_method,
                "app_key": creds["app_key"],
                "timestamp": signing.beijing_now_iso(),
                "format": "json",
                "v": "2.0",
                "sign_method": "md5",
            }
            if creds["access_token"]:
                params["session"] = creds["access_token"]
            params.update(business)
            params["sign"] = signing.sign_taobao(params, creds["app_secret"])
            return endpoint, "POST", {"form_body": params}

        if self.signer == "douyin":
            api_method = self._api_method(action)
            if not api_method:
                raise ValueError("douyin connector requires actions.<action>.method")
            sign_method = str(self.config.get("sign_method") or "hmac-sha256")
            params = {
                "method": api_method,
                "app_id": creds["app_id"],
                "timestamp": signing.unix_timestamp_seconds(),
                "param_json": json.dumps(business, ensure_ascii=False, separators=(",", ":")),
                "v": "2",
                "sign_method": sign_method,
            }
            if creds["access_token"]:
                params["access_token"] = creds["access_token"]
            params["sign"] = signing.sign_douyin(params, creds["app_secret"], sign_method)
            return endpoint, "POST", {"form_body": params}

        if self.signer == "sfexpress":
            service_code = self._service_code(action)
            if not service_code:
                raise ValueError("sfexpress connector requires actions.<action>.service_code")
            msg_data = json.dumps(business, ensure_ascii=False, separators=(",", ":"))
            timestamp = signing.unix_timestamp_millis()
            params = {
                "partnerID": creds["partner_id"],
                "requestID": f"krama{uuid.uuid4().hex}",
                "serviceCode": service_code,
                "timestamp": timestamp,
                "msgData": msg_data,
            }
            params["msgDigest"] = signing.sign_sf(msg_data, timestamp, creds["checkword"])
            return endpoint, "POST", {"form_body": params}

        raise ValueError(f"unsupported signer: {self.signer}")

    async def _request(self, action: str, payload: dict):
        from .functions import recheck_url, request_http, validate_url

        endpoint, http_method, transport = self._build_request(action, payload)
        allow_private = bool(self.config.get("allow_private"))
        endpoint = validate_url(endpoint, allow_private=allow_private)
        recheck_url(endpoint, allow_private=allow_private)
        resp = await request_http(http_method, endpoint, headers=_headers_from_config(self.config), **transport)
        body = resp.get("json")
        if not isinstance(body, (dict, list)):
            body = resp.get("text") or ""
        return resp, body

    def _parse_response(self, body, action: str) -> tuple[bool, str | None, object]:
        from .functions import _dot_get

        def dot(data, path):
            return _dot_get(data, path) if path and isinstance(data, dict) else None

        config = self.config or {}
        rules = config.get("verification") or {}
        if isinstance(body, dict) and rules.get("status_path"):
            observed = _dot_get(body, rules["status_path"])
            reference = dot(body, rules.get("reference_path") or "")
            values = rules.get("success_values") or ("SUCCEEDED", "SUCCESS", "OK", "PAID", "0")
            normalized = str(observed).upper() if observed is not None else None
            success = normalized in {str(v).upper() for v in values}
            return success, reference, observed
        if not isinstance(body, dict):
            return False, None, None
        reference_path = config.get("reference_path") or ""
        if self.signer == "taobao":
            if body.get("error_response") or body.get("errorResponse"):
                return False, None, body.get("error_response") or body.get("errorResponse")
            reference = dot(body, reference_path) or body.get("tid") or body.get("trade_id") or body.get("order_id")
            return True, reference, None
        if self.signer == "douyin":
            err_no = body.get("err_no", body.get("errno"))
            if err_no is not None and str(err_no) not in ("0",):
                return False, None, err_no
            reference = dot(body, reference_path) or body.get("order_id") or body.get("order_list")
            return True, reference, None
        if self.signer == "sfexpress":
            code = body.get("apiResultCode") or body.get("resultCode") or body.get("code")
            if code is not None and str(code).upper() not in ("A1000", "S0000", "0", "SUCCESS"):
                return False, None, code
            reference = dot(body, reference_path) or body.get("waybillNo") or body.get("mailNo")
            return True, reference, None
        return False, None, None

    async def execute(self, action: str, payload: dict) -> dict:
        try:
            resp, body = await self._request(action, payload)
        except Exception as exc:  # noqa: BLE001
            return {
                "channel": self.connector_type,
                "action": action,
                "signer": self.signer,
                "error": str(exc),
                "receipt_status": "UNKNOWN",
            }
        success, reference, observed = self._parse_response(body, action)
        return {
            "channel": self.connector_type,
            "action": action,
            "signer": self.signer,
            "provider_reference": reference or "",
            "order_reference": reference or "",
            "receipt_status": "SUCCEEDED" if success else "UNKNOWN",
            "http_status": resp.get("status"),
            "verification": {
                "status": "SUCCEEDED" if success else "UNKNOWN",
                "reference": reference,
                "observed_status": observed,
            },
        }

    async def read(self, params: dict | None) -> dict:
        if not self._read_configured():
            return {
                "connector_type": self.connector_type,
                "signer": self.signer,
                "connected": True,
                "status": "configured",
                "note": "signed channel configured; add actions.read method/service_code to read live receipts",
            }
        try:
            resp, body = await self._request("read", params or {})
        except Exception as exc:  # noqa: BLE001
            return {"connector_type": self.connector_type, "signer": self.signer, "connected": True, "error": str(exc)}
        if isinstance(body, str) and len(body) > 4000:
            body = body[:4000]
        return {
            "connector_type": self.connector_type,
            "signer": self.signer,
            "connected": True,
            "http_status": resp.get("status"),
            "body": body,
        }

    def verify(self, action: str, reference: str) -> dict:
        if not self._read_configured():
            return {
                "channel": self.connector_type,
                "signer": self.signer,
                "reference": reference,
                "status": "UNKNOWN",
                "note": "signed channel verify requires actions.read configured",
            }
        return {
            "channel": self.connector_type,
            "signer": self.signer,
            "reference": reference,
            "status": "UNKNOWN",
            "note": "signed channel verify requires a read call (execute verifies inline)",
        }


def _connector_class(connector_type: str, config: dict | None = None):
    config = config or {}
    signer = config.get("signer")
    if signer:
        if signer not in ("taobao", "douyin", "sfexpress"):
            raise ValueError(f"unknown signer: {signer}")
        return SignedApiConnector(connector_type, signer, config=config)
    if connector_type == ConnectorType.KARMA_PAYMENT.value:
        return KarmaPaymentConnector(config=config)
    if connector_type in (ConnectorType.ECOMMERCE_INDEPENDENT.value, ConnectorType.ECOMMERCE_SOCIAL.value):
        return EcommerceConnector(connector_type, config=config or {})
    if connector_type in (
        ConnectorType.TAKEOUT_DELIVERY.value,
        ConnectorType.ECOMMERCE_MARKETPLACE.value,
        ConnectorType.FOREIGN_TRADE.value,
        ConnectorType.LOGISTICS.value,
        ConnectorType.CROSS_BORDER.value,
    ):
        return ReservedConnector(connector_type, config=config or {})
    raise ValueError(f"unknown connector type: {connector_type}")


def install_connector(db: Session, workspace_id: str, connector_type: str, name: str, config: dict | None = None) -> ConnectorInstance:
    instance = db.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.workspace_id == workspace_id,
            ConnectorInstance.connector_type == connector_type,
        )
    ).scalars().first()
    if instance is None:
        instance = ConnectorInstance(
            workspace_id=workspace_id,
            connector_type=connector_type,
            name=name,
            config=config or {},
            status="active",
        )
        db.add(instance)
    else:
        instance.name = name or instance.name
        instance.config = config or {}
        instance.status = "active"
    db.flush()
    return instance


DEFAULT_CONNECTORS = (
    (ConnectorType.KARMA_PAYMENT.value, "Karma 收款"),
    (ConnectorType.ECOMMERCE_INDEPENDENT.value, "独立站"),
    (ConnectorType.ECOMMERCE_SOCIAL.value, "社交网店"),
    (ConnectorType.TAKEOUT_DELIVERY.value, "外卖配送"),
    (ConnectorType.ECOMMERCE_MARKETPLACE.value, "电商平台"),
    (ConnectorType.FOREIGN_TRADE.value, "外贸业务"),
    (ConnectorType.LOGISTICS.value, "物流管理"),
    (ConnectorType.CROSS_BORDER.value, "跨境电商"),
)


def ensure_default_connectors(db: Session, workspace_id: str) -> list[str]:
    """Install the standard payment/e-commerce channels if not already present.

    They start as deterministic stubs so the full order/payment chain works
    offline; each becomes a real HTTP call once the merchant configures the
    receipt URL or Karma credentials in settings.
    """
    existing = {
        row[0]
        for row in db.execute(
            select(ConnectorInstance.connector_type).where(ConnectorInstance.workspace_id == workspace_id)
        ).all()
    }
    created: list[str] = []
    for connector_type, name in DEFAULT_CONNECTORS:
        if connector_type in existing:
            continue
        db.add(
            ConnectorInstance(
                workspace_id=workspace_id,
                connector_type=connector_type,
                name=name,
                config={},
                status="active",
            )
        )
        created.append(connector_type)
    db.flush()
    return created


def _karma_config(db: Session, workspace_id: str) -> dict:
    """Workspace-level Karma credentials (api_key decrypted in memory only)."""
    ws = db.get(Workspace, workspace_id)
    karma = ((ws.settings or {}).get("karma") or {}) if ws else {}
    config = dict(karma)
    if karma.get("api_key_enc"):
        from . import crypto

        config["api_key"] = crypto.decrypt(karma["api_key_enc"])
    return config


async def execute_connector_action(
    db: Session,
    *,
    workspace_id: str,
    connector_type: str,
    action: str,
    payload: dict,
    idempotency_key: str,
) -> dict:
    instance = db.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.workspace_id == workspace_id,
            ConnectorInstance.connector_type == connector_type,
            ConnectorInstance.status == "active",
        )
    ).scalars().first()
    if instance is None:
        raise ValueError(f"connector {connector_type} is not installed in this workspace")
    config = decrypt_config_secrets(instance.config or {})
    if connector_type == ConnectorType.KARMA_PAYMENT.value:
        config = {**_karma_config(db, workspace_id), **config}
    connector = _connector_class(connector_type, config)
    result = connector.execute(action, {**payload, "idempotency_key": idempotency_key})
    if inspect.isawaitable(result):
        result = await result
    return {"connector_instance_id": instance.id, "connector_type": connector_type, **result}


def verify_connector_receipt(
    db: Session,
    *,
    workspace_id: str,
    connector_type: str,
    action: str,
    reference: str,
) -> dict:
    instance = db.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.workspace_id == workspace_id,
            ConnectorInstance.connector_type == connector_type,
        )
    ).scalars().first()
    config = decrypt_config_secrets((instance.config if instance else {}) or {})
    if connector_type == ConnectorType.KARMA_PAYMENT.value:
        config = {**_karma_config(db, workspace_id), **config}
    connector = _connector_class(connector_type, config)
    return connector.verify(action, reference)


# ---------------------------------------------------------------------------
# 独立站平台预设模板（Shopify / WooCommerce / 通用 Webhook）
# 真凭证到位即激活：配置 base_url + 密钥，系统自动补全 endpoint/鉴权/回执规则
# ---------------------------------------------------------------------------

def platform_preset(platform: str, config: dict | None = None) -> dict:
    """按平台生成 EcommerceConnector 标准配置。

    返回的配置会合并进用户提供的 config（用户可覆盖 endpoint/method/verification）。
    platform: "shopify" | "woocommerce" | "generic_webhook"
    """
    config = dict(config or {})
    platform = (platform or "generic_webhook").lower()
    base_url = str(config.get("base_url") or "").strip().rstrip("/")

    if platform == "shopify":
        # Shopify Admin REST API：POST /admin/api/2024-01/orders.json
        auth = config.get("auth") or {"type": "api_key", "header": "X-Shopify-Access-Token"}
        preset = {
            "platform": "shopify",
            "endpoint_url": f"{base_url}/admin/api/2024-01/orders.json" if base_url else "",
            "method": "POST",
            "auth": auth,
            "verification": {
                "status_path": "orders",
                "reference_path": "orders.0.id",
                "success_values": ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1"),
            },
        }
    elif platform == "woocommerce":
        # WooCommerce REST API：POST /wp-json/wc/v3/orders（Basic consumer_key:consumer_secret）
        cred = config.get("auth") or {}
        if cred.get("type") != "basic" and (cred.get("consumer_key") or config.get("consumer_key")):
            ck = cred.get("consumer_key") or config.get("consumer_key", "")
            cs = cred.get("consumer_secret") or config.get("consumer_secret", "")
            cred = {"type": "basic", "username": ck, "password": cs}
        preset = {
            "platform": "woocommerce",
            "endpoint_url": f"{base_url}/wp-json/wc/v3/orders" if base_url else "",
            "method": "POST",
            "auth": cred,
            "verification": {
                "status_path": "id",
                "reference_path": "id",
                "success_values": ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1"),
            },
        }
    else:
        # 通用 Webhook/API：用户自给 endpoint/method/headers/verification
        preset = {
            "platform": "generic_webhook",
            "endpoint_url": config.get("endpoint_url") or "",
            "method": str(config.get("method") or "POST").upper(),
            "headers": config.get("headers") or {},
            "verification": config.get("verification") or {
                "status_path": "receipt_status",
                "reference_path": "provider_reference",
                "success_values": ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1"),
            },
        }
    return {**config, **preset}


def normalize_webhook_order(platform: str, body: dict | list) -> dict:
    """把独立站推送的订单 JSON 归一化成系统 Order 落库结构。

    Shopify：{"orders":[{id,total_price,currency,customer,line_items}]}
    WooCommerce：{"id",total,currency,billing,line_items} 或单订单对象
    """
    platform = (platform or "").lower()
    data = body
    if isinstance(body, dict):
        if platform == "shopify":
            orders = body.get("orders") or [body]
        elif isinstance(body.get("id"), (int, str)):
            orders = [body]
        else:
            orders = [body]
    elif isinstance(body, list):
        orders = body
    else:
        orders = []

    def _one(raw: dict) -> dict:
        if platform == "shopify":
            total = raw.get("total_price", raw.get("total_price_set", {}).get("shop_money", {}).get("amount")) or 0
            currency = raw.get("currency") or raw.get("presentment_currency") or "USD"
            customer_raw = raw.get("customer") or {}
            line_items = raw.get("line_items") or []
        else:
            total = raw.get("total", raw.get("total_amount")) or 0
            currency = raw.get("currency") or "USD"
            customer_raw = raw.get("billing") or raw.get("customer") or {}
            line_items = raw.get("line_items") or raw.get("items") or []
        items = []
        for it in line_items:
            items.append({
                "product_id": it.get("product_id"),
                "name": it.get("title") or it.get("name") or "",
                "quantity": it.get("quantity") or it.get("qty") or 1,
                "price": it.get("price") or it.get("total") or 0,
            })
        email = customer_raw.get("email") or ""
        return {
            "channel": f"independent_{platform}" if platform else "independent",
            "provider_reference": str(raw.get("id") or ""),
            "status": "paid" if str(raw.get("financial_status", "paid")).lower() in ("paid", "processing") else "created",
            "payment_status": raw.get("financial_status") or raw.get("payment_status") or "paid",
            "total_amount": float(total or 0),
            "currency": currency,
            "customer": {
                "email": email,
                "name": (customer_raw.get("first_name", "") + " " + customer_raw.get("last_name", "")).strip(),
                "phone": customer_raw.get("phone") or "",
            },
            "items": items,
        }

    parsed = [_one(o) for o in orders if isinstance(o, dict)]
    if len(parsed) == 1:
        return parsed[0]
    return {"orders": parsed}


def verify_webhook_signature(secret: str, body_bytes: bytes, signature_header: str | None) -> bool:
    """HMAC-SHA256 webhook 验签：X-Krama-Signature（hex）或 X-Shopify-Hmac-SHA256。"""
    if not secret or not signature_header:
        return True  # 未配置 secret 时视为开放接收（提示用户补验签）
    import hashlib
    import hmac
    try:
        expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.lower().replace("sha256=", ""))
    except Exception:  # noqa: BLE001
        return False
