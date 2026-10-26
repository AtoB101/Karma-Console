from app.plan import validate_agent_plan


def test_valid_plan_passes():
    ok, errors, plan = validate_agent_plan({
        "intent": "create_order",
        "steps": [{"tool": "create_order", "params": {"quantity": 1}}],
        "reply": "ok",
        "done": True,
    })
    assert ok, errors
    assert plan["steps"][0]["tool"] == "create_order"


def test_malformed_step_rejected():
    ok, errors, _ = validate_agent_plan({"intent": "x", "steps": [{"params": {}}]})
    assert not ok
    assert any("tool" in e for e in errors)


def test_steps_not_list_rejected():
    ok, errors, _ = validate_agent_plan({"intent": "x", "steps": "not-a-list"})
    assert not ok


def test_missing_intent_defaults_to_model():
    ok, errors, plan = validate_agent_plan({"reply": "hi", "steps": []})
    assert ok, errors
    assert plan["intent"] == "model"


def test_non_object_rejected():
    ok, errors, _ = validate_agent_plan("nope")
    assert not ok
