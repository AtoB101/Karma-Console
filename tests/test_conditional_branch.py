"""P70 structured conditional branches: predicate evaluation, skip-on-false,
and checkpoint resume that treats a skipped step as already handled.

Linear plans without a ``condition`` behave exactly as before (backward
compatible). No eval/exec: predicates are structured op comparisons over a
prior step's result.
"""
import dataclasses

from app.workflow import _completed_prefix, _eval_condition


# --- pure predicate unit tests ----------------------------------------------

def test_eval_present_absent_truthy():
    ctx = {"a": {"result": {"v": 7}}}
    last = ctx["a"]
    assert _eval_condition({"field": "v", "op": "present"}, ctx, last) is True
    assert _eval_condition({"field": "missing", "op": "present"}, ctx, last) is False
    assert _eval_condition({"field": "v", "op": "absent"}, ctx, last) is False
    assert _eval_condition({"field": "missing", "op": "absent"}, ctx, last) is True
    assert _eval_condition({"field": "v", "op": "truthy"}, ctx, last) is True
    assert _eval_condition({"field": "v", "op": "eq", "value": 7}, ctx, last) is True
    assert _eval_condition({"field": "v", "op": "ne", "value": 8}, ctx, last) is True


def test_eval_numeric_comparisons():
    last = {"result": {"stock": 4}}
    assert _eval_condition({"field": "stock", "op": "lt", "value": 10}, {}, last) is True
    assert _eval_condition({"field": "stock", "op": "gt", "value": 3}, {}, last) is True
    assert _eval_condition({"field": "stock", "op": "gte", "value": 4}, {}, last) is True
    assert _eval_condition({"field": "stock", "op": "lte", "value": 4}, {}, last) is True
    assert _eval_condition({"field": "stock", "op": "gt", "value": 4}, {}, last) is False


def test_eval_reads_named_earlier_step():
    ctx = {"list_products": {"result": {"count": 5}}}
    last = {"result": {"count": 1}}
    # Select the earlier step explicitly instead of the last one.
    assert _eval_condition(
        {"tool": "list_products", "field": "count", "op": "gt", "value": 3}, ctx, last
    ) is True


def test_eval_contains_and_in():
    last = {"result": {"tags": ["vip", "repeat"], "tag": "vip"}}
    assert _eval_condition({"field": "tags", "op": "contains", "value": "vip"}, {}, last) is True
    assert _eval_condition({"field": "tags", "op": "contains", "value": "new"}, {}, last) is False
    assert _eval_condition({"field": "tag", "op": "in", "value": ["vip", "repeat"]}, {}, last) is True
    assert _eval_condition({"field": "tag", "op": "in", "value": ["new"]}, {}, last) is False


def test_malformed_condition_is_safe_false():
    # Non-dict condition passes through (no gate); bad op / missing source skips.
    assert _eval_condition(None, {}, None) is True
    assert _eval_condition({"field": "nope", "op": "gt", "value": 1}, {}, None) is False
    assert _eval_condition({"field": "stock", "op": "bogus", "value": 1}, {}, {"result": {"stock": 5}}) is False


# --- checkpoint prefix treats skipped as handled -----------------------------

def test_completed_prefix_counts_skipped_position():
    plan = [
        {"tool": "list_products"},
        {"tool": "workspace_summary", "condition": {"field": "x", "op": "gt", "value": 9}},
        {"tool": "list_orders"},
    ]
    previous = [
        {"tool": "list_products", "execution_id": "e1", "result": {"ok": True}},
        {"tool": "workspace_summary", "status": "skipped", "reason": "condition not met"},
    ]
    assert _completed_prefix(plan, previous) == 2


# --- end-to-end through the task engine --------------------------------------

def _patch_readonly(monkeypatch, tool, handler):
    from app.tools import registry
    spec = registry.get(tool)
    monkeypatch.setitem(registry._tools, tool, dataclasses.replace(spec, handler=handler))


def test_branch_executes_when_condition_met(client, workspace, monkeypatch):
    h, ws = workspace["headers"], workspace["workspace_id"]
    called = {"b": 0}

    _patch_readonly(monkeypatch, "list_products", lambda ctx: {"count": 5, "products": []})

    async def b_handler(_ctx):
        called["b"] += 1
        return {"ran": True}

    _patch_readonly(monkeypatch, "workspace_summary", b_handler)

    steps = [
        {"tool": "list_products", "params": {}},
        {"tool": "workspace_summary", "params": {},
         "condition": {"field": "count", "op": "gt", "value": 3}},
    ]
    tid = client.post(f"/workspaces/{ws}/tasks", json={"goal": "g", "steps": steps}, headers=h).json()["id"]
    body = client.post(f"/tasks/{tid}/run", headers=h).json()
    assert body["status"] == "COMPLETED"
    assert called["b"] == 1
    assert not any(s.get("status") == "skipped" for s in body["result"]["steps"])


def test_branch_skips_when_condition_false(client, workspace, monkeypatch):
    h, ws = workspace["headers"], workspace["workspace_id"]
    called = {"b": 0}

    _patch_readonly(monkeypatch, "list_products", lambda ctx: {"count": 1, "products": []})

    async def b_handler(_ctx):
        called["b"] += 1
        return {"ran": True}

    _patch_readonly(monkeypatch, "workspace_summary", b_handler)

    steps = [
        {"tool": "list_products", "params": {}},
        {"tool": "workspace_summary", "params": {},
         "condition": {"field": "count", "op": "gt", "value": 3}},
    ]
    tid = client.post(f"/workspaces/{ws}/tasks", json={"goal": "g", "steps": steps}, headers=h).json()["id"]
    body = client.post(f"/tasks/{tid}/run", headers=h).json()
    assert body["status"] == "COMPLETED"
    assert called["b"] == 0  # gated step never executed
    skipped = [s for s in body["result"]["steps"] if s.get("status") == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["tool"] == "workspace_summary"
