"""Phase-1 end-to-end: 需求 → 主脑规划 → 面板创建 → 校验 → 经理人执行 → 验证 → 记录 → 查看."""
import json


def test_phase1_full_chain(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    # 1) 面板创建（草稿功能区）
    r = client.post(
        f"/workspaces/{ws}/layout",
        json={"name": "库存面板", "kind": "module", "target_type": "inventory", "stage": "test"},
        headers=h,
    )
    assert r.status_code == 201, r.text

    # 2) 配置校验：合法通过，非法拒绝
    r = client.post(f"/workspaces/{ws}/panels/validate", json={"type": "data_list", "title": "库存", "data_source": "inventory"}, headers=h)
    assert r.json()["ok"] is True
    r = client.post(f"/workspaces/{ws}/panels/validate", json={"type": "data_list", "title": "库存", "data_source": "root_shell"}, headers=h)
    assert r.json()["ok"] is False

    # 3) 需求 → 主脑规划 → 经理人执行（确定性解析走网关）→ 返回
    r = client.post(f"/workspaces/{ws}/chat", json={"message": "看看库存"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "库存" in body.get("reply", "")

    # 4) 记录保存：主脑事件流有该动作且已允许
    r = client.get(f"/workspaces/{ws}/brain/events?limit=50", headers=h)
    assert r.status_code == 200
    actions = {e["action"]: e["decision"] for e in r.json()["events"]}
    assert actions.get("list_inventory") == "ALLOWED"