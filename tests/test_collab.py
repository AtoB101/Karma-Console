"""P73 M04 deterministic collaboration kernel: DAG order, cycle detection,
failure short-circuit, named handoff injection. No LLM, no gateway.
"""
from app.collab import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    plan_dag,
    resolve_handoff,
    run_collaboration,
)


def test_topological_order_and_handoff_injection():
    calls = []

    async def executor(node, prepared):
        calls.append((node["id"], prepared["steps"][0]["params"]))
        if node["id"] == "create_customer":
            return {"status": STATUS_OK, "result": {"customer_id": "c-123"}}
        return {"status": STATUS_OK, "result": {"order_id": "o-9"}}

    nodes = [
        {"id": "make_order", "role": "sales", "depends_on": ["create_customer"],
         "steps": [{"tool": "create_order",
                    "params": {"customer_id": {"$from": {"node": "create_customer", "field": "customer_id"}}}}]},
        {"id": "create_customer", "role": "sales", "depends_on": [],
         "steps": [{"tool": "upsert_customer", "params": {"name": "张三"}}]},
    ]
    import asyncio
    result = asyncio.run(run_collaboration(nodes, executor))

    assert result["ok"] is True
    # customer node ran before order node despite being listed second
    assert calls[0][0] == "create_customer"
    assert calls[1][0] == "make_order"
    # upstream customer_id handed into downstream params
    assert calls[1][1]["customer_id"] == "c-123"
    assert result["handoff"]["create_customer"]["customer_id"] == "c-123"


def test_failed_dependency_short_circuits_downstream():
    calls = []

    async def executor(node, prepared):
        calls.append(node["id"])
        if node["id"] == "a":
            return {"status": STATUS_FAILED, "result": {}, "error": "boom"}
        return {"status": STATUS_OK, "result": {}}

    nodes = [
        {"id": "a", "depends_on": [], "steps": []},
        {"id": "b", "depends_on": ["a"], "steps": []},
        {"id": "c", "depends_on": ["b"], "steps": []},
    ]
    import asyncio
    result = asyncio.run(run_collaboration(nodes, executor))

    assert calls == ["a"]  # b and c never executed
    statuses = {n["id"]: n["status"] for n in result["nodes"]}
    assert statuses["a"] == STATUS_FAILED
    assert statuses["b"] == STATUS_SKIPPED
    assert statuses["c"] == STATUS_SKIPPED


def test_cycle_detected_not_executed():
    order, errors = plan_dag([
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ])
    assert order is None
    assert any("cycle" in e for e in errors)


def test_unknown_dependency_reported():
    order, errors = plan_dag([{"id": "a", "depends_on": ["ghost"]}])
    assert order is None
    assert any("unknown dependency ghost" in e for e in errors)


def test_missing_handoff_field_warns_not_raises():
    warnings: list[str] = []
    out = resolve_handoff(
        {"customer_id": {"$from": {"node": "a", "field": "missing"}}},
        {"a": {"other": 1}},
        warnings,
    )
    assert out["customer_id"] is None
    assert any("handoff missing: a.missing" in w for w in warnings)


def test_independent_nodes_all_run():
    calls = []

    async def executor(node, prepared):
        calls.append(node["id"])
        return {"status": STATUS_OK, "result": {}}

    nodes = [
        {"id": "x", "depends_on": [], "steps": []},
        {"id": "y", "depends_on": [], "steps": []},
    ]
    import asyncio
    result = asyncio.run(run_collaboration(nodes, executor))
    assert sorted(calls) == ["x", "y"]
    assert all(n["status"] == STATUS_OK for n in result["nodes"])


def test_executor_exception_caught_node_failed():
    async def executor(node, prepared):
        raise RuntimeError("crash")

    nodes = [{"id": "a", "depends_on": [], "steps": []}]
    import asyncio
    result = asyncio.run(run_collaboration(nodes, executor))
    assert result["nodes"][0]["status"] == STATUS_FAILED
    assert "crash" in result["nodes"][0]["error"]
