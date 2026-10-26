from app.chat import detect_intent, extract_area_name, extract_parent_area, parse_products


def _chat(client, workspace, message):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": message},
        headers=workspace["headers"],
    )


def test_parse_products():
    products = parse_products("棉麻衬衫 99元、牛仔裤 ¥159.5、奶茶 12.5元")
    assert [p["name"] for p in products] == ["棉麻衬衫", "牛仔裤", "奶茶"]
    assert products[1]["price"] == 159.5


def test_intent_detection():
    assert detect_intent("支付 10 元") == "payment"
    assert detect_intent("写一段棉麻衬衫的营销文案") == "copy"
    assert detect_intent("把 棉麻衬衫 99元 归类到产品展示区") == "product_display"
    assert extract_area_name("把商品加到产品展示区") == "产品展示区"
    assert extract_parent_area("在测试功能区给我加上一个产品展示区") == "测试功能区"


def test_chat_product_display_flow(client, workspace):
    r = _chat(client, workspace, "在产品展示区加入 棉麻衬衫 99元、牛仔裤 159元")
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "product_display"
    assert "产品展示区" in body["reply"]
    assert len(body["created"]) == 2

    ws = workspace["workspace_id"]
    h = workspace["headers"]
    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert any(a["name"] == "产品展示区" for a in areas)

    products = client.get(f"/workspaces/{ws}/products", headers=h).json()["products"]
    names = {p["name"] for p in products}
    assert {"棉麻衬衫", "牛仔裤"} <= names
    for p in products:
        assert p["category"] == "产品展示区"
        assert p["layout_area_id"]


def test_chat_subfunction_under_parent(client, workspace):
    r = _chat(client, workspace, "在测试功能区给我加上一个 产品展示区 棉麻衬衫 99元")
    assert r.status_code == 200
    body = r.json()
    assert body["parent_id"]

    areas = client.get(f"/workspaces/{workspace['workspace_id']}/layout", headers=workspace["headers"]).json()["areas"]
    parent = next(a for a in areas if a["name"] == "测试功能区")
    child = next(a for a in areas if a["name"] == "产品展示区")
    assert child["parent_id"] == parent["id"]


def test_chat_copy(client, workspace):
    r = _chat(client, workspace, "写一段棉麻衬衫的营销文案")
    body = r.json()
    assert body["intent"] == "copy"
    assert "mock" in body["draft"]


def test_chat_payment_requires_approval(client, workspace):
    r = _chat(client, workspace, "支付 10 元")
    body = r.json()
    assert body["intent"] == "payment"
    assert body["decision"] == "PENDING"
    assert body["approval_id"]


def test_intent_detection_for_orders_and_inventory():
    assert detect_intent("看看库存") == "inventory"
    assert detect_intent("订单列表") == "orders"
    assert detect_intent("查一下发货单") == "shipments"


def test_intent_detection_for_capabilities():
    assert detect_intent("你能调用你安装的所有技能吗") == "capabilities"


def test_intent_detection_for_business_onboarding():
    assert detect_intent("我是一家川菜馆的老板，你能帮我做什么") == "business_onboarding"


def test_chat_business_onboarding_food(client, workspace):
    r = _chat(client, workspace, "我是一家川菜馆的老板，你能帮我做什么")
    body = r.json()
    assert body["intent"] == "business_onboarding"
    assert body["category"] == "food"
    assert "接单" in body["reply"]
    assert "骑手" in body["reply"]
    assert "送达" in body["reply"]


def test_chat_capabilities_lists_skills_and_tools(client, workspace):
    r = _chat(client, workspace, "你能调用你安装的所有技能吗")
    body = r.json()
    assert body["intent"] == "capabilities"
    assert "已安装技能" in body["reply"]
    assert "内置工具" in body["reply"]
    assert "list_products" in body["reply"]
    assert "尚未配置模型" in body["reply"]


def test_chat_lists_inventory_and_orders(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.post(f"/workspaces/{ws}/products", json={"name": "衬衫", "price": 99.0}, headers=h)

    inv = _chat(client, workspace, "看看库存")
    assert inv.json()["intent"] == "inventory"
    assert "库存" in inv.json()["reply"]

    orders = _chat(client, workspace, "订单列表")
    assert orders.json()["intent"] == "orders"
    assert "订单" in orders.json()["reply"]
