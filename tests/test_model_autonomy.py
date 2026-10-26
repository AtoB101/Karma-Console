"""#176 模型配置走通 + 本地自主执行验证。

链路：保存模型配置（密钥加密落库）-> 运行时解出 -> 模型产出合法计划 ->
计划经统一网关执行 -> 本地工具真实落库并带回执；模型不可用时报错兜底不崩。
不伪造真实外部成交/扣款：资金与渠道工具在无凭证时返回 stub，这里只验证
本地自营闭环（待办/商品等）真实落库。
"""
import json

import app.chat as chat


def test_model_config_saved_encrypted_runtime_readable(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-real-secret-123456", "model": "deepseek-chat"}},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    # 回显必须掩码，绝不返回明文 key
    assert body["model"]["api_key"] != "sk-real-secret-123456"
    assert "3456" in body["model"]["api_key"]
    assert "api_key_enc" not in body["model"]
    assert body["model"]["model"] == "deepseek-chat"
    assert body["model"]["base_url"] == "https://api.deepseek.com/v1"

    # 落库的是密文，运行时按工作区解出明文（仅内存）
    from app.db import SessionLocal
    from app.llm import workspace_model_config

    db = SessionLocal()
    try:
        cfg = workspace_model_config(db, ws)
        assert cfg is not None
        assert cfg["api_key"] == "sk-real-secret-123456"
        assert cfg["model"] == "deepseek-chat"
        assert cfg["base_url"] == "https://api.deepseek.com/v1"
        # 未配置模型的工作区返回 None，启用确定性兜底
        assert workspace_model_config(db, "does-not-exist") is None
    finally:
        db.close()


def test_model_driven_local_todo_persists_with_receipt(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    # 先把模型配置走通（加密保存）
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-secret", "model": "deepseek-chat"}},
        headers=h,
    )

    plan = json.dumps({
        "intent": "todo",
        "steps": [
            {"tool": "create_todo", "params": {"kind": "info", "title": "核对今日在售库存", "detail": "开盘前盘点在售商品数量"}},
        ],
        "reply": "已记下开盘前核对库存的待办",
        "done": True,
    })
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: plan)

    r = client.post(
        f"/workspaces/{ws}/chat",
        json={"message": "帮我记一个待办：开盘前核对今日在售库存"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_source"] == "model"
    actions = body["actions"]
    assert [a["tool"] for a in actions] == ["create_todo"]
    assert actions[0]["decision"] == "ALLOWED"
    todo_id = actions[0]["result"]["todo_id"]
    assert todo_id

    # 真实落库，待办列表可查，回执闭环
    todos = client.get(f"/workspaces/{ws}/todos", headers=h).json()["todos"]
    saved = [t for t in todos if t["id"] == todo_id]
    assert len(saved) == 1
    assert saved[0]["title"] == "核对今日在售库存"
    assert saved[0]["status"] == "pending"


def test_model_unavailable_falls_back_without_500(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    # 配了模型但规划失败（无 key / 网络不可达 / 返回不可解析），必须吞掉并兜底
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-bad", "model": "deepseek-chat"}},
        headers=h,
    )
    monkeypatch.setattr(chat, "_call_model", lambda db, actor, message: None)

    r = client.post(
        f"/workspaces/{ws}/chat",
        json={"message": "你好，介绍一下这个工作台"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["reply"]


def test_gateway_local_tool_executes_with_receipt(client, workspace):
    """不经模型，统一网关直接执行本地 R1 工具，带回执与执行凭证。"""
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        "/gateway/execute",
        json={"tool": "create_todo", "params": {"kind": "info", "title": "网关直连待办", "detail": "验证网关回执"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["execution_id"]
    assert body["result"]["todo_id"]
    todos = client.get(f"/workspaces/{ws}/todos", headers=h).json()["todos"]
    assert any(t["id"] == body["result"]["todo_id"] for t in todos)
