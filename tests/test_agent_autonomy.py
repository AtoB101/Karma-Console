"""Tests for the autonomous business loop, customer memory and outbound reply."""
import json
import uuid

import app.chat as chat


def _enable_chat(client, workspace, token=None):
    token = token or f"bot-{uuid.uuid4().hex}"
    return token, client.put(
        f"/workspaces/{workspace['workspace_id']}/settings",
        json={"chat": {"provider": "webhook", "token": token, "enabled": True}},
        headers=workspace["headers"],
    )


def test_inbound_records_customer_conversation_and_outbound(client, workspace):
    token, _ = _enable_chat(client, workspace)
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    r = client.post(
        "/chat/inbound",
        json={"token": token, "message": "你好 想看看棉麻衬衫", "channel": "telegram", "external_id": "u123", "customer_name": "李小姐"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["customer_id"]
    assert body["conversation_id"]
    assert body["outbound"]["sent"] is True

    customers = client.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]
    assert len(customers) == 1
    assert customers[0]["name"] == "李小姐"
    assert customers[0]["external_id"] == "u123"

    detail = client.get(f"/workspaces/{ws}/customers/{body['customer_id']}", headers=h).json()
    messages = detail["conversations"][0]["messages"]
    assert [m["direction"] for m in messages] == ["in", "out"]
    assert "棉麻衬衫" in messages[0]["content"]

    # Same external id reuses the customer and accumulates memory.
    client.post(
        "/chat/inbound",
        json={"token": token, "message": "多少钱", "channel": "telegram", "external_id": "u123"},
    )
    customers = client.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]
    assert len(customers) == 1
    detail = client.get(f"/workspaces/{ws}/customers/{body['customer_id']}", headers=h).json()
    assert len(detail["conversations"][0]["messages"]) == 4


def test_business_tools_quote_introduce_and_reply(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    product = client.post(f"/workspaces/{ws}/products", json={"name": "棉麻衬衫", "price": 99.0}, headers=h).json()
    product_id = product["id"]

    def run(tool, params):
        return client.post("/gateway/execute", json={"tool": tool, "params": params}, headers=h).json()

    quote = run("quote_product", {"product_id": product_id, "quantity": 2})
    assert quote["decision"] == "ALLOWED"
    assert quote["result"]["quote"]["total"] == 198.0
    assert quote["result"]["reply"]

    intro = run("introduce_product", {"product_id": product_id})
    assert intro["decision"] == "ALLOWED"
    assert intro["result"]["reply"]

    customer = run("upsert_customer", {"channel": "telegram", "external_id": "u9", "name": "王先生"})
    assert customer["decision"] == "ALLOWED"
    customer_id = customer["result"]["customer_id"]

    reply = run("reply_customer", {"customer_id": customer_id, "text": "您好，已为您报价"})
    assert reply["decision"] == "ALLOWED"
    assert reply["result"]["sent"] is True
    assert reply["result"]["message_id"]

    customers = run("list_customers", {})
    assert any(c["external_id"] == "u9" for c in customers["result"]["customers"])


def test_agent_loop_continues_across_steps(client, workspace, monkeypatch):
    plans = iter([
        json.dumps({"intent": "product_display", "steps": [{"tool": "add_layout_area", "params": {"name": "产品展示区", "kind": "module", "target_type": "product_display"}}], "done": False}),
        json.dumps({"intent": "product_display", "steps": [{"tool": "create_product_draft", "params": {"name": "棉麻衬衫", "price": 99}}], "done": False}),
        json.dumps({"intent": "product_display", "steps": [], "reply": "已建区并上架商品", "done": True}),
    ])
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: next(plans))

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "建一个产品展示区并上架棉麻衬衫"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert body["iterations"] == 3
    assert [a["tool"] for a in body["actions"]] == ["add_layout_area", "create_product_draft"]
    assert body["reply"] == "已建区并上架商品"


def test_agent_loop_clarify(client, workspace, monkeypatch):
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: '{"clarify": "请补充商品名称和单价"}')
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "帮我加个商品"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["intent"] == "clarify"
    assert "单价" in body["reply"]


def test_single_instruction_chains_product_stock_quote(client, workspace, monkeypatch):
    plan_json = json.dumps({
        "intent": "product_display",
        "steps": [
            {"tool": "create_product_draft", "params": {"name": "棉麻衬衫", "price": 99}},
            {"tool": "add_stock", "params": {"quantity": 10}},
            {"tool": "upsert_customer", "params": {"channel": "chat", "external_id": "u-li", "name": "李小姐"}},
            {"tool": "quote_product", "params": {"quantity": 2}},
        ],
        "reply": "已上架棉麻衬衫并给李小姐报价",
        "done": True,
    })
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: plan_json)

    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "上架棉麻衬衫 99元，库存 10，并给李小姐报价"},
        headers=workspace["headers"],
    )
    body = r.json()
    assert body["plan_source"] == "model"
    assert [a["tool"] for a in body["actions"]] == ["create_product_draft", "add_stock", "upsert_customer", "quote_product"]
    results = {a["tool"]: a["result"] for a in body["actions"]}
    assert results["add_stock"]["stock"] == 10
    assert results["quote_product"]["quote"]["total"] == 198.0
    assert results["quote_product"]["reply"]
