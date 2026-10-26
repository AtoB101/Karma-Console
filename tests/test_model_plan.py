import json

import app.chat as chat


def test_parse_plan_handles_fences_and_invalid():
    raw = '```json\n{"intent":"copy","steps":[{"tool":"generate_marketing_draft","params":{"prompt":"x"}}],"reply":"ok"}\n```'
    plan = chat._parse_plan(raw)
    assert plan["intent"] == "copy"
    assert plan["steps"][0]["tool"] == "generate_marketing_draft"
    assert chat._parse_plan("not json at all") is None
    assert chat._parse_plan('{"steps":[]}') is None


def test_model_plan_executes_through_gateway(client, workspace, monkeypatch):
    plan_json = json.dumps({
        "intent": "product_display",
        "steps": [
            {"tool": "add_layout_area", "params": {"name": "产品展示区", "kind": "module", "target_type": "product_display"}},
            {"tool": "create_product_draft", "params": {"name": "棉麻衬衫", "price": 99}},
            {"tool": "create_product_draft", "params": {"name": "牛仔裤", "price": 159}},
        ],
        "reply": "已把2件商品归类进产品展示区",
    })
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: plan_json)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "在产品展示区加入 棉麻衬衫 99元、牛仔裤 159元"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert body["reply"] == "已把2件商品归类进产品展示区"
    assert [a["tool"] for a in body["actions"]] == ["add_layout_area", "create_product_draft", "create_product_draft"]

    h = workspace["headers"]
    ws = workspace["workspace_id"]
    products = client.get(f"/workspaces/{ws}/products", headers=h).json()["products"]
    assert {p["name"] for p in products} == {"棉麻衬衫", "牛仔裤"}
    assert all(p["layout_area_id"] for p in products)


def test_model_plan_denies_unknown_tool_then_recovers(client, workspace, monkeypatch):
    bad_plan = json.dumps({
        "intent": "help",
        "steps": [
            {"tool": "steal_money", "params": {}},
            {"tool": "workspace_summary", "params": {}},
        ],
        "reply": "first",
    })
    good_plan = json.dumps({
        "intent": "help",
        "steps": [
            {"tool": "workspace_summary", "params": {}},
        ],
        "reply": "ok",
        "done": True,
    })
    queue = [bad_plan, good_plan]
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: queue.pop(0))
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "随便"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert body["reply"] == "ok"
    executed = [a["tool"] for a in body["actions"]]
    # P15: unknown tools are rejected at plan-lint layer and never enter execution
    assert "steal_money" not in executed
    allowed = [a for a in body["actions"] if a["tool"] == "workspace_summary" and a["decision"] == "ALLOWED"]
    assert allowed


def test_model_plain_prose_returned_when_unparseable(client, workspace, monkeypatch):
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: "garbage-not-json")
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "写一段棉麻衬衫的营销文案"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["intent"] == "model"
    assert body["reply"] == "garbage-not-json"


def test_param_hints_cover_goal_and_memory():
    for name in ("list_goals", "create_goal", "update_goal", "get_memory", "set_memory"):
        assert name in chat._PARAM_HINTS


def test_planner_prompt_includes_contract_rules_and_tools(client, workspace, monkeypatch):
    import app.llm as llm

    seen = {}

    def fake_ws_config(db, workspace_id):
        return {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "api_key": "sk-test", "model": "deepseek-chat"}

    def fake_chat(messages, model_config=None, temperature=0.2, max_tokens=None, json_mode=False):
        seen["messages"] = messages
        return json.dumps({"intent": "chat", "steps": [], "reply": "ok", "done": True})

    monkeypatch.setattr(llm, "workspace_model_config", fake_ws_config)
    monkeypatch.setattr(llm, "chat", fake_chat)

    client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我看看库存"},
        headers=workspace["headers"],
    )
    user_prompt = seen["messages"][1]["content"]
    assert "可用工具及参数" in user_prompt
    assert "执行规则" in user_prompt
    assert "示例（用户：在产品展示区加入" in user_prompt
    assert "list_inventory" in user_prompt
    assert "create_goal" in user_prompt


def test_model_plan_retries_invalid_json_then_recovers(client, workspace, monkeypatch):
    queue = [
        '{"steps": [broken',
        '{"intent": "inventory", "steps": [{"tool": "list_inventory", "params": {}}], "reply": "库存如下", "done": true}',
    ]
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: queue.pop(0))
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "看看库存"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert body["reply"] == "库存如下"
    assert [a["tool"] for a in body["actions"]] == ["list_inventory"]


def test_llm_chat_mock_json_mode():
    from app.llm import chat

    raw = chat([{"role": "user", "content": "x"}], json_mode=True)
    data = json.loads(raw)
    assert data["steps"] == []


def test_planner_uses_chat_with_system_prompt(client, workspace, monkeypatch):
    import app.llm as llm

    calls = []

    def fake_ws_config(db, workspace_id):
        return {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "api_key": "sk-test", "model": "deepseek-chat"}

    plan = json.dumps({"intent": "inventory", "steps": [{"tool": "list_inventory", "params": {}}], "reply": "已查看库存", "done": True})

    def fake_chat(messages, model_config=None, temperature=0.2, max_tokens=None, json_mode=False):
        calls.append({"messages": messages, "temperature": temperature, "json_mode": json_mode})
        return plan

    monkeypatch.setattr(llm, "workspace_model_config", fake_ws_config)
    monkeypatch.setattr(llm, "chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "看看库存"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert body["reply"] == "已查看库存"
    assert calls
    assert calls[0]["messages"][0]["role"] == "system"
    assert calls[0]["messages"][1]["role"] == "user"
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["json_mode"] is True
