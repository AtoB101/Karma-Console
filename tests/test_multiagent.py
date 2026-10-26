import json

import app.multiagent as ma


def test_is_compound_heuristic():
    assert ma.is_compound("帮我上架棉麻衬衫并给客户报价")
    assert ma.is_compound("上架产品，同时生成营销文案")
    assert not ma.is_compound("看看库存")
    assert not ma.is_compound("你好")


def _fake_chat_factory():
    calls = []

    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        calls.append({"system": system, "user": user})
        if "编排器" in system:
            return json.dumps({
                "subtasks": [
                    {"role": "listing", "instruction": "上架棉麻衬衫99元"},
                    {"role": "fulfillment", "instruction": "给棉麻衬衫加库存10件"},
                ],
                "summary": "已上架并加库存",
            }, ensure_ascii=False)
        if "加库存" in user:
            return json.dumps({"steps": [{"tool": "add_stock", "params": {"quantity": 10}}], "reply": "已加库存", "done": True}, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "棉麻衬衫", "price": 99}}], "reply": "已上架", "done": True}, ensure_ascii=False)

    return calls, fake_chat


def test_multiagent_orchestrates_and_executes(client, workspace, monkeypatch):
    calls, fake_chat = _fake_chat_factory()
    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我上架棉麻衬衫99元，并给它加库存10件"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    assert body["intent"] == "multiagent"
    assert len(body["subtasks"]) == 2
    assert [a["tool"] for a in body["actions"]] == ["create_product_draft", "add_stock"]

    products = client.get(f"/workspaces/{workspace['workspace_id']}/products", headers=workspace["headers"]).json()["products"]
    shirt = next(p for p in products if p["name"] == "棉麻衬衫")
    assert shirt["stock"] == 10
    assert len(calls) == 3  # orchestrator + 2 workers


def test_multiagent_denies_unknown_tool(client, workspace, monkeypatch):
    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        if "编排器" in system:
            return json.dumps({"subtasks": [{"role": "store_manager", "instruction": "执行动作"}], "summary": "汇总"}, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "steal_money", "params": {}}], "reply": "执行", "done": True}, ensure_ascii=False)

    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我执行一个动作并顺便做个总结"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    denied = [a for a in body["actions"] if a["tool"] == "steal_money"]
    assert denied and denied[0]["decision"] == "DENIED"
    assert "已拒绝" in body["reply"]


def test_multiagent_resolves_duplicate_mutation_conflict(client, workspace, monkeypatch):
    """Two workers proposing the same create_product target: second is CONFLICT."""
    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        if "编排器" in system:
            return json.dumps({
                "subtasks": [
                    {"role": "listing", "instruction": "上架棉麻衬衫99元"},
                    {"role": "store_manager", "instruction": "再次上架棉麻衬衫99元"},
                ],
                "summary": "已上架",
            }, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "棉麻衬衫", "price": 99}}], "reply": "已上架", "done": True}, ensure_ascii=False)

    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我上架棉麻衬衫99元，并再上架一次棉麻衬衫99元"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    decisions = [a["decision"] for a in body["actions"]]
    assert decisions.count("ALLOWED") == 1
    assert decisions.count("CONFLICT") == 1
    assert body.get("conflicts_resolved") == 1
    products = client.get(f"/workspaces/{workspace['workspace_id']}/products", headers=workspace["headers"]).json()["products"]
    assert len([p for p in products if p["name"] == "棉麻衬衫"]) == 1


def test_multiagent_executes_distinct_workers_in_order(client, workspace, monkeypatch):
    """Multiple distinct worker plans are applied in dependency order."""
    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        if "编排器" in system:
            return json.dumps({
                "subtasks": [
                    {"role": "listing", "instruction": "上架牛仔裤159元"},
                    {"role": "listing", "instruction": "上架卫衣129元"},
                ],
                "summary": "已上架两件",
            }, ensure_ascii=False)
        if "牛仔裤" in user:
            return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "牛仔裤", "price": 159}}], "reply": "ok", "done": True}, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "卫衣", "price": 129}}], "reply": "ok", "done": True}, ensure_ascii=False)

    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我上架牛仔裤159元，并上架卫衣129元"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    assert [a["tool"] for a in body["actions"]] == ["create_product_draft", "create_product_draft"]
    names = [p["name"] for p in client.get(f"/workspaces/{workspace['workspace_id']}/products", headers=workspace["headers"]).json()["products"]]
    assert "牛仔裤" in names and "卫衣" in names


def test_multiagent_depends_on_runs_prerequisite_first(client, workspace, monkeypatch):
    """A declared dependency makes the prerequisite run before the dependent,
    even when the dependent is listed first in the orchestrator array."""
    def fake_chat(db, actor, system, user, *, temperature=0.2, max_tokens=1600):
        if "编排器" in system:
            # dependent listed BEFORE its prerequisite on purpose
            return json.dumps({
                "subtasks": [
                    {"id": "second", "role": "listing", "instruction": "上架依赖商品B", "depends_on": ["first"]},
                    {"id": "first", "role": "listing", "instruction": "上架前置商品A", "depends_on": []},
                ],
                "summary": "先A后B",
            }, ensure_ascii=False)
        if "商品B" in user:
            return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "商品B", "price": 2}}], "reply": "B", "done": True}, ensure_ascii=False)
        return json.dumps({"steps": [{"tool": "create_product_draft", "params": {"name": "商品A", "price": 1}}], "reply": "A", "done": True}, ensure_ascii=False)

    monkeypatch.setattr(ma, "_model_config", lambda db, actor: {"provider": "openai_compat", "model": "deepseek-chat", "api_key": "sk-test"})
    monkeypatch.setattr(ma, "_chat", fake_chat)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "先建前置商品A，再建依赖商品B"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "multiagent"
    names = [a["params"]["name"] for a in body["actions"]]
    assert names == ["商品A", "商品B"]
