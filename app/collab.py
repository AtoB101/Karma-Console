"""M04 deterministic multi-agent collaboration kernel.

A declarative DAG of worker nodes replaces the implicit "run in array order"
assumption: each node declares ``depends_on`` upstream nodes, receives their
named outputs through a handoff map, and is skipped when a dependency did not
succeed. This layer is **pure orchestration** — it never calls the LLM or the
gateway itself; the caller injects an ``executor(node, ctx)`` that performs the
real (gateway-backed) work. That keeps the ordering, cycle detection, failure
short-circuit and handoff injection fully deterministic and unit-testable.

Node shape::

    {
      "id": "create_customer",
      "role": "sales",
      "instruction": "...",
      "depends_on": [],
      "steps": [{"tool": "upsert_customer", "params": {"name": "张三"}}]
    }

Handoff placeholder inside params::

    {"customer_id": {"$from": {"node": "create_customer", "field": "customer_id"}}}

The placeholder is replaced with the upstream node's result field; a missing
reference records a warning and leaves the value absent (never raises, never
evals).
"""
from __future__ import annotations

import inspect

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


def _dig(obj, dotted: str):
    cur = obj
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def plan_dag(nodes: list[dict]) -> tuple[list[str] | None, list[str]]:
    """Validate and topologically order node ids.

    Returns ``(order, errors)``; ``order`` is None when there are errors
    (duplicate id, unknown dependency, or a cycle) and the caller must not run.
    """
    errors: list[str] = []
    by_id: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            errors.append("node must be an object")
            continue
        nid = node.get("id")
        if not nid:
            errors.append("node missing id")
            continue
        if nid in by_id:
            errors.append(f"duplicate node id: {nid}")
            continue
        by_id[nid] = node

    deps: dict[str, list[str]] = {}
    for nid, node in by_id.items():
        ds = node.get("depends_on") or []
        if not isinstance(ds, list):
            errors.append(f"node {nid}: depends_on must be a list")
            ds = []
        clean = []
        for d in ds:
            if d not in by_id:
                errors.append(f"node {nid}: unknown dependency {d}")
            elif d == nid:
                errors.append(f"node {nid}: self-dependency")
            else:
                clean.append(d)
        deps[nid] = clean

    if errors:
        return None, errors

    indegree = {nid: len(deps[nid]) for nid in by_id}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, ds in deps.items():
        for d in ds:
            dependents[d].append(nid)

    ready = [nid for nid, deg in indegree.items() if deg == 0]
    order: list[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for child in dependents[nid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(order) != len(by_id):
        cyclic = [nid for nid, deg in indegree.items() if deg > 0]
        errors.append("dependency cycle among: " + ", ".join(sorted(cyclic)))
        return None, errors
    return order, []


def resolve_handoff(value, handoff: dict, warnings: list[str]):
    """Recursively replace ``{"$from": {node, field}}`` placeholders."""
    if isinstance(value, dict):
        if "$from" in value and isinstance(value["$from"], dict):
            spec = value["$from"]
            producer = spec.get("node")
            field = spec.get("field", "")
            source = handoff.get(producer)
            resolved = _dig(source, field) if source is not None else None
            if resolved is None:
                warnings.append(f"handoff missing: {producer}.{field}")
            return resolved
        return {k: resolve_handoff(v, handoff, warnings) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_handoff(v, handoff, warnings) for v in value]
    return value


def _prepare_node(node: dict, handoff: dict, warnings: list[str]) -> dict:
    """Deep-copy the node's steps with handoff placeholders resolved."""
    import copy

    prepared = copy.deepcopy(node)
    steps = prepared.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("params"), dict):
                step["params"] = resolve_handoff(step["params"], handoff, warnings)
    return prepared


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


async def run_collaboration(nodes: list[dict], executor) -> dict:
    """Execute validated nodes in topological order via the injected executor.

    ``executor(node, prepared) -> {"status": OK|FAILED, "result": {...},
    "actions": [...]}``. A node whose dependency did not return OK is skipped
    (short-circuit) instead of running blind.
    """
    order, errors = plan_dag(nodes)
    if errors:
        return {"ok": False, "errors": errors, "nodes": [], "handoff": {}}

    handoff: dict[str, dict] = {}
    outcomes: dict[str, dict] = {}
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and n.get("id")}

    for nid in order:
        node = by_id[nid]
        deps = node.get("depends_on") or []
        unsatisfied = [d for d in deps if outcomes.get(d, {}).get("status") != STATUS_OK]
        if unsatisfied:
            outcomes[nid] = {
                "id": nid,
                "status": STATUS_SKIPPED,
                "reason": "deps not met: " + ", ".join(unsatisfied),
            }
            continue

        warnings: list[str] = []
        prepared = _prepare_node(node, handoff, warnings)
        try:
            out = await _maybe_await(executor(node, prepared))
        except Exception as exc:  # noqa: BLE001 - one node must not kill the run
            out = {"status": STATUS_FAILED, "result": {}, "error": str(exc)}
        if not isinstance(out, dict):
            out = {"status": STATUS_FAILED, "result": {}, "error": "executor returned non-object"}

        status = out.get("status") or STATUS_OK
        record = {"id": nid, "status": status, "result": out.get("result") or {},
                  "actions": out.get("actions") or [], "warnings": warnings}
        if out.get("error"):
            record["error"] = out["error"]
        outcomes[nid] = record
        if status == STATUS_OK:
            handoff[nid] = out.get("result") or {}

    return {
        "ok": True,
        "errors": [],
        "nodes": [outcomes[nid] for nid in order],
        "handoff": handoff,
    }
