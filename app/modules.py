"""Module marketplace: package functions, publish, and install across workspaces.

This is the prototype for user-created, publishable capabilities. Drafts stay
private; publishing strips embedded secrets so the marketplace never leaks
credentials. Installing copies the packaged function into the target
workspace as a ``WorkspaceFunction`` that still runs through the unified
gateway with the installer's own risk/permission context.

P3 adds a review state machine (draft -> pending_review -> approved/rejected
-> published) and a content fingerprint so a module changed after
submission must be resubmitted before it can be approved and published.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Module, ModuleStatus, ModuleVisibility, Role, WorkspaceFunction, utcnow
from .security import Actor

ROLE_RANK = {Role.VIEWER.value: 0, Role.MEMBER.value: 1, Role.OWNER.value: 2}

SENSITIVE_HEADER_KEYS = {"authorization", "x-api-key", "api-key", "token", "cookie", "set-cookie"}

# P4 lightweight sandbox: only configuration kinds are allowed; modules never
# carry executable code. Unknown kinds, code-execution fields, private URLs and
# non-strict schemas are rejected at create and install time.
ALLOWED_MODULE_KINDS = {"http_read", "http_write", "model", "agent", "karma"}
_FORBIDDEN_EXEC_KEYS = {
    "code", "script", "exec", "eval", "python", "shell", "command",
    "run", "entrypoint", "binary", "bytecode",
}
_SCHEMA_ALLOWED_KEYS = {"type", "properties", "required", "enum", "items", "description", "default", "title"}
_SCHEMA_TYPES = {"string", "number", "integer", "boolean", "object", "array"}


def _scan_no_exec_fields(obj, path="") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in _FORBIDDEN_EXEC_KEYS:
                raise ValueError(f"forbidden execution field: {path}{key}")
            _scan_no_exec_fields(value, path + f"{key}.")
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            _scan_no_exec_fields(value, f"{path}[{idx}].")


def _validate_strict_schema(node, path="schema") -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{path} must be a JSON object")
    for key in node:
        if key not in _SCHEMA_ALLOWED_KEYS:
            raise ValueError(f"{path}: disallowed schema key {key}")
    if "type" in node and node["type"] not in _SCHEMA_TYPES:
        raise ValueError(f"{path}: unsupported type {node['type']}")
    for child_key in ("properties", "items"):
        if child_key in node:
            children = node[child_key]
            iterable = children.values() if isinstance(children, dict) else children
            for child in iterable:
                _validate_strict_schema(child, f"{path}.{child_key}")


def validate_module_config(
    *, kind: str, endpoint_url: str, method: str,
    config: dict, input_schema: dict, auth: dict, headers: dict,
) -> None:
    """Reject non-configuration modules, private URLs, code fields, loose schemas."""
    if kind not in ALLOWED_MODULE_KINDS:
        raise ValueError(
            f"unsupported module kind: {kind} (allowed: {sorted(ALLOWED_MODULE_KINDS)})"
        )
    if kind in ("http_read", "http_write") and endpoint_url:
        from .functions import validate_url

        validate_url(endpoint_url)
    if kind == "http_read" and method and method.upper() != "GET":
        raise ValueError("http_read modules may only use GET")
    for container in (config or {}, input_schema or {}, auth or {}, headers or {}):
        _scan_no_exec_fields(container)
    if input_schema:
        _validate_strict_schema(input_schema)


def role_rank(role: Role | str) -> int:
    return ROLE_RANK.get(role.value if isinstance(role, Role) else role, 0)


def strip_secret(auth: dict) -> dict:
    cleaned = dict(auth or {})
    cleaned.pop("token", None)
    cleaned.pop("password", None)
    return cleaned


def sanitize_headers(headers: dict) -> dict:
    return {k: v for k, v in (headers or {}).items() if k.lower() not in SENSITIVE_HEADER_KEYS}


def sanitize_for_publish(module: Module) -> None:
    module.auth = strip_secret(module.auth)
    module.headers = sanitize_headers(module.headers)


def _apply_fields(module: Module, fields: dict) -> None:
    for key, value in fields.items():
        if key == "method" and value:
            value = str(value).upper()
        if value is not None:
            setattr(module, key, value)


def create_module(db: Session, actor: Actor, payload) -> Module:
    source_id = getattr(payload, "source_function_id", None)
    source = None
    if source_id:
        source = db.get(WorkspaceFunction, source_id)
        if source is None or source.workspace_id != actor.workspace_id:
            raise ValueError("source function not found in this workspace")

    module = Module(
        author_workspace_id=actor.workspace_id,
        name=payload.name,
        description=payload.description if payload.description else (source.description if source else ""),
        kind=payload.kind if payload.kind else (source.kind if source else "http_read"),
        category=payload.category if payload.category else (source.category if source else "general"),
        endpoint_url=payload.endpoint_url or (source.endpoint_url if source else ""),
        method=(payload.method or (source.method if source else "GET")).upper(),
        headers=payload.headers or (source.headers if source else {}),
        auth=payload.auth or (source.auth if source else {}),
        input_schema=payload.input_schema or (source.input_schema if source else {}),
        risk_level=payload.risk_level or (source.risk_level if source else "R0"),
        side_effect=payload.side_effect if payload.side_effect is not None else (source.side_effect if source else False),
        idempotent=payload.idempotent if payload.idempotent is not None else (source.idempotent if source else True),
        verification=payload.verification or (source.verification if source else {}),
        config=payload.config or (source.config if source else {}),
        min_role=payload.min_role or "member",
    )
    validate_module_config(
        kind=module.kind,
        endpoint_url=module.endpoint_url,
        method=module.method,
        config=module.config,
        input_schema=module.input_schema,
        auth=module.auth,
        headers=module.headers,
    )
    db.add(module)
    db.flush()
    return module


def publish_module(db: Session, actor: Actor, module: Module) -> Module:
    if actor.role is not Role.OWNER:
        raise PermissionError("only the workspace owner can publish modules")
    sanitize_for_publish(module)
    module.visibility = ModuleVisibility.PUBLISHED.value
    module.status = ModuleStatus.PUBLISHED.value
    db.flush()
    return module


def list_marketplace(db: Session, actor: Actor) -> list[Module]:
    return db.execute(
        select(Module).where(
            Module.visibility == ModuleVisibility.PUBLISHED.value,
            Module.status == ModuleStatus.PUBLISHED.value,
        ).order_by(Module.updated_at.desc())
    ).scalars().all()


def install_module(db: Session, actor: Actor, module: Module, overrides) -> WorkspaceFunction:
    if role_rank(actor.role) < role_rank(module.min_role):
        raise PermissionError(f"this module requires at least role {module.min_role} to install")
    if module.status != ModuleStatus.PUBLISHED.value:
        raise PermissionError("module is not published; only published modules can be installed")
    # Re-validate the packaged configuration at install time (defense in depth).
    validate_module_config(
        kind=module.kind,
        endpoint_url=module.endpoint_url,
        method=module.method,
        config=module.config,
        input_schema=module.input_schema,
        auth=module.auth,
        headers=module.headers,
    )

    name = overrides.name or module.name
    exists = db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == actor.workspace_id,
            WorkspaceFunction.name == name,
        )
    ).scalars().first()
    if exists is not None:
        raise ValueError(f"function {name} already exists in this workspace")

    function = WorkspaceFunction(
        workspace_id=actor.workspace_id,
        name=name,
        description=module.description,
        kind=module.kind,
        category=module.category,
        endpoint_url=overrides.endpoint_url or module.endpoint_url,
        method=module.method,
        headers=module.headers,
        auth=overrides.auth if overrides.auth is not None else module.auth,
        input_schema=module.input_schema,
        risk_level=module.risk_level,
        side_effect=module.side_effect,
        idempotent=module.idempotent,
        verification=module.verification,
        config={"installed_from_module": module.id, **module.config},
    )
    db.add(function)
    db.flush()
    return function


# --- review state machine (P3 supply-chain safety) -------------------------

def _content_fingerprint(module: Module) -> str:
    """Stable SHA-256 over the publishable, secret-free configuration."""
    payload = {
        "kind": module.kind,
        "category": module.category,
        "endpoint_url": module.endpoint_url,
        "method": module.method,
        "headers": sanitize_headers(module.headers),
        "input_schema": module.input_schema,
        "verification": module.verification,
        "risk_level": module.risk_level,
        "side_effect": module.side_effect,
        "idempotent": module.idempotent,
        "min_role": module.min_role,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest


def submit_for_review(db: Session, actor: Actor, module: Module) -> Module:
    """draft/rejected -> pending_review; freeze a content fingerprint."""
    if actor.role is not Role.OWNER:
        raise PermissionError("only the workspace owner can submit a module for review")
    if module.status not in (ModuleStatus.DRAFT.value, ModuleStatus.REJECTED.value):
        raise ValueError(f"module in status {module.status} cannot be submitted for review")
    sanitize_for_publish(module)
    module.config = {
        **module.config,
        "content_hash": _content_fingerprint(module),
        "submitted_at": utcnow().isoformat(),
    }
    module.status = ModuleStatus.PENDING_REVIEW.value
    db.flush()
    return module


def review_module(db: Session, actor: Actor, module: Module, decision: str, note: str = "") -> Module:
    """pending_review -> approved/rejected; reject changes made after submission."""
    if actor.role is not Role.OWNER:
        raise PermissionError("only the workspace owner can review modules")
    if module.status != ModuleStatus.PENDING_REVIEW.value:
        raise ValueError(f"module in status {module.status} is not pending review")
    submitted = module.config.get("content_hash")
    if submitted and submitted != _content_fingerprint(module):
        raise ValueError("module changed after submission; resubmit for review")
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    module.config = {
        **module.config,
        "reviewed_by": actor.user.id,
        "reviewed_at": utcnow().isoformat(),
        "review_note": note,
    }
    module.status = (
        ModuleStatus.APPROVED.value if decision == "approve" else ModuleStatus.REJECTED.value
    )
    db.flush()
    return module


def publish_approved(db: Session, actor: Actor, module: Module) -> Module:
    """approved -> published; only an approved module may enter the marketplace."""
    if actor.role is not Role.OWNER:
        raise PermissionError("only the workspace owner can publish modules")
    if module.status != ModuleStatus.APPROVED.value:
        raise ValueError("module must be approved before publishing")
    sanitize_for_publish(module)
    module.visibility = ModuleVisibility.PUBLISHED.value
    module.status = ModuleStatus.PUBLISHED.value
    db.flush()
    return module