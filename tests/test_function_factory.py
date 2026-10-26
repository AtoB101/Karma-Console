"""One-line request -> real executable function -> test -> promote -> auto-use."""
import json

import app.chat as chat
import app.llm as llm
from sqlalchemy import select


def _gw(client, headers, tool, params):
    return client.post("/gateway/execute", json={"tool": tool, "params": params}, headers=headers).json()


def test_create_function_lands_in_test_area(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _gw(client, h, "create_function", {
        "name": "auto_reply",
        "display_name": "自动回复询价",
        "description": "客户询价时自动介绍产品并报价",
        "category": "marketing",
        "kind": "agent",
        "config": {
            "goal": "自动回复客户询价",
            "instructions": "报价必须基于实时库存与售价",
            "acceptance": "能对询价生成准确报价且不泄露成本",
            "allowed_tools": ["list_products", "quote_product", "reply_customer"],
        },
    })
    assert r["decision"] == "ALLOWED"
    fn_id = r["result"]["function_id"]
    area_id = r["result"]["area_id"]
    assert r["result"]["stage"] == "test"

    fns = client.get(f"/workspaces/{ws}/functions", headers=h).json()["functions"]
    fn = next(f for f in fns if f["id"] == fn_id)
    assert fn["stage"] == "test"
    assert fn["display_name"] == "自动回复询价"
    assert fn["acceptance"] == "能对询价生成准确报价且不泄露成本"

    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    area = next(a for a in areas if a["id"] == area_id)
    assert area["stage"] == "test"
    assert area["target_id"] == fn_id


def test_promote_function_moves_to_live_and_becomes_callable(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _gw(client, h, "create_function", {
        "name": "quote_bot",
        "display_name": "报价机器人",
        "kind": "agent",
        "config": {"goal": "给客户报价", "instructions": "", "acceptance": "", "allowed_tools": []},
    })
    fn_id = r["result"]["function_id"]

    p = _gw(client, h, "promote_function", {"function_id": fn_id})
    assert p["decision"] == "ALLOWED"
    assert p["result"]["stage"] == "live"
    assert p["result"]["areas_promoted"] >= 1

    fns = client.get(f"/workspaces/{ws}/functions", headers=h).json()["functions"]
    assert next(f for f in fns if f["id"] == fn_id)["stage"] == "live"

    from app.db import SessionLocal
    from app.models import Role, User
    from app.security import Actor

    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.id == workspace["user_id"])).scalar_one()
        actor = Actor(user=user, workspace_id=ws, role=Role.OWNER)
        allowed = chat._allowed_tools(db, actor)
        assert "quote_bot" in allowed
    finally:
        db.close()


def test_test_function_is_not_auto_callable_but_runnable(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    _gw(client, h, "create_function", {
        "name": "draft_fn",
        "display_name": "草稿功能",
        "kind": "agent",
        "config": {"goal": "草稿", "instructions": "", "acceptance": "", "allowed_tools": []},
    })

    from app.db import SessionLocal
    from app.models import Role, User
    from app.security import Actor

    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.id == workspace["user_id"])).scalar_one()
        actor = Actor(user=user, workspace_id=ws, role=Role.OWNER)
        assert "draft_fn" not in chat._allowed_tools(db, actor)
    finally:
        db.close()

    run = _gw(client, h, "run_function", {"name": "draft_fn", "params": {}})
    assert run["decision"] == "ALLOWED"
    assert run["result"]["function_id"]


def test_develop_feature_chat_creates_executable_function(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.post(
        f"/workspaces/{ws}/chat",
        json={"message": "帮我开发一个自动回复客户询价的营销功能"},
        headers=h,
    )
    body = r.json()
    assert body["intent"] == "develop_feature"
    assert "测试功能区" in body["reply"]
    assert body["actions"][0]["tool"] == "create_function"

    fns = client.get(f"/workspaces/{ws}/functions", headers=h).json()["functions"]
    assert any(f["stage"] == "test" for f in fns)
    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert any(a["stage"] == "test" and a["kind"] == "function" for a in areas)


def test_run_agent_function_executes_allowed_tools(client, workspace, monkeypatch):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.example.com", "model": "test-model", "api_key": "test-key"}},
        headers=h,
    )
    r = _gw(client, h, "create_function", {
        "name": "inventory_qa",
        "display_name": "库存问答",
        "kind": "agent",
        "config": {
            "goal": "回答库存问题",
            "instructions": "只读库存",
            "acceptance": "返回库存数量",
            "allowed_tools": ["list_inventory"],
        },
    })
    fn_id = r["result"]["function_id"]

    plan = json.dumps({"steps": [{"tool": "list_inventory", "params": {}}], "reply": "库存已读取", "done": True})
    monkeypatch.setattr(llm, "generate", lambda prompt, context="", model_config=None: plan)

    run = _gw(client, h, "run_function", {"function_id": fn_id, "params": {"question": "还有多少货"}})
    assert run["decision"] == "ALLOWED"
    assert run["result"]["result"]["kind"] == "agent"
    assert run["result"]["result"]["reply"] == "库存已读取"
    steps = run["result"]["result"]["steps"]
    assert any(s["tool"] == "list_inventory" and s["decision"] == "ALLOWED" for s in steps)
