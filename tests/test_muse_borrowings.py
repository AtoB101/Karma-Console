"""Muse-inspired borrowings: durable goals, agent memory and protection summary."""
from app.chat import detect_intent


def test_goal_intent():
    assert detect_intent("每天帮我盯着库存预警和待发货订单") == "goal"
    assert detect_intent("持续关注意向客户有没有回复") == "goal"


def test_goal_lifecycle(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.post(f"/workspaces/{ws}/goals", json={"title": "盯库存", "detail": "每天"}, headers=h)
    assert r.status_code == 201, r.text
    goal = r.json()
    assert goal["status"] == "active"
    r = client.get(f"/workspaces/{ws}/goals", headers=h)
    assert r.status_code == 200
    assert any(g["id"] == goal["id"] for g in r.json()["goals"])
    r = client.patch(f"/workspaces/{ws}/goals/{goal['id']}", json={"status": "done", "progress_note": "完成"}, headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_agent_memory(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.put(f"/workspaces/{ws}/agent/memory", json={"key": "spicy", "value": "默认不接特辣", "kind": "preference"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "默认不接特辣"
    r = client.get(f"/workspaces/{ws}/agent/memory", headers=h)
    assert r.status_code == 200
    assert r.json()["memories"][0]["key"] == "spicy"
    r = client.delete(f"/workspaces/{ws}/agent/memory/spicy", headers=h)
    assert r.status_code == 204, r.text
    r = client.get(f"/workspaces/{ws}/agent/memory", headers=h)
    assert r.json()["memories"] == []


def test_dashboard_protection_summary(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.get(f"/workspaces/{ws}/dashboard", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "protection" in body
    assert body["protection"]["approval_required_for_payments"] is True
    assert "goals" in body and "agent_memory" in body