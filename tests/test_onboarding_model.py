"""Onboarding is model-driven once a workspace model is configured.

Before this change, "你是xx老板，你能帮我做什么" always short-circuited to a
static playbook. Now the deterministic handler still registers the shop and
installs skills, then the real agent loop takes over when a model key exists.
"""
from __future__ import annotations


def _configure_model(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-test-onboarding"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return h, ws


def _chat(client, workspace, message):
    return client.post(f"/workspaces/{workspace['workspace_id']}/chat", json={"message": message}, headers=workspace["headers"])


def test_onboarding_stays_deterministic_without_model(client, workspace):
    r = _chat(client, workspace, "我是一家川菜馆的老板，你能帮我做什么")
    body = r.json()
    assert body["intent"] == "business_onboarding"
    assert body["category"] == "food"
    assert "骑手" in body["reply"]


def test_onboarding_uses_model_loop_when_configured(client, workspace, monkeypatch):
    from app import chat

    _configure_model(client, workspace)

    plan = '{"intent":"business_onboarding","steps":[{"tool":"add_layout_area","params":{"name":"菜单管理","kind":"tool","target_type":"menu","stage":"live"}}],"done":true,"reply":"我是川菜馆老板，我已经把菜单管理区建好，你可以把菜品发我。"}'
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: plan)

    r = _chat(client, workspace, "我是一家川菜馆的老板，你能帮我做什么")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"] == "business_onboarding"
    assert body["category"] == "food"
    assert body["plan_source"] == "model"
    tools = [a["tool"] for a in body["actions"]]
    assert "add_layout_area" in tools
    allowed = next(a for a in body["actions"] if a["tool"] == "add_layout_area")
    assert allowed["decision"] == "ALLOWED"
    assert allowed["result"]["area_id"]


def test_onboarding_model_prose_returned(client, workspace, monkeypatch):
    from app import chat

    _configure_model(client, workspace)
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: "请你先发我菜单和单价")

    r = _chat(client, workspace, "我是一家川菜馆的老板，你能帮我做什么")
    body = r.json()
    assert body["intent"] == "business_onboarding"
    assert body["category"] == "food"
    assert body["reply"] == "请你先发我菜单和单价"
    assert body["plan_source"] == "model"
