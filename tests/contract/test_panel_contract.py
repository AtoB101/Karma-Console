from app.panels import COMPONENT_REGISTRY, registry, validate_panel_config


def test_registry_lists_known_components():
    types = {c["type"] for c in registry()}
    assert {"data_list", "overview", "todos", "approvals", "chat"} <= types


def test_valid_data_list_passes():
    ok, errors, panel = validate_panel_config({"type": "data_list", "title": "客户", "data_source": "customers", "status": "live", "version": 1})
    assert ok, errors
    assert panel["data_source"] == "customers"
    assert panel["version"] == 1


def test_unknown_component_rejected():
    ok, errors, _ = validate_panel_config({"type": "eval('1')", "title": "x"})
    assert not ok
    assert any("未注册" in e for e in errors)


def test_missing_required_field_rejected():
    ok, errors, _ = validate_panel_config({"type": "data_list", "data_source": "customers"})
    assert not ok
    assert any("title" in e for e in errors)


def test_unauthorized_data_source_rejected():
    ok, errors, _ = validate_panel_config({"type": "data_list", "title": "x", "data_source": "admin/delete"})
    assert not ok
    assert any("未授权" in e for e in errors)


def test_invalid_status_and_version_rejected():
    ok, errors, _ = validate_panel_config({"type": "overview", "status": "running", "version": 0})
    assert not ok
    assert any("非法状态" in e for e in errors)
    assert any("version" in e for e in errors)


def test_malformed_config_rejected():
    ok, errors, _ = validate_panel_config("not-an-object")
    assert not ok


def test_registry_endpoint(client):
    r = client.get("/panels/registry")
    assert r.status_code == 200
    types = {c["type"] for c in r.json()["components"]}
    assert "data_list" in types


def test_validate_endpoint(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(f"/workspaces/{ws}/panels/validate", json={"type": "data_list", "title": "客户", "data_source": "customers"}, headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.post(f"/workspaces/{ws}/panels/validate", json={"type": "not_a_type"}, headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is False
def test_registry_lists_fourteen_base_components():
    types = {c["type"] for c in registry()}
    base = {
        "metric_card", "data_table", "trend_chart", "pie_chart", "filter",
        "search_box", "form", "task_list", "calendar", "report_reader",
        "manager_status", "approval_card", "log_viewer", "notification_list",
    }
    assert base <= types


def test_metric_card_requires_authorized_data_source():
    ok, errors, panel = validate_panel_config({"type": "metric_card", "title": "订单数", "data_source": "orders", "status": "live", "version": 1})
    assert ok, errors
    assert panel["data_source"] == "orders"

    ok, errors, _ = validate_panel_config({"type": "metric_card", "title": "x", "data_source": "admin/delete", "status": "live", "version": 1})
    assert not ok
    assert any("未授权" in e for e in errors)


def test_registry_declares_write_flag():
    by_type = {c["type"]: c for c in registry()}
    assert by_type["form"]["allows_write"] is True
    assert by_type["metric_card"]["allows_write"] is False
