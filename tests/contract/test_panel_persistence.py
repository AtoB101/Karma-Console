"""T5: server-side persistence for neural-console panels.

Contract (docs/INTERFACE_CONTRACTS.md section 6):
  - panel_config = {panel_id, type, title, data_source, layout, permissions, version, status}
  - unknown type is rejected; invalid config never enabled
  - a failed panel update leaves the previous version usable (version rollback)
"""
from __future__ import annotations


def _create_panel(client, headers, ws, **overrides):
    payload = {"type": "data_list", "title": "客户列表", "data_source": "customers", "status": "live", "version": 1}
    payload.update(overrides)
    return client.post(f"/workspaces/{ws}/panels", json=payload, headers=headers)


def test_panel_create_and_list_persists(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]

    r = _create_panel(client, h, ws)
    assert r.status_code == 201, r.text
    panel = r.json()["panel"]
    assert panel["panel_id"]
    assert panel["type"] == "data_list"
    assert panel["title"] == "客户列表"
    assert panel["data_source"] == "customers"
    assert panel["status"] == "live"
    assert panel["version"] == 1

    r = client.get(f"/workspaces/{ws}/panels", headers=h)
    assert r.status_code == 200
    panels = r.json()["panels"]
    assert len(panels) == 1
    assert panels[0]["panel_id"] == panel["panel_id"]


def test_panel_unknown_type_rejected(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _create_panel(client, h, ws, type="not_a_component")
    assert r.status_code == 400
    assert "未注册" in r.json()["detail"]


def test_panel_invalid_data_source_rejected(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    r = _create_panel(client, h, ws, data_source="admin/delete")
    assert r.status_code == 400
    assert "未授权" in r.json()["detail"]


def test_panel_update_failure_keeps_previous_version(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]
    pid = created["panel_id"]

    bad = client.patch(f"/workspaces/{ws}/panels/{pid}", json={"data_source": "forbidden"}, headers=h)
    assert bad.status_code == 400

    ok = client.get(f"/workspaces/{ws}/panels", headers=h)
    panels = ok.json()["panels"]
    assert len(panels) == 1
    assert panels[0]["data_source"] == "customers"
    assert panels[0]["version"] == 1
    assert panels[0]["title"] == "客户列表"


def test_panel_update_valid_bumps_version(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]
    pid = created["panel_id"]

    r = client.patch(f"/workspaces/{ws}/panels/{pid}", json={"title": "客户总览", "status": "draft"}, headers=h)
    assert r.status_code == 200
    assert r.json()["panel"]["title"] == "客户总览"
    assert r.json()["panel"]["status"] == "draft"
    assert r.json()["panel"]["version"] == 2
    assert r.json()["panel"]["data_source"] == "customers"


def test_panel_delete(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]

    r = client.delete(f"/workspaces/{ws}/panels/{created['panel_id']}", headers=h)
    assert r.status_code == 204
    r = client.get(f"/workspaces/{ws}/panels", headers=h)
    assert r.json()["panels"] == []


def test_panel_workspace_isolation(client, workspace):
    h = workspace["headers"]
    user_key = workspace["headers"]["Authorization"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]

    r = client.post("/workspaces", json={"name": "第二家店", "region": "CN", "business_category": "food"}, headers={"Authorization": user_key})
    assert r.status_code == 201, r.text
    other_ws = r.json()["id"]
    other_headers = {"Authorization": user_key, "X-Workspace-Id": other_ws}

    r = client.get(f"/workspaces/{other_ws}/panels", headers=other_headers)
    assert r.status_code == 200
    assert r.json()["panels"] == []

    r = client.patch(f"/workspaces/{other_ws}/panels/{created['panel_id']}", json={"title": "越权"}, headers=other_headers)
    assert r.status_code == 404


def test_neural_layout_roundtrip(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    positions = {"customers": {"x": 10, "y": 20}, "products": {"x": 30, "y": 40}}

    r = client.put(f"/workspaces/{ws}/neural/layout", json={"positions": positions}, headers=h)
    assert r.status_code == 200
    assert r.json()["positions"] == positions

    r = client.get(f"/workspaces/{ws}/neural/layout", headers=h)
    assert r.status_code == 200
    assert r.json()["positions"] == positions


def test_panel_layout_enriched_from_settings(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]

    r = client.put(f"/workspaces/{ws}/neural/layout", json={"positions": {created["panel_id"]: {"x": 5, "y": 6}}}, headers=h)
    assert r.status_code == 200

    r = client.get(f"/workspaces/{ws}/panels", headers=h)
    panel = r.json()["panels"][0]
    assert panel["layout"] == {"x": 5, "y": 6}


def test_panel_endpoints_require_actor(client, workspace):
    ws = workspace["workspace_id"]
    r = client.get(f"/workspaces/{ws}/panels")
    assert r.status_code == 401
    r = client.post(f"/workspaces/{ws}/panels", json={"type": "overview", "title": "x"})
    assert r.status_code == 401

def test_panel_version_history_and_rollback(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    created = _create_panel(client, h, ws).json()["panel"]
    pid = created["panel_id"]

    # first update creates history for v1
    r = client.patch(f"/workspaces/{ws}/panels/{pid}", json={"title": "客户表V2"}, headers=h)
    assert r.status_code == 200
    assert r.json()["panel"]["version"] == 2

    hist = client.get(f"/workspaces/{ws}/panels/{pid}/history", headers=h).json()
    versions = [v["version"] for v in hist["versions"]]
    assert versions[0] == 1  # newest snapshot is the pre-update (v1) state

    roll = client.post(f"/workspaces/{ws}/panels/{pid}/rollback/{1}", headers=h)
    assert roll.status_code == 200
    assert roll.json()["panel"]["title"] == "客户列表"
    assert roll.json()["panel"]["version"] == 3
