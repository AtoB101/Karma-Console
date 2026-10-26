"""Tests for the MCP-compatible JSON-RPC 2.0 surface (app/mcp_server.py).

tools/call always delegates to the unified gateway, so permission/approval/
idempotency/audit are unchanged; these tests pin the envelope, the catalog
shape, secret suppression, pending-approval passthrough and isolation.
"""
import uuid


def _rpc(client, workspace, method, params=None, rpc_id=1, headers=None):
    h = headers or workspace["headers"]
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/workspaces/{workspace['workspace_id']}/mcp", json=body, headers=h)


def _make_workspace(client):
    email = f"mcp{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/users", json={"email": email, "display_name": "Owner"})
    user_id = r.json()["id"]
    key = client.post(f"/users/{user_id}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws_id = client.post(
        "/workspaces",
        json={"name": "MCP店", "region": "CN", "business_category": "clothing"},
        headers=headers,
    ).json()["id"]
    headers["X-Workspace-Id"] = ws_id
    return {"client": client, "headers": headers, "workspace_id": ws_id, "user_id": user_id}


def test_initialize_and_ping(client, workspace):
    r = _rpc(client, workspace, "initialize")
    assert r.status_code == 200
    data = r.json()["result"]
    assert data["protocolVersion"]
    assert data["serverInfo"]["name"]
    assert "tools" in data["capabilities"]

    r = _rpc(client, workspace, "ping")
    assert r.json()["result"] == {}


def test_tools_list_contains_builtin_shape(client, workspace):
    r = _rpc(client, workspace, "tools/list")
    tools = r.json()["result"]["tools"]
    assert isinstance(tools, list) and tools
    names = {t["name"] for t in tools}
    assert "list_products" in names
    item = next(t for t in tools if t["name"] == "list_products")
    assert item["inputSchema"]["type"] == "object"
    assert "readOnlyHint" in item["annotations"]
    assert item["annotations"]["readOnlyHint"] is True


def test_tools_list_hides_function_secrets(client, workspace):
    secret = f"bearer-secret-{uuid.uuid4().hex}"
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/functions",
        json={
            "name": "my_receipt_reader",
            "kind": "http_read",
            "endpoint_url": "https://example.com/receipt",
            "method": "GET",
            "auth": {"type": "bearer", "token": secret},
            "headers": {"X-Internal": "hidden-header"},
        },
        headers=workspace["headers"],
    )
    assert r.status_code == 201, r.text

    r = _rpc(client, workspace, "tools/list")
    raw = r.text
    assert secret not in raw
    assert "hidden-header" not in raw
    tools = r.json()["result"]["tools"]
    fn = next(t for t in tools if t["name"] == "my_receipt_reader")
    # No raw credential transport fields are advertised by the MCP catalog.
    assert "auth" not in fn
    assert "headers" not in fn
    assert "endpoint_url" not in fn
    assert fn["_meta"]["source"] == "function"


def test_tools_call_readonly_allowed(client, workspace):
    r = _rpc(client, workspace, "tools/call", {"name": "list_products", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["gateway"]["decision"] == "ALLOWED"
    assert result["content"][0]["type"] == "text"


def test_tools_call_pending_approval_passthrough(client, workspace):
    r = _rpc(client, workspace, "tools/call", {"name": "make_payment", "arguments": {"order_id": "x"}})
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["gateway"]["decision"] == "PENDING"
    assert result["approval_id"]


def test_tools_call_unknown_tool(client, workspace):
    r = _rpc(client, workspace, "tools/call", {"name": "no_such_tool_xyz", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["error"]["code"] == "UNKNOWN_TOOL"


def test_unknown_method_returns_rpc_error(client, workspace):
    r = _rpc(client, workspace, "fake/method")
    body = r.json()
    assert body["error"]["code"] == -32601


def test_invalid_params_missing_name(client, workspace):
    r = _rpc(client, workspace, "tools/call", {"arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_unauthenticated_rejected(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r.status_code == 401


def test_cross_workspace_isolation(client):
    ws_a = _make_workspace(client)
    ws_b = _make_workspace(client)
    secret = f"secret-{uuid.uuid4().hex}"
    client.post(
        f"/workspaces/{ws_a['workspace_id']}/functions",
        json={"name": "a_private_reader", "kind": "http_read", "auth": {"type": "bearer", "token": secret}},
        headers=ws_a["headers"],
    )
    # B must not see A's function or its secret.
    r = _rpc(client, ws_b, "tools/list")
    raw = r.text
    assert "a_private_reader" not in raw
    assert secret not in raw
    # A does see its own function.
    r = _rpc(client, ws_a, "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "a_private_reader" in names
