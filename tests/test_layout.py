def test_layout_place_connector_widget(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = client.post(f"/workspaces/{ws}/connectors", json={"connector_type": "ecommerce_social", "name": "社交网店"}, headers=h)
    conn_id = r.json()["id"]

    r = client.post(
        f"/workspaces/{ws}/layout",
        json={"name": "社交网店下单", "kind": "connector", "target_type": "ecommerce_social", "target_id": conn_id, "sort_order": 1},
        headers=h,
    )
    assert r.status_code == 201

    r = client.get(f"/workspaces/{ws}/layout", headers=h)
    areas = r.json()["areas"]
    assert len(areas) == 1
    assert areas[0]["target_id"] == conn_id
    assert areas[0]["enabled"] is True
