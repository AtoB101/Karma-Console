def _post_product(client, workspace, payload):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/products",
        json=payload,
        headers=workspace["headers"],
    )


def _patch_product(client, workspace, product_id, payload):
    return client.patch(
        f"/workspaces/{workspace['workspace_id']}/products/{product_id}",
        json=payload,
        headers=workspace["headers"],
    )


def _chat(client, workspace, message):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": message},
        headers=workspace["headers"],
    )


def test_product_create_with_full_details(client, workspace):
    r = _post_product(client, workspace, {
        "name": "棉麻衬衫",
        "category": "服饰",
        "price": 99.0,
        "photo_url": "https://img.example.com/shirt.jpg",
        "specs": {"材质": "棉", "颜色": "白"},
        "detail": "100%棉，宽松版型，男女同款",
        "qualifications": ["GB18401", "SGS-2026"],
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    products = client.get(
        f"/workspaces/{workspace['workspace_id']}/products", headers=workspace["headers"]
    ).json()["products"]
    p = next(p for p in products if p["id"] == pid)
    assert p["photo_url"].endswith("shirt.jpg")
    assert p["specs"] == {"材质": "棉", "颜色": "白"}
    assert "100%棉" in p["detail"]
    assert p["qualifications"] == ["GB18401", "SGS-2026"]


def test_product_update_links_to_area(client, workspace):
    r = _post_product(client, workspace, {"name": "牛仔裤", "price": 159.0})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    area = client.post(
        f"/workspaces/{workspace['workspace_id']}/layout",
        json={"name": "产品管理", "kind": "module", "target_type": "product"},
        headers=workspace["headers"],
    ).json()

    r = _patch_product(client, workspace, pid, {
        "detail": "直筒版型",
        "specs": {"材质": "牛仔布"},
        "qualifications": ["GB/T 2662"],
        "layout_area_id": area["id"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "直筒版型"
    assert r.json()["layout_area_id"] == area["id"]


def test_chat_product_manage_categorizes_with_details(client, workspace):
    r = _chat(client, workspace, "在产品管理中加入 棉麻衬衫 99元 详情: 100%棉，材质: 棉，资质: GB18401")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"] == "product_display"
    assert len(body["created"]) == 1

    ws = workspace["workspace_id"]
    h = workspace["headers"]
    areas = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    area = next(a for a in areas if a["name"] == "产品管理")

    products = client.get(f"/workspaces/{ws}/products", headers=h).json()["products"]
    p = next(p for p in products if p["name"] == "棉麻衬衫")
    assert p["category"] == "产品管理"
    assert p["layout_area_id"] == area["id"]
    assert p["specs"].get("材质") == "棉"
    assert p["detail"]
    assert "GB18401" in p["qualifications"]
