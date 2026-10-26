"""Formal task-plan contract for the AI brain (plan chapter 3.3).

The model proposes a structured plan; the gateway executes it. This module is
the single source of truth for what a valid plan looks like, so malformed or
over-privileged shapes are rejected before execution.
"""
from __future__ import annotations

PLAN_REQUIRED = ("intent",)
STEP_REQUIRED = ("tool",)


def validate_agent_plan(data) -> tuple[bool, list[str], dict]:
    """Return (ok, errors, normalized). Never raises for malformed input."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["plan must be a JSON object"], {}

    intent = data.get("intent")
    if intent is None or not str(intent).strip():
        intent = "model"
    else:
        intent = str(intent)

    steps = data.get("steps")
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        return False, ["steps must be a list"], {}

    normalized_steps: list[dict] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        tool = step.get("tool")
        if not tool or not isinstance(tool, str):
            errors.append(f"steps[{index}] missing tool name")
            continue
        params = step.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            errors.append(f"steps[{index}].params must be an object")
            continue
        normalized_steps.append({"tool": tool, "params": params})

    reply = data.get("reply")
    clarify = data.get("clarify")
    done = data.get("done") in (True, "true", "True", 1)

    if errors:
        return False, errors, {}

    return True, [], {
        "intent": intent,
        "steps": normalized_steps,
        "reply": str(reply) if reply is not None else "",
        "clarify": str(clarify) if clarify is not None else "",
        "done": done,
    }


def lint_known_tools(steps: list[dict], known_tools: set[str]) -> list[str]:
    """Reject step tools that are not a registered tool or enabled function.

    Pure static check (no DB); the caller supplies the allowed name set. This
    catches a hallucinated tool name before any gateway round-trip.
    """
    errors: list[str] = []
    for index, step in enumerate(steps):
        tool = step.get("tool")
        if isinstance(tool, str) and tool not in known_tools:
            errors.append(
                f"steps[{index}] 工具「{tool}」不存在，只能使用可用工具/已启用功能列表里的名称"
            )
    return errors


def lint_params(steps: list[dict], allowed_params: dict[str, set[str]]) -> list[str]:
    """Reject params keys that a tool does not accept.

    ``allowed_params`` maps tool name -> set of legal param keys. Tools absent
    from the map are skipped (unknown-tool rejection is lint_known_tools' job).
    """
    errors: list[str] = []
    for index, step in enumerate(steps):
        tool = step.get("tool")
        params = step.get("params") or {}
        allowed = allowed_params.get(tool)
        if not isinstance(allowed, set):
            continue
        for key in params:
            if key not in allowed:
                legal = ", ".join(sorted(allowed)) or "（无参数）"
                errors.append(
                    f"steps[{index}] 工具「{tool}」不接受参数「{key}」，合法键：{legal}"
                )
    return errors


