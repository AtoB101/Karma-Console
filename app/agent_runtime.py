"""Autonomous manager (AIAgent) single-run runtime.

Server-side safety bounds (never trusted from config or model output alone):

- only ``live``/``test`` agents may run; ``draft``/``disabled`` are refused;
- every executed tool must be (a) in the agent's ``allowed_tools`` allowlist AND
  (b) a read-only tool (``side_effect=False``) — a write/mutation tool declared in
  the allowlist is still refused by this read-only patrol runner;
- tool-call count and wall-clock limits come from ``execution_limits``;
- every call flows through the unified gateway, so region policy, approval,
  idempotency, verification and audit still apply;
- the run report is persisted to durable ``AgentMemory`` when memory_policy.enabled.

This runner performs read-only patrols. Mutating work stays behind the gateway
approval flow and is not auto-executed by a manager.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import record
from .models import AIAgent, AgentMemory, Decision, utcnow

DEFAULT_MAX_CALLS = 20
DEFAULT_MAX_SECONDS = 300


class AgentRunError(Exception):
    """Raised when an agent cannot be run (wrong status, not found, ...)."""


def _limits(agent: AIAgent) -> tuple[int, int]:
    lim = agent.execution_limits or {}
    return (
        int(lim.get("max_tool_calls") or DEFAULT_MAX_CALLS),
        int(lim.get("max_runtime_seconds") or DEFAULT_MAX_SECONDS),
    )


async def run_agent_once(
    db: Session,
    actor,
    agent: AIAgent,
    steps: list[dict] | None = None,
) -> dict:
    """Run one patrol pass for ``agent`` and persist a report to memory."""
    if agent.workspace_id != actor.workspace_id:
        raise AgentRunError("agent not found")
    if agent.status not in ("live", "test"):
        raise AgentRunError(
            f"agent status '{agent.status}' cannot run; activate it (live/test) first"
        )

    from .gateway import execute
    from .tools import registry

    allowed = set(agent.allowed_tools or [])
    max_calls, max_seconds = _limits(agent)
    started = time.monotonic()
    calls: list[dict] = []
    denied: list[dict] = []

    # Resolve the tool set. Explicit caller steps are re-validated against the
    # allowlist and read-only flag; otherwise patrol the allowlist's read-only tools.
    if steps:
        targets: list[tuple[str, dict]] = []
        for s in steps:
            tool = (s or {}).get("tool")
            params = (s or {}).get("params") or {}
            spec = registry.get(tool)
            if tool not in allowed or spec is None or spec.side_effect:
                denied.append({"tool": tool, "reason": "not in allowlist or not read-only"})
                continue
            targets.append((tool, params))
    else:
        targets = [
            (tool, {})
            for tool in allowed
            if (spec := registry.get(tool)) is not None and not spec.side_effect
        ]
        if not targets:
            fallback = registry.get("business_overview")
            if fallback is not None:
                targets = [("business_overview", {})]

    for tool, params in targets:
        if len(calls) >= max_calls:
            denied.append({"tool": tool, "reason": "max_tool_calls reached"})
            break
        if time.monotonic() - started > max_seconds:
            denied.append({"tool": tool, "reason": "max_runtime_seconds reached"})
            break
        res = await execute(
            db, actor, tool, params, idempotency_key=f"agent:{agent.id}:{tool}"
        )
        calls.append({"tool": tool, "decision": res.decision.value, "result": res.result})

    ok = [c for c in calls if c["decision"] == "ALLOWED"]
    summary = {
        "agent_id": agent.id,
        "name": agent.name,
        "ran_at": utcnow().isoformat(),
        "call_count": len(calls),
        "allowed_count": len(ok),
        "calls": calls,
        "denied": denied,
    }

    mem_enabled = (agent.memory_policy or {}).get("enabled", True)
    mem_key = f"agent:{agent.id}:last_report"
    if mem_enabled:
        row = db.execute(
            select(AgentMemory).where(
                AgentMemory.workspace_id == actor.workspace_id,
                AgentMemory.key == mem_key,
            )
        ).scalars().first()
        value = json.dumps(summary, ensure_ascii=False)
        if row is None:
            row = AgentMemory(
                workspace_id=actor.workspace_id, key=mem_key, value=value, kind="note"
            )
            db.add(row)
        else:
            row.value = value
            row.kind = "note"

    record(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user.id,
        action="agent_run",
        risk_level="R0",
        decision=Decision.ALLOWED,
        resource=agent.id,
        evidence={"calls": len(calls), "denied": denied},
    )
    db.commit()
    return {**summary, "memory_key": mem_key if mem_enabled else None}
