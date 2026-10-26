def _add_area(client, workspace, name="回执", kind="function", target_type="function", target_id="x", sort_order=0):
    return client.post(
        f"/workspaces/{workspace['workspace_id']}/layout",
        json={"name": name, "kind": kind, "target_type": target_type, "target_id": target_id, "sort_order": sort_order},
        headers=workspace["headers"],
    )


def test_console_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    r = client.get("/console", follow_redirects=False)
    assert r.status_code in (301, 302, 307)
    assert r.headers["location"] == "/"


def test_tools_include_description(client, workspace):
    r = client.get(f"/workspaces/{workspace['workspace_id']}/tools", headers=workspace["headers"])
    assert r.status_code == 200
    assert all("description" in t for t in r.json()["tools"])


def test_layout_update_delete_reorder(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    a1 = _add_area(client, workspace, "第一", sort_order=0).json()
    a2 = _add_area(client, workspace, "第二", sort_order=1).json()

    r = client.patch(f"/workspaces/{ws}/layout/{a1['id']}", json={"enabled": False, "name": "第一改"}, headers=h)
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["name"] == "第一改"

    r = client.post(f"/workspaces/{ws}/layout/reorder", json={"area_ids": [a2["id"], a1["id"]]}, headers=h)
    assert r.status_code == 200
    assert r.json()["areas"][0]["id"] == a2["id"]
    assert r.json()["areas"][1]["id"] == a1["id"]

    r = client.delete(f"/workspaces/{ws}/layout/{a1['id']}", headers=h)
    assert r.status_code == 204
    r = client.get(f"/workspaces/{ws}/layout", headers=h)
    assert [a["id"] for a in r.json()["areas"]] == [a2["id"]]


def test_approvals_list_and_catalog(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.get(f"/workspaces/{ws}/approvals", headers=h)
    assert r.status_code == 200
    assert "approvals" in r.json()

    r = client.get(f"/workspaces/{ws}/catalog", headers=h)
    assert r.status_code == 200
    types = {c["type"] for c in r.json()["catalog"]}
    assert "tool" in types


# 首屏各面板只读端点连通性：任何 404/500 都会让面板点开报错，逐个断言 200。
_HOME_READONLY = [
    "/dashboard", "/products", "/orders", "/customers", "/agents",
    "/commerce", "/readiness", "/audit/integrity", "/devices", "/data-map",
    "/settings", "/metrics", "/connectors", "/functions", "/tools",
    "/brain/events?limit=5", "/approvals", "/todos?status=pending",
    "/shipments", "/inventory", "/alerts", "/jobs", "/retention/policy", "/goals", "/agent/memory",
    "/settings/history", "/virtual-numbers",
    "/nervous/tentacles", "/nervous/assignment?tool=list_products",
    "/nervous/panel/order", "/nervous/overview", "/ledger/board",
    "/integrations/status", "/ops/selfcheck", "/system/logs",
    "/briefing?period=daily",
]


def test_home_panel_readonly_endpoints_respond(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    failures = []
    for ep in _HOME_READONLY:
        r = client.get(f"/workspaces/{ws}{ep}", headers=h)
        if r.status_code != 200:
            failures.append(f"{ep} -> {r.status_code}")
    for ep in ["/skills", "/marketplace", "/backups", "/health"]:
        r = client.get(ep, headers=h)
        if r.status_code != 200:
            failures.append(f"{ep} -> {r.status_code}")
    assert not failures, "首屏面板端点异常: " + "; ".join(failures)


def test_nervous_endpoints_return_dispatch_cards(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    idx = client.get(f"/workspaces/{ws}/nervous/tentacles", headers=h).json()
    assert idx["tentacles"] and len(idx["tentacles"]) >= 6

    money = client.get(
        f"/workspaces/{ws}/nervous/assignment", params={"tool": "make_payment"}, headers=h
    ).json()
    assert money["escalate"] is True
    assert money["autonomy"] == "ESCALATE"
    assert money["plan_b"] and money["worst_case"]

    unknown = client.get(
        f"/workspaces/{ws}/nervous/assignment", params={"tool": "nope_xyz"}, headers=h
    ).json()
    assert unknown["escalate"] is True

    owner = client.get(f"/workspaces/{ws}/nervous/panel/order", headers=h).json()
    assert owner["role"] == "fulfillment"


def test_ledger_board_returns_state_columns(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    board = client.get(f"/workspaces/{ws}/ledger/board", headers=h).json()
    for col in ("queued", "pending_approval", "executing", "completed", "failed", "escalated"):
        assert col in board["counts"]
        assert col in board["lanes"]
    assert board["columns"]


def test_integrations_status_lists_adapters_without_faking_success(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    data = client.get(f"/workspaces/{ws}/integrations/status", headers=h).json()
    assert data["total"] >= 6
    assert data["adapters"]
    keys = {a["key"] for a in data["adapters"]}
    assert {"karma_payment", "sfexpress", "taobao", "douyin"} <= keys
    # with no credentials injected, nothing claims to be connected
    for a in data["adapters"]:
        assert a["status"] in ("ready_to_probe", "unavailable")
        assert "methods" in a


def test_ops_selfcheck_returns_summary_and_checks(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    data = client.get(f"/workspaces/{ws}/ops/selfcheck", headers=h).json()
    assert "summary" in data and "checks" in data
    assert data["summary"]["fail"] == 0
    names = {c["name"] for c in data["checks"]}
    assert any("数据库" in n for n in names)
    assert any("密钥" in n for n in names)


def test_system_logs_owner_readonly_structure(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.get(f"/workspaces/{ws}/system/logs?lines=10", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert {"file", "lines", "count", "empty"} <= set(data)
    assert isinstance(data["lines"], list)


def test_briefing_daily_and_weekly_local_aggregate(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    daily = client.get(f"/workspaces/{ws}/briefing?period=daily", headers=h).json()
    weekly = client.get(f"/workspaces/{ws}/briefing?period=weekly", headers=h).json()
    assert daily["period"] == "daily" and weekly["period"] == "weekly"
    assert {"orders_in_window", "revenue_local", "highlights", "caveat"} <= set(daily)
    assert "非平台对账" in daily["caveat"]


def test_briefing_snapshot_save_overwrites_same_day(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r1 = client.post(f"/workspaces/{ws}/briefing/snapshot", headers=h)
    assert r1.status_code == 200 and r1.json()["saved"] is True
    # 同日重复存档只覆盖，不新增第二行
    client.post(f"/workspaces/{ws}/briefing/snapshot", headers=h)
    listing = client.get(f"/workspaces/{ws}/briefing/snapshots", headers=h).json()
    rows = listing["snapshots"]
    assert len(rows) == 1
    assert rows[0]["date"]
    assert "revenue_local" in rows[0]

