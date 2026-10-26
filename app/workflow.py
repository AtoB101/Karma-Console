"""Task orchestration: intent -> plan -> gateway -> verify -> report.

The model (or a caller) proposes steps; only the gateway executes them.
Tasks are resumable: completed steps are checkpointed, stale in-flight tasks
recover after a timeout, and failed side-effect steps are compensated in
reverse order (refunds, order cancellation) through the same gateway.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .gateway import GatewayError, GatewayResult, execute
from .models import Decision, Intent, RiskLevel, Task, TaskStatus
from .retry import NONE, PERMANENT, RETRYABLE, classify_step_failure, sleep_backoff
from .security import Actor

TERMINAL_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.CANCELLED.value,
}
RUNNABLE_STATUSES = {
    TaskStatus.CREATED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.PENDING_APPROVAL.value,
    TaskStatus.EXECUTING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.UNKNOWN.value,
}

# Side-effecting tools and the tool that reverses their effect.
COMPENSATION_TOOLS = {
    "make_payment": "make_refund",
    "create_order": "cancel_order",
}


def _is_idempotent(db: Session, actor: Actor, tool: str) -> bool:
    """Whether the tool declares itself idempotent (safe to retry).

    Built-in specs carry ``idempotent``; custom workspace functions fall back to
    their own declaration; unknown tools are treated as non-idempotent.
    """
    from .functions import build_function_spec
    from .tools import registry

    spec = registry.get(tool)
    if spec is not None:
        return bool(spec.idempotent)
    custom = build_function_spec(db, actor.workspace_id, tool)
    return bool(getattr(custom, "idempotent", False)) if custom is not None else False


def plan_steps(goal: str, steps: list[dict] | None = None) -> list[dict]:
    if steps:
        return steps
    lowered = goal.lower()
    if "支付" in goal or "pay" in lowered or "付款" in goal:
        return [{"tool": "make_payment", "params": {"amount": 0.0, "currency": "CNY"}}]
    if "下单" in goal or "order" in lowered:
        return [{"tool": "create_order", "params": {"channel": "ecommerce_independent"}}]
    if "文案" in goal or "营销" in goal or "copy" in lowered:
        return [{"tool": "generate_marketing_draft", "params": {"prompt": goal}}]
    if "商品" in goal or "product" in lowered:
        return [{"tool": "list_products", "params": {}}]
    return [{"tool": "workspace_summary", "params": {}}]


def create_task(
    db: Session,
    actor: Actor,
    goal: str,
    *,
    success_criteria: str = "",
    constraints: list | None = None,
    prohibited: list | None = None,
    budget_limit: float | None = None,
    risk_level: str | None = None,
    steps: list[dict] | None = None,
) -> Task:
    intent = Intent(
        workspace_id=actor.workspace_id,
        actor_id=actor.user.id,
        goal=goal,
        success_criteria=success_criteria,
        constraints=constraints or [],
        prohibited=prohibited or [],
        budget_limit=budget_limit,
        risk_level=risk_level or RiskLevel.R0.value,
    )
    db.add(intent)
    db.flush()
    task = Task(
        workspace_id=actor.workspace_id,
        actor_id=actor.user.id,
        intent_id=intent.id,
        status=TaskStatus.CREATED.value,
        plan=plan_steps(goal, steps),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _is_stale(task: Task, now=None) -> bool:
    if task.status in TERMINAL_STATUSES:
        return False
    updated = task.updated_at or task.created_at
    if updated is None:
        return False
    if updated.tzinfo is not None:
        updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    age = (now - updated).total_seconds()
    return age > settings.task_resume_timeout_seconds


def _step_done(step_result: dict) -> bool:
    if not isinstance(step_result, dict):
        return False
    # A conditionally skipped step is already "handled" and must not block resume.
    if step_result.get("status") == "skipped":
        return True
    if not step_result.get("execution_id"):
        return False
    result = step_result.get("result")
    if isinstance(result, dict) and result.get("error"):
        return False
    return True


# --- structured conditional branch (no eval, side-effect-free) --------------

def _dig(result: dict, dotted: str):
    cur = result
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _coerce_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eval_condition(cond: dict, context: dict, last: dict | None) -> bool:
    """Structured branch predicate over a prior step's result.

    ``cond``: ``{"tool"?: str, "field": str, "op": str, "value"?: any}``.
    The source result defaults to the last executed step; ``tool`` selects an
    earlier completed step by tool name. Ops: present/absent/truthy/contains/in
    /eq/ne/gt/gte/lt/lte. A malformed condition evaluates False (safe skip)
    rather than raising inside the engine.
    """
    if not isinstance(cond, dict):
        return True
    source_tool = cond.get("tool")
    source = None
    if source_tool and source_tool in context:
        source = context[source_tool].get("result")
    elif last is not None:
        source = last.get("result")
    if not isinstance(source, dict):
        source = {}
    field = cond.get("field")
    actual = _dig(source, field) if field else source
    op = str(cond.get("op") or "present").lower()
    expected = cond.get("value")

    if op == "present":
        return actual is not None
    if op == "absent":
        return actual is None
    if op == "truthy":
        return bool(actual)
    if op == "contains":
        try:
            return expected in actual
        except TypeError:
            return False
    if op == "in":
        try:
            return actual in expected
        except TypeError:
            return False
    a, b = _coerce_number(actual), _coerce_number(expected)
    if op in ("eq", "ne"):
        equal = (a is not None and b is not None and abs(a - b) < 1e-9) or actual == expected
        return equal if op == "eq" else not equal
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    return False


def _completed_prefix(plan: list[dict], previous: list[dict]) -> int:
    count = 0
    for index, step in enumerate(plan):
        checkpoint = previous[index] if index < len(previous) else None
        if not isinstance(checkpoint, dict):
            break
        if checkpoint.get("tool") != step.get("tool"):
            break
        if not _step_done(checkpoint):
            break
        count = index + 1
    return count


def _compensation_params(tool: str, step_result: dict) -> dict | None:
    result = step_result.get("result") if isinstance(step_result, dict) else None
    if not isinstance(result, dict):
        return None
    if tool == "make_payment":
        reference = result.get("provider_reference")
        return {"provider_reference": reference} if reference else None
    if tool == "create_order":
        reference = result.get("order_reference")
        if not reference:
            return None
        return {
            "channel": result.get("channel") or result.get("connector_type") or "ecommerce_independent",
            "order_reference": reference,
        }
    return None


async def _run_compensations(db: Session, actor: Actor, task: Task, completed_steps: list[dict]) -> dict:
    prior = (task.result or {}).get("compensation") or {}
    done = set(prior.get("done") or [])
    # Re-entry after approval must never repeat an already-applied reversal:
    # treat any prior item recorded as DONE as already compensated, not just
    # the explicit ``done`` set.
    for item in prior.get("items") or []:
        if item.get("status") == "DONE" and item.get("tool"):
            done.add(item["tool"])
    items = list(prior.get("items") or [])
    pending: list[dict] = []

    for step in reversed(completed_steps):
        tool = step.get("tool")
        if tool not in COMPENSATION_TOOLS or tool in done:
            continue
        params = _compensation_params(tool, step)
        if params is None:
            continue
        key = f"compensate:{task.id}:{tool}:{step.get('execution_id', '')}"
        result: GatewayResult = await execute(
            db, actor, COMPENSATION_TOOLS[tool], params,
            idempotency_key=key, task_id=task.id,
        )
        if result.decision is Decision.PENDING:
            pending.append({"tool": tool, "approval_id": result.approval_id, "params": params})
        elif result.decision is Decision.DENIED:
            items.append({"tool": tool, "status": "DENIED", "reason": result.reason, "params": params})
        else:
            done.add(tool)
            items.append({"tool": tool, "status": "DONE", "execution_id": result.execution_id, "params": params})

    return {
        "status": "PENDING" if pending else "DONE",
        "done": sorted(done),
        "items": items,
        "pending": pending,
    }


async def _fail_with_compensation(
    db: Session,
    actor: Actor,
    task: Task,
    completed_steps: list[dict],
    final_status: str,
    reason: str,
) -> Task:
    compensation = await _run_compensations(db, actor, task, completed_steps)
    task.error = reason
    if compensation["status"] == "PENDING":
        task.status = TaskStatus.PENDING_APPROVAL.value
        task.result = {"steps": completed_steps, "final_status": final_status, "compensation": compensation}
    else:
        task.status = final_status
        task.result = {"steps": completed_steps, "compensation": compensation}
    db.commit()
    db.refresh(task)
    return task


async def _finish_compensation(db: Session, actor: Actor, task: Task) -> Task:
    steps = (task.result or {}).get("steps") or []
    final_status = (task.result or {}).get("final_status") or TaskStatus.FAILED.value
    compensation = await _run_compensations(db, actor, task, steps)
    if compensation["status"] == "PENDING":
        task.status = TaskStatus.PENDING_APPROVAL.value
        task.result = {"steps": steps, "final_status": final_status, "compensation": compensation}
        db.commit()
        db.refresh(task)
        return task
    task.status = final_status
    task.error = task.error or "task failed; compensation applied"
    task.result = {"steps": steps, "compensation": compensation}
    db.commit()
    db.refresh(task)
    return task


async def execute_task(db: Session, actor: Actor, task_id: str) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.workspace_id != actor.workspace_id:
        raise GatewayError("task not found")

    if (task.result or {}).get("compensation", {}).get("status") == "PENDING":
        return await _finish_compensation(db, actor, task)

    if task.status not in RUNNABLE_STATUSES:
        raise GatewayError(f"task cannot run from status {task.status}")

    if _is_stale(task):
        task.error = "recovered after timeout; resuming from last checkpoint"
    task.status = TaskStatus.EXECUTING.value
    db.commit()

    previous = (task.result or {}).get("steps") or []
    completed_count = _completed_prefix(task.plan, previous)
    results: list[dict] = [dict(r) for r in previous[:completed_count] if isinstance(r, dict)]
    # Index of executed steps by tool name so a later branch can read an earlier
    # step's result; resumed steps are already present here.
    ctx_map: dict[str, dict] = {
        r["tool"]: r for r in results if isinstance(r, dict) and r.get("tool") and "result" in r
    }

    for index in range(completed_count, len(task.plan)):
        step = task.plan[index]
        tool = step["tool"]
        condition = step.get("condition") or step.get("if")
        last = results[-1] if results else None
        if condition is not None and not _eval_condition(condition, ctx_map, last):
            # Branch predicate false: skip deterministically, no execution,
            # no retry, no compensation; resume keeps this position handled.
            results.append({"tool": tool, "status": "skipped", "reason": "condition not met"})
            continue
        idem = _is_idempotent(db, actor, tool)
        attempt = 0
        kind = NONE
        result: GatewayResult | None = None
        while True:
            try:
                result = await execute(
                    db, actor, tool, step.get("params", {}),
                    idempotency_key=f"{task_id}:{tool}", task_id=task_id,
                )
            except GatewayError as exc:
                # Unknown tool / structural error: permanent, never retry.
                return await _fail_with_compensation(
                    db, actor, task, results, TaskStatus.FAILED.value, str(exc)
                )
            if result.decision is Decision.PENDING:
                task.status = TaskStatus.PENDING_APPROVAL.value
                task.result = {"steps": results, "pending": tool, "approval_id": result.approval_id}
                db.commit()
                db.refresh(task)
                return task
            if result.decision is Decision.DENIED:
                return await _fail_with_compensation(
                    db, actor, task, results, TaskStatus.BLOCKED.value, result.reason
                )

            kind = classify_step_failure(
                result.result, decision=result.decision.value, idempotent=idem
            )
            if kind != RETRYABLE or attempt >= settings.workflow_max_retries:
                break
            attempt += 1
            await sleep_backoff(
                attempt - 1,
                settings.workflow_retry_base_seconds,
                settings.workflow_retry_cap_seconds,
            )

        # Exhausted retries on a permanent error -> fail fast with compensation.
        if kind == PERMANENT and isinstance(result.result, dict) and result.result.get("error"):
            return await _fail_with_compensation(
                db, actor, task, results, TaskStatus.FAILED.value,
                str(result.result["error"]),
            )
        done_record = {
            "tool": tool,
            "execution_id": result.execution_id,
            "result": result.result,
            "attempts": attempt + 1,
        }
        results.append(done_record)
        ctx_map[tool] = done_record

    # Verify connector-backed steps are all confirmed.
    task.status = TaskStatus.VERIFYING.value
    db.commit()
    unverified = [
        r for r in results
        if isinstance(r.get("result"), dict)
        and r["result"].get("verification") is not None
        and r["result"]["verification"].get("status") != "SUCCEEDED"
    ]
    if unverified:
        return await _fail_with_compensation(
            db, actor, task, results, TaskStatus.UNKNOWN.value, "receipt verification unresolved"
        )
    task.status = TaskStatus.COMPLETED.value
    task.error = ""
    task.result = {"steps": results}
    db.commit()
    db.refresh(task)
    return task
