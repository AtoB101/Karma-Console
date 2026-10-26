from sqlalchemy import select


def _gw(client, headers, tool, params):
    return client.post("/gateway/execute", json={"tool": tool, "params": params}, headers=headers).json()


def _add_area(client, workspace, name, stage="test", parent_id=None, target_type="function"):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/layout",
        json={"name": name, "kind": "module", "target_type": target_type, "parent_id": parent_id, "stage": stage},
        headers=workspace["headers"],
    ).json()


def test_area_test_to_live_promotion(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    area = _add_area(client, workspace, "自动回复客户", stage="test")

    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert next(a for a in areas if a["id"] == area["id"])["stage"] == "test"

    r = client.patch(f"/workspaces/{ws}/layout/{area['id']}", json={"stage": "live"}, headers=h)
    assert r.status_code == 200
    assert r.json()["stage"] == "live"

    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert next(a for a in areas if a["id"] == area["id"])["stage"] == "live"


def test_customer_pipeline_and_overview(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    for external_id, name in [("u1", "新增客"), ("u2", "意向客"), ("u3", "成交客")]:
        assert _gw(client, h, "upsert_customer", {"channel": "chat", "external_id": external_id, "name": name})["decision"] == "ALLOWED"
    customers = client.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]
    by_name = {c["name"]: c for c in customers}
    _gw(client, h, "update_customer", {"customer_id": by_name["意向客"]["id"], "stage": "lead"})
    _gw(client, h, "update_customer", {"customer_id": by_name["成交客"]["id"], "stage": "won"})

    overview = _gw(client, h, "business_overview", {})
    assert overview["decision"] == "ALLOWED"
    pipe = overview["result"]["pipeline"]
    assert pipe["new"] == 1
    assert pipe["lead"] == 1
    assert pipe["won"] == 1


def test_script_record_and_optimize(client, workspace, monkeypatch):
    h = workspace["headers"]
    r = _gw(client, h, "record_script", {"title": "报价话术A", "content": "您好，这款今天下单立减10元", "wins": 3, "sends": 5})
    assert r["decision"] == "ALLOWED"
    assert r["result"]["wins"] == 3
    _gw(client, h, "record_script", {"title": "报价话术B", "content": "库存不多了，先锁价", "wins": 1, "sends": 8})

    import app.tools as tools

    monkeypatch.setattr(tools, "_model_text", lambda ctx, prompt, fallback: "最优话术")
    opt = _gw(client, h, "optimize_scripts", {})
    assert opt["decision"] == "ALLOWED"
    assert opt["result"]["samples"] == 2
    assert opt["result"]["reply"] == "最优话术"
    assert opt["result"]["top"][0]["wins"] == 3


def test_business_context_includes_live_areas_and_scripts(client, workspace, monkeypatch):
    from app.db import SessionLocal
    from app.models import Role, User
    from app.security import Actor
    import app.chat as chat

    h = workspace["headers"]
    _add_area(client, workspace, "已上线功能X", stage="live")
    _add_area(client, workspace, "测试中功能Y", stage="test")
    _gw(client, h, "record_script", {"title": "成交话术", "content": "今天下单包邮", "wins": 5})

    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.id == workspace["user_id"])).scalar_one()
        actor = Actor(user=user, workspace_id=workspace["workspace_id"], role=Role.OWNER)
        text = chat._business_context(db, actor)
        assert "已上线功能X" in text
        assert "测试中功能Y" in text
        assert "已上线" in text
        assert "测试中" in text
        assert "成交话术" in text
    finally:
        db.close()

def test_function_full_lifecycle_run_and_promote(client, workspace):
    """T8: create -> catalog(acceptance) -> run -> promote -> live everywhere."""
    h = workspace["headers"]
    ws = workspace["workspace_id"]

    created = _gw(client, h, "create_function", {
        "name": "product_qa",
        "display_name": "产品问答",
        "description": "回答产品相关问题",
        "kind": "model",
        "category": "general",
        "config": {"goal": "回答产品相关问题", "instructions": "如实回答", "acceptance": "能返回产品问答结果"},
    })
    assert created["decision"] == "ALLOWED", created
    fn_id = created["result"]["function_id"]

    # catalog exposes acceptance + test stage
    catalog = client.get(f"/workspaces/{ws}/catalog", headers=h).json()["catalog"]
    entry = next(c for c in catalog if c.get("id") == fn_id)
    assert entry["acceptance"] == "能返回产品问答结果"
    assert entry["stage"] == "test"

    # layout area exists in test stage
    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    area = next(a for a in areas if a["target_id"] == fn_id)
    assert area["stage"] == "test"

    # run executes through gateway (model kind -> deterministic mock provider)
    run = _gw(client, h, "run_function", {"function_id": fn_id, "params": {"prompt": "介绍一下这款产品"}})
    assert run["decision"] == "ALLOWED", run
    inner = run["result"]["result"]
    assert inner["kind"] == "model"
    assert inner["text"]

    # promote flips function + area to live
    promote = _gw(client, h, "promote_function", {"function_id": fn_id})
    assert promote["decision"] == "ALLOWED", promote
    assert promote["result"]["stage"] == "live"

    catalog = client.get(f"/workspaces/{ws}/catalog", headers=h).json()["catalog"]
    assert next(c for c in catalog if c.get("id") == fn_id)["stage"] == "live"
    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert next(a for a in areas if a["target_id"] == fn_id)["stage"] == "live"

