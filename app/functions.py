"""User-defined workspace functions.

Users assemble their own capabilities (receipt URL readers, inventory feeds,
Karma payment actions, model prompts, HTTP writebacks) as data rows instead of
hardcoded tools. Each function is turned into a gateway-routed ToolSpec at
execution time, so permission, region policy, risk approval, idempotency,
verification and audit still apply to every invocation.

Network safety: outbound URLs are validated and private/reserved hosts are
blocked by default (SSRF guard). Users opt in per-function via
``config.allow_private`` or globally via ``FUNCTION_ALLOW_PRIVATE=1``.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import RiskLevel, WorkspaceFunction
from .risk import rank
from .tools import ToolContext, ToolSpec

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
KIND_MIN_RISK = {
    "http_read": RiskLevel.R0,
    "model": RiskLevel.R1,
    "agent": RiskLevel.R1,
    "http_write": RiskLevel.R2,
    "karma": RiskLevel.R3,
}
DEFAULT_SUCCESS_VALUES = ("SUCCEEDED", "SUCCESS", "OK", "PAID", "COMPLETED", "TRUE", "1")


class FunctionError(ValueError):
    pass


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise FunctionError("function name must match ^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    return name


def effective_risk(function: WorkspaceFunction) -> RiskLevel:
    floor = KIND_MIN_RISK.get(function.kind, RiskLevel.R0)
    try:
        declared = RiskLevel(function.risk_level)
    except ValueError:
        declared = floor
    return RiskLevel(f"R{max(rank(declared), rank(floor))}")


def is_private_host(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return True
    if host.startswith("["):
        host = host.strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    if host == "metadata.google.internal":
        return True
    return False


def resolve_has_private(host: str) -> bool:
    """Resolve the host and report whether any address is private/reserved."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    for info in infos:
        ip = info[4][0]
        if is_private_host(ip):
            return True
    return False


def validate_url(url: str, allow_private: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FunctionError("function URL scheme must be http or https")
    if not parsed.hostname:
        raise FunctionError("function URL must include a hostname")
    exempt = allow_private or settings.function_allow_private
    if is_private_host(parsed.hostname) and not exempt:
        raise FunctionError(f"function URL host {parsed.hostname!r} is private and blocked (SSRF guard)")
    if not exempt and resolve_has_private(parsed.hostname):
        raise FunctionError(f"function URL {parsed.hostname!r} resolves to a private address (SSRF guard)")
    return url


def recheck_url(url: str, allow_private: bool = False) -> str:
    """Re-resolve immediately before the request to shrink the DNS rebinding window."""
    parsed = urllib.parse.urlparse(url)
    exempt = allow_private or settings.function_allow_private
    if parsed.hostname and not exempt and resolve_has_private(parsed.hostname):
        raise FunctionError(f"function URL {parsed.hostname!r} resolved to a private address on recheck (SSRF guard)")
    return url


def _interpolate_url(endpoint_url: str, params: dict) -> tuple[str, dict]:
    remaining = dict(params or {})
    url = endpoint_url

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key in remaining:
            value = remaining.pop(key)
            return urllib.parse.quote(str(value), safe="")
        return match.group(0)

    url = re.sub(r"\{([a-zA-Z0-9_]+)\}", repl, url)
    return url, remaining


def _dot_get(data: dict, path: str):
    if not path or not isinstance(data, dict):
        return data if not path else None
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        else:
            return None
    return current


def build_headers(function: WorkspaceFunction) -> dict:
    headers = dict(function.headers or {})
    auth = function.auth or {}
    atype = (auth.get("type") or "none").lower()
    if atype == "none" or not atype:
        return headers
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


async def request_http(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
    form_body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Default transport. Replaceable in tests."""
    import httpx

    if json_body is not None and form_body is not None:
        raise FunctionError("request_http: use either json_body or form_body, not both")
    async with httpx.AsyncClient(timeout=timeout or settings.function_timeout_seconds) as client:
        resp = await client.request(method, url, headers=headers, json=json_body, params=params, data=form_body)
    parsed = None
    text = resp.text
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            parsed = None
    return {"status": resp.status_code, "headers": dict(resp.headers), "json": parsed, "text": text}


async def request_http_safe(method: str, url: str, **kwargs) -> dict:
    """Transport that never raises on timeout/unreachable.

    Returns ``{"error": "external_unreachable", ...}`` so callers can
    reconcile against the real external state instead of auto-failing.
    """
    import httpx

    try:
        return await request_http(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return {"status": None, "headers": {}, "json": None, "text": "", "error": "external_unreachable", "detail": str(exc)}


def apply_verification(function: WorkspaceFunction, body: dict, result: dict) -> dict:
    rules = function.verification or {}
    if not rules or rules.get("type") == "none":
        return result
    status_path = rules.get("status_path", "receipt_status")
    reference_path = rules.get("reference_path", "provider_reference")
    success_values = rules.get("success_values", list(DEFAULT_SUCCESS_VALUES))
    observed = _dot_get(body, status_path)
    reference = _dot_get(body, reference_path)
    normalized = str(observed).upper() if observed is not None else None
    success = normalized in {str(v).upper() for v in success_values}
    result["verification"] = {
        "status": "SUCCEEDED" if success else ("UNKNOWN" if normalized is not None else "MISSING"),
        "reference": reference,
        "observed_status": observed,
    }
    return result


async def execute_http_function(function: WorkspaceFunction, ctx: ToolContext) -> dict:
    method = (function.method or "GET").upper()
    if method not in HTTP_METHODS:
        raise FunctionError(f"unsupported HTTP method: {method}")
    if function.kind == "http_read" and method != "GET":
        raise FunctionError("http_read functions may only use GET")
    url, remaining = _interpolate_url(function.endpoint_url or ctx.params.get("url", ""), ctx.params)
    allow_private = bool((function.config or {}).get("allow_private"))
    url = validate_url(url, allow_private=allow_private)
    recheck_url(url, allow_private=allow_private)
    headers = build_headers(function)

    json_body = None
    query = None
    if method in ("POST", "PUT", "PATCH"):
        json_body = remaining
    else:
        query = remaining or None

    resp = await request_http_safe(method, url, headers=headers, json_body=json_body, params=query)
    if resp.get("error"):
        return {
            "function_id": function.id,
            "name": function.name,
            "kind": function.kind,
            "method": method,
            "url": url,
            "receipt_status": "UNKNOWN",
            "needs_reconcile": True,
            "note": "外部接口超时/不可达，请先回查真实状态再决定重试",
            "verification": {"status": "UNKNOWN", "reference": None, "observed_status": None, "reason": "external_unreachable"},
        }
    result: dict = {
        "function_id": function.id,
        "name": function.name,
        "kind": function.kind,
        "method": method,
        "url": url,
        "status_code": resp["status"],
    }
    if resp["json"] is not None:
        result["data"] = resp["json"]
        result = apply_verification(function, resp["json"], result)
    else:
        result["text"] = (resp["text"] or "")[:4000]
    return result


def execute_model_function(function: WorkspaceFunction, ctx: ToolContext) -> dict:
    from .llm import generate, provider_name

    prompt = ctx.params.get("prompt") or (function.config or {}).get("prompt", "")
    context = ctx.params.get("context") or json.dumps(ctx.params, ensure_ascii=False)
    text = generate(prompt=prompt, context=context)
    return {"function_id": function.id, "kind": "model", "provider": provider_name(), "text": text}


def execute_karma_function(function: WorkspaceFunction, ctx: ToolContext) -> dict:
    from .karma import execute_action, verify_action

    action = ctx.params.get("action", "pay")
    result = execute_action(action, ctx.params, ctx.idempotency_key)
    result["function_id"] = function.id
    reference = result.get("provider_reference")
    if reference:
        result["verification"] = verify_action(action, reference)
    else:
        result["verification"] = {"status": "MISSING", "reference": None}
    return result


def _agent_params(ctx: ToolContext) -> dict:
    nested = ctx.params.get("params")
    if isinstance(nested, dict):
        return nested
    params = dict(ctx.params or {})
    params.pop("params", None)
    params.pop("function_id", None)
    params.pop("name", None)
    return params


def _channel_context(db: Session, actor) -> str:
    from .models import ConnectorInstance

    rows = db.execute(
        select(ConnectorInstance).where(ConnectorInstance.workspace_id == actor.workspace_id)
    ).scalars().all()
    configured = [r for r in rows if (r.config or {}).get("endpoint_url")]
    if not configured:
        return "已配置渠道回执：无（读取回执会返回预留桩，请用本地数据并提示主人配置）\n"
    lines = [f"- {r.connector_type}（{r.name}）：{(r.config or {}).get('endpoint_url')}" for r in configured]
    return "已配置渠道回执（可用 get_channel_receipt(channel=...) 读取真实回执）：\n" + "\n".join(lines) + "\n"


async def execute_agent_function(function: WorkspaceFunction, ctx: ToolContext) -> dict:
    """Run an agent-kind function: a named, reusable business capability.

    The function's ``config`` carries the commercial standard (goal,
    instructions, acceptance, allowed tools). A bounded planner loop turns
    the caller's parameters plus the live business context into concrete
    gateway-routed steps, so every inner action is still permission/risk/
    approval/verification/audit protected.
    """
    from .gateway import execute as gateway_execute
    from .llm import generate, workspace_model_config

    config = function.config or {}
    model_config = workspace_model_config(ctx.db, ctx.actor.workspace_id)
    if not model_config:
        return {"function_id": function.id, "kind": "agent", "error": "尚未配置模型，无法执行该智能功能"}

    goal = config.get("goal") or function.description or function.name
    instructions = config.get("instructions") or ""
    acceptance = config.get("acceptance") or ""
    allowed = set(config.get("allowed_tools") or [])
    params = _agent_params(ctx)

    prompt = (
        f"你是功能「{config.get('display_name') or function.name}」的执行器。\n"
        f"功能目标：{goal}\n"
        + (f"商业执行标准（必须逐条遵守）：\n{instructions}\n" if instructions else "")
        + (f"验收标准：{acceptance}\n" if acceptance else "")
        + (f"只允许调用这些工具：{', '.join(sorted(allowed))}\n" if allowed else "可使用工作区内置工具。\n")
        + '只输出一个 JSON 对象：{"steps":[{"tool":"工具名","params":{}}],"reply":"给用户的一句话中文结果","done":true}。不要输出解释或 markdown。\n'
        + f"本次调用参数：{json.dumps(params, ensure_ascii=False)}\n"
    )
    from .chat import _business_context

    prompt += "业务上下文：\n" + _business_context(ctx.db, ctx.actor) + "\n" + _channel_context(ctx.db, ctx.actor)

    steps_log: list[dict] = []
    for _ in range(4):
        try:
            raw = generate(prompt=prompt, context=json.dumps(params, ensure_ascii=False), model_config=model_config)
        except Exception as exc:  # noqa: BLE001 - best-effort planner
            return {"function_id": function.id, "kind": "agent", "error": f"模型调用失败: {exc}"}
        plan = None
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                plan = json.loads(text[start:end + 1])
            except Exception:  # noqa: BLE001
                plan = None
        if not isinstance(plan, dict):
            return {"function_id": function.id, "kind": "agent", "error": "模型计划不可解析", "raw": raw[:400]}
        if plan.get("clarify"):
            return {"function_id": function.id, "kind": "agent", "clarify": plan["clarify"]}
        for step in plan.get("steps") or []:
            tool = step.get("tool")
            step_params = step.get("params") or {}
            if not isinstance(step_params, dict):
                step_params = {}
            if allowed and tool not in allowed:
                steps_log.append({"tool": tool, "decision": "DENIED", "reason": "不在该功能允许的工具范围内"})
                continue
            res = await gateway_execute(
                ctx.db, ctx.actor, tool, step_params,
                idempotency_key=f"{ctx.idempotency_key}:{len(steps_log)}",
            )
            entry = {"tool": tool, "decision": res.decision.value, "result": res.result}
            if res.approval_id:
                entry["approval_id"] = res.approval_id
            steps_log.append(entry)
        done = plan.get("done") in (True, "true", "True", 1)
        if done or plan.get("reply"):
            return {
                "function_id": function.id,
                "kind": "agent",
                "reply": plan.get("reply") or "已完成",
                "steps": steps_log,
                "acceptance": {"criteria": acceptance} if acceptance else {},
            }
        prompt += "\n\n【已执行】\n" + json.dumps(steps_log, ensure_ascii=False) + "\n请继续输出下一步 JSON。"
    return {"function_id": function.id, "kind": "agent", "reply": "已执行部分步骤。", "steps": steps_log}


async def execute_workspace_function(function: WorkspaceFunction, ctx: ToolContext) -> dict:
    if function.kind == "karma":
        return execute_karma_function(function, ctx)
    if function.kind == "model":
        return execute_model_function(function, ctx)
    if function.kind == "agent":
        return await execute_agent_function(function, ctx)
    if function.kind in ("http_read", "http_write"):
        return await execute_http_function(function, ctx)
    raise FunctionError(f"unsupported function kind: {function.kind}")


def get_workspace_function(db: Session, workspace_id: str, name: str) -> WorkspaceFunction | None:
    return db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == workspace_id,
            WorkspaceFunction.name == name,
            WorkspaceFunction.enabled.is_(True),
        )
    ).scalars().first()


def build_function_spec(db: Session, workspace_id: str, name: str) -> ToolSpec | None:
    function = get_workspace_function(db, workspace_id, name)
    if function is None:
        return None
    risk = effective_risk(function)

    async def handler(ctx: ToolContext) -> dict:
        return await execute_workspace_function(function, ctx)

    return ToolSpec(
        name=function.name,
        description=function.description or f"user function {function.name}",
        risk_level=risk,
        side_effect=function.side_effect or function.kind in ("http_write", "karma"),
        idempotent=function.idempotent,
        handler=handler,
    )
