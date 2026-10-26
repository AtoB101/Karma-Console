"""Unified execution gateway.

This is the single choke point for every tool invocation. Models and
modules can never bypass it: permission, region policy, risk approval,
idempotency, verification and audit all happen here.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import record
from .models import (
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Execution,
    ExecutionStatus,
    Todo,
    Workspace,
)
from .policy import PolicyEngine
from .risk import requires_approval
from .security import Actor, can_execute, can_execute_reason
from .tools import ToolContext, registry


@dataclass
class GatewayResult:
    decision: Decision
    result: dict | None = None
    approval_id: str | None = None
    execution_id: str | None = None
    reason: str = ""


class GatewayError(Exception):
    pass


policy_engine = PolicyEngine()


def _workspace_region(db: Session, workspace_id: str) -> str:
    ws = db.get(Workspace, workspace_id)
    return ws.region if ws else "UNKNOWN"


async def execute(
    db: Session,
    actor: Actor,
    tool_name: str,
    params: dict,
    idempotency_key: str | None = None,
    task_id: str = "",
) -> GatewayResult:
    spec = registry.get(tool_name)
    if spec is None:
        from .functions import build_function_spec

        spec = build_function_spec(db, actor.workspace_id, tool_name)
    if spec is None:
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action=tool_name, risk_level="R0", decision=Decision.DENIED,
            reason="unknown tool",
        )
        db.commit()
        raise GatewayError(f"unknown tool: {tool_name}")

    risk = spec.risk_level.value

    denied = can_execute_reason(actor, risk)
    if denied:
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action=tool_name, risk_level=risk, decision=Decision.DENIED,
            reason=denied,
        )
        db.commit()
        return GatewayResult(Decision.DENIED, reason=denied)

    policy = policy_engine.evaluate(db, _workspace_region(db, actor.workspace_id), tool_name)
    if not policy.allowed:
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action=tool_name, risk_level=risk, decision=Decision.DENIED,
            reason="; ".join(policy.reasons),
        )
        db.commit()
        return GatewayResult(Decision.DENIED, reason="; ".join(policy.reasons))

    customer_sensitive = getattr(actor, "source", "console") == "customer" and int(risk[1]) >= 2
    # Neural dispatch gate: the dispatcher labels who owns an action (used for
    # audit evidence / plan). Only explicitly force-escalated actions (money
    # disbursement/refund/dispute, bulk repricing, policy-threshold change) are
    # held here below the R3 line; routine and read-only tools flow through.
    from . import nervous

    dispatch = nervous.assign(tool_name)
    neural_escalate = tool_name in nervous.FORCE_ESCALATE
    needs_approval = (
        requires_approval(risk)
        or policy.require_approval
        or customer_sensitive
        or neural_escalate
    )
    if needs_approval:
        key = idempotency_key or _make_idempotency(tool_name, params)
        approved = db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == actor.workspace_id,
                ApprovalRequest.action == tool_name,
                ApprovalRequest.idempotency_key == key,
                ApprovalRequest.status == ApprovalStatus.APPROVED.value,
            )
        ).scalars().first()
        pending = db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == actor.workspace_id,
                ApprovalRequest.action == tool_name,
                ApprovalRequest.idempotency_key == key,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
        ).scalars().first()
        if approved is None:
            if pending is None:
                from .notify import generate_code, notify_owner_for_workspace

                code = generate_code()
                req = ApprovalRequest(
                    workspace_id=actor.workspace_id,
                    action=tool_name,
                    risk_level=risk,
                    payload=params,
                    reason="; ".join(policy.reasons),
                    idempotency_key=key,
                    status=ApprovalStatus.PENDING.value,
                    confirm_code=code,
                )
                db.add(req)
                db.flush()
                pending_id = req.id
                try:
                    from .models import Todo

                    todo = Todo(
                        workspace_id=actor.workspace_id,
                        kind="must_confirm",
                        title=f"待确认：{tool_name}",
                        detail="; ".join(policy.reasons) or "需要主人确认",
                        status="pending",
                        confirm_code=code,
                        ref_type="approval",
                        ref_id=req.id,
                    )
                    db.add(todo)
                    notify_owner_for_workspace(
                        db,
                        actor.workspace_id,
                        f"[Krama] 待确认：{tool_name}",
                        f"动作 {tool_name} 需要主人确认。确认码：{code}",
                    )
                except Exception:  # noqa: BLE001 - notification/todo is best-effort
                    pass
            else:
                pending_id = pending.id
            record(
                db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
                action=tool_name, risk_level=risk, decision=Decision.PENDING,
                reason="neural escalate: 需老板拍板" if neural_escalate else "approval required",
                evidence={
                    "approval_id": pending_id,
                    "dispatch_owner": dispatch.get("owner_name"),
                    "dispatch_autonomy": dispatch.get("autonomy"),
                    "dispatch_plan_b": dispatch.get("plan_b"),
                },
            )
            db.commit()
            return GatewayResult(
                Decision.PENDING, approval_id=pending_id,
                reason="neural escalate: 需老板拍板" if neural_escalate else "approval required",
            )
        # Approved path falls through to execution.

    # Execution-side idempotent replay: a caller-supplied key that already
    # produced a SUCCEEDED result is replayed instead of re-executing the
    # side effect (prevents duplicate payments/refunds on retries).
    if idempotency_key:
        prior = db.execute(
            select(Execution).where(
                Execution.workspace_id == actor.workspace_id,
                Execution.tool_name == tool_name,
                Execution.idempotency_key == idempotency_key,
                Execution.status == ExecutionStatus.SUCCEEDED.value,
            )
        ).scalars().first()
        if prior is not None:
            record(
                db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
                action=tool_name, risk_level=risk, decision=Decision.ALLOWED,
                reason="idempotent replay",
                evidence={"execution_id": prior.id, "replay": True},
            )
            db.commit()
            return GatewayResult(
                Decision.ALLOWED, result=prior.output, execution_id=prior.id,
                reason="idempotent replay",
            )

    execution = Execution(
        workspace_id=actor.workspace_id,
        task_id=task_id,
        tool_name=tool_name,
        input=params,
        risk_level=risk,
        idempotency_key=idempotency_key or "",
        status=ExecutionStatus.RUNNING.value,
    )
    db.add(execution)
    db.flush()

    ctx = ToolContext(db=db, actor=actor, params=params, idempotency_key=execution.id)
    try:
        raw = spec.handler(ctx)
        if inspect.isawaitable(raw):
            raw = await raw
        result = raw if isinstance(raw, dict) else {"value": raw}
        result = _verify_if_connector(db, actor.workspace_id, tool_name, result)
        execution.output = result
        execution.status = ExecutionStatus.SUCCEEDED.value
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action=tool_name, risk_level=risk, decision=Decision.ALLOWED,
            resource=str(result.get("provider_reference") or result.get("order_reference") or ""),
            evidence={"execution_id": execution.id},
        )
        db.commit()
        return GatewayResult(Decision.ALLOWED, result=result, execution_id=execution.id)
    except Exception as exc:  # noqa: BLE001 - gateway must never hide failures
        execution.status = ExecutionStatus.FAILED.value
        execution.output = {"error": str(exc)}
        record(
            db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
            action=tool_name, risk_level=risk, decision=Decision.ALLOWED,
            reason=f"execution failed: {exc}", evidence={"execution_id": execution.id},
        )
        db.commit()
        return GatewayResult(Decision.ALLOWED, result={"error": str(exc)}, execution_id=execution.id)


def _verify_if_connector(db: Session, workspace_id: str, tool_name: str, result: dict) -> dict:
    action_by_tool = {
        "make_payment": "pay",
        "make_refund": "refund",
        "reconcile_payment": "reconcile",
        "dispute_payment": "dispute",
        "create_order": "create_order",
        "cancel_order": "cancel_order",
    }
    if tool_name not in action_by_tool:
        return result
    if result.get("verification"):
        return result
    connector_type = result.get("connector_type")
    reference = result.get("provider_reference") or result.get("order_reference")
    if not connector_type or not reference:
        return result
    from .connectors import verify_connector_receipt

    verification = verify_connector_receipt(
        db, workspace_id=workspace_id, connector_type=connector_type,
        action=action_by_tool[tool_name],
        reference=reference,
    )
    result["verification"] = verification
    if verification.get("status") != "SUCCEEDED":
        result["receipt_status"] = "UNKNOWN"
    return result


def approve(db: Session, actor: Actor, approval_id: str) -> ApprovalRequest:
    req = db.get(ApprovalRequest, approval_id)
    if req is None or req.workspace_id != actor.workspace_id:
        raise GatewayError("approval not found")
    if req.status != ApprovalStatus.PENDING.value:
        raise GatewayError(f"approval already {req.status}")
    req.status = ApprovalStatus.APPROVED.value
    _sync_approval_todo(db, approval_id, done=True)
    record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
        action=f"approve:{req.action}", risk_level=req.risk_level,
        decision=Decision.ALLOWED, resource=approval_id,
    )
    db.commit()
    db.refresh(req)
    return req


def reject(db: Session, actor: Actor, approval_id: str) -> ApprovalRequest:
    req = db.get(ApprovalRequest, approval_id)
    if req is None or req.workspace_id != actor.workspace_id:
        raise GatewayError("approval not found")
    req.status = ApprovalStatus.DENIED.value
    _sync_approval_todo(db, approval_id, done=False)
    record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
        action=f"reject:{req.action}", risk_level=req.risk_level,
        decision=Decision.DENIED, resource=approval_id,
    )
    db.commit()
    db.refresh(req)
    return req


def _sync_approval_todo(db: Session, approval_id: str, done: bool) -> None:
    """Keep the linked owner-facing todo in sync with the approval decision."""
    todo = db.execute(
        select(Todo).where(Todo.ref_type == "approval", Todo.ref_id == approval_id)
    ).scalars().first()
    if todo is not None and todo.status == "pending":
        todo.status = "done" if done else "cancelled"


def _make_idempotency(tool_name: str, params: dict) -> str:
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps({"tool": tool_name, "params": params}, sort_keys=True).encode()).hexdigest()
    return digest[:32]
