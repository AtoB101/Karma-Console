"""MCP-compatible JSON-RPC 2.0 server surface (minimal, local-only).

This module exposes the workspace's registered capabilities (built-in tools and
enabled user functions) through a JSON-RPC 2.0 envelope that a mainstream MCP
client / agent host can speak: ``initialize``, ``ping``, ``tools/list`` and
``tools/call``.

It is a compatibility *surface*, not a full MCP transport:

* Every ``tools/call`` invocation still flows through the unified
  :mod:`app.gateway` (permission -> region policy -> approval -> idempotency ->
  execution -> verification -> audit). The MCP layer can never grant rights or
  call an external system directly.
* No credentials (function ``headers``/``auth``/``endpoint_url`` secrets) are
  ever returned by ``tools/list``.
* stdio / SSE transports and a full MCP handshake negotiation are external
  integration concerns and remain registered in EXTERNAL_DEPENDENCIES.md; this
  layer only implements the POST JSON-RPC methods over the existing authenticated
  HTTP API.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .functions import effective_risk
from .models import Decision, WorkspaceFunction
from .security import Actor
from .tools import registry

# MCP protocol version label we claim compatibility with. We do not implement
# the full capability negotiation; this is advertised by ``initialize`` only.
MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "krama-console"
SERVER_VERSION = "1.0.0"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _schema_for_function(row: WorkspaceFunction) -> dict:
    raw = row.input_schema or {}
    if isinstance(raw, dict) and raw.get("type") == "object" and isinstance(raw.get("properties"), dict):
        return {"type": "object", "properties": raw["properties"], "additionalProperties": True}
    return _empty_schema()


def _builtin_catalog() -> list[dict]:
    tools = []
    for spec in registry.all():
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "inputSchema": _empty_schema(),
            "annotations": {
                "readOnlyHint": not spec.side_effect,
                "idempotentHint": spec.idempotent,
                "destructiveHint": False,
                "title": spec.name,
            },
            "_meta": {
                "source": "builtin",
                "risk_level": spec.risk_level.value,
                "side_effect": spec.side_effect,
                "idempotent": spec.idempotent,
            },
        })
    return tools


def _function_catalog(db: Session, actor: Actor) -> list[dict]:
    """Enabled user functions, excluding names already provided by built-ins.

    Built-ins win on name collision, mirroring gateway resolution order.
    """
    builtin_names = {t["name"] for t in _builtin_catalog()}
    rows = db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == actor.workspace_id,
            WorkspaceFunction.enabled.is_(True),
        )
    ).scalars().all()
    tools = []
    for row in rows:
        if row.name in builtin_names:
            continue
        risk = effective_risk(row)
        tools.append({
            "name": row.name,
            "description": row.description or f"custom function {row.name}",
            "inputSchema": _schema_for_function(row),
            "annotations": {
                "readOnlyHint": not row.side_effect,
                "idempotentHint": row.idempotent,
                "destructiveHint": False,
                "title": row.name,
            },
            "_meta": {
                "source": "function",
                "kind": row.kind,
                "risk_level": risk.value,
                "side_effect": row.side_effect,
                "idempotent": row.idempotent,
            },
        })
    return tools


def list_tools(db: Session, actor: Actor) -> list[dict]:
    return _builtin_catalog() + _function_catalog(db, actor)


def _initialize() -> dict:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "capabilities": {"tools": {"listChanged": False}},
    }


def _rpc_error(code: int, message: str, rpc_id: Any = None, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    payload: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if rpc_id is not None:
        payload["id"] = rpc_id
    return payload


def _rpc_result(result: Any, rpc_id: Any = None) -> dict:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "result": result}
    if rpc_id is not None:
        payload["id"] = rpc_id
    return payload


async def _tool_call(db: Session, actor: Actor, params: dict) -> dict:
    from .gateway import GatewayError, execute as gateway_execute

    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not name or not isinstance(name, str):
        return _rpc_error(INVALID_PARAMS, "tools/call requires a string 'name'")
    if not isinstance(arguments, dict):
        return _rpc_error(INVALID_PARAMS, "'arguments' must be an object")

    try:
        res = await gateway_execute(db, actor, name, arguments)
    except GatewayError as exc:
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
                "error": {"code": "UNKNOWN_TOOL", "message": str(exc)},
            },
        }

    decision = res.decision.value if hasattr(res.decision, "value") else str(res.decision)
    base: dict[str, Any] = {"gateway": {"decision": decision}}
    if res.execution_id:
        base["execution_id"] = res.execution_id
    if res.approval_id:
        base["approval_id"] = res.approval_id

    if decision == Decision.ALLOWED.value:
        text = json.dumps(res.result or {}, ensure_ascii=False)
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
                **base,
            },
        }
    if decision == Decision.PENDING.value:
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": f"待审批：{res.reason or '需要 owner 审批'}"}],
                "isError": True,
                **base,
            },
        }
    # DENIED
    return {
        "jsonrpc": "2.0",
        "result": {
            "content": [{"type": "text", "text": f"已拒绝：{res.reason or '安全策略拦截'}"}],
            "isError": True,
            **base,
        },
    }


async def handle_request(db: Session, actor: Actor, body: Any) -> dict:
    """Process a single JSON-RPC 2.0 request object and return a response."""
    if not isinstance(body, dict):
        return _rpc_error(PARSE_ERROR, "request must be a JSON object")
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if not isinstance(method, str) or not method:
        return _rpc_error(INVALID_REQUEST, "missing 'method'", rpc_id)

    if method == "initialize":
        return _rpc_result(_initialize(), rpc_id)
    if method == "ping":
        return _rpc_result({}, rpc_id)
    if method == "tools/list":
        return _rpc_result({"tools": list_tools(db, actor)}, rpc_id)
    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(INVALID_PARAMS, "'params' must be an object", rpc_id)
        response = await _tool_call(db, actor, params)
        if rpc_id is not None:
            response["id"] = rpc_id
        return response

    return _rpc_error(METHOD_NOT_FOUND, f"unknown method: {method}", rpc_id)
