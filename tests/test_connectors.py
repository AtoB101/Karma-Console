import asyncio
def _install(client, ws_id, headers, ctype, name):
    return client.post(
        f"/workspaces/{ws_id}/connectors",
        json={"connector_type": ctype, "name": name},
        headers=headers,
    )


def test_ecommerce_order_returns_receipt(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = _install(client, ws, h, "ecommerce_independent", "独立站")
    assert r.status_code == 201

    r = client.post("/gateway/execute", json={"tool": "create_order", "params": {"channel": "ecommerce_independent", "items": [{"sku": "A1"}]}, "idempotency_key": "ord-1"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["receipt_status"] == "SUCCEEDED"
    assert body["result"]["verification"]["status"] == "SUCCEEDED"


def test_default_connectors_installed_on_workspace_creation(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.get(f"/workspaces/{ws}/connectors", headers=h)
    assert r.status_code == 200
    types = {c["connector_type"] for c in r.json()["connectors"]}
    assert {"karma_payment", "ecommerce_independent", "ecommerce_social"} <= types

    r = client.post(
        "/gateway/execute",
        json={"tool": "create_order", "params": {"channel": "ecommerce_independent", "items": [{"sku": "A1"}]}, "idempotency_key": "ord-auto"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["receipt_status"] == "SUCCEEDED"


def test_ecommerce_real_http_create_order_verifies(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        f"/workspaces/{ws}/connectors",
        json={
            "connector_type": "ecommerce_independent",
            "name": "独立站",
            "config": {
                "endpoint_url": "https://shop.example.com/orders",
                "method": "POST",
                "verification": {"status_path": "status", "reference_path": "order_id"},
            },
        },
        headers=h,
    )
    assert r.status_code == 201

    calls = []

    async def fake_request(method, url, *, headers=None, json_body=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "json": json_body})
        return {"status": 200, "headers": {}, "json": {"status": "PAID", "order_id": "ord-777"}, "text": ""}

    monkeypatch.setattr("app.functions.request_http", fake_request)

    r = client.post(
        "/gateway/execute",
        json={"tool": "create_order", "params": {"channel": "ecommerce_independent", "items": [{"sku": "A1"}]}, "idempotency_key": "ord-1"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["receipt_status"] == "PAID"
    assert body["result"]["order_reference"] == "ord-777"
    assert body["result"]["verification"]["status"] == "SUCCEEDED"
    assert calls[0]["url"] == "https://shop.example.com/orders"
    assert calls[0]["json"]["items"] == [{"sku": "A1"}]


def test_ecommerce_cancel_order_stub(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    assert _install(client, ws, h, "ecommerce_independent", "独立站").status_code == 201

    r = client.post(
        "/gateway/execute",
        json={"tool": "cancel_order", "params": {"channel": "ecommerce_independent", "order_reference": "ord-1"}},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["receipt_status"] == "SUCCEEDED"
    assert body["result"]["verification"]["status"] == "SUCCEEDED"


def test_signature_determinism():
    from app.signing import sign_douyin, sign_sf, sign_taobao

    taobao_params = {"method": "taobao.trade.create", "app_key": "1000", "timestamp": "2026-01-01 00:00:00", "format": "json", "v": "2.0", "sign_method": "md5", "session": "s1"}
    sign1 = sign_taobao(taobao_params, "secret")
    sign2 = sign_taobao(taobao_params, "secret")
    assert sign1 == sign2
    assert len(sign1) == 32 and sign1 == sign1.upper()

    dy = {"method": "order.list", "app_id": "a1", "timestamp": "1700000000", "param_json": "{}", "v": "2", "sign_method": "hmac-sha256"}
    assert sign_douyin(dy, "secret", "hmac-sha256") == sign_douyin(dy, "secret", "hmac_sha256")
    assert len(sign_douyin(dy, "secret", "hmac-sha256")) == 64
    assert len(sign_douyin(dy, "secret", "md5")) == 32

    sf = sign_sf('{"a":1}', "1700000000000", "check")
    assert sf and sf == sign_sf('{"a":1}', "1700000000000", "check")


def test_config_secret_encryption_roundtrip():
    from app.connectors import decrypt_config_secrets, encrypt_config_secrets

    cfg = encrypt_config_secrets({"app_secret": "s1", "checkword": "c1", "access_token": "t1", "app_key": "k1"})
    assert "app_secret" not in cfg and "checkword" not in cfg and "access_token" not in cfg
    assert cfg["app_key"] == "k1"
    assert cfg["app_secret_enc"] != "s1"
    out = decrypt_config_secrets(cfg)
    assert out["app_secret"] == "s1"
    assert out["checkword"] == "c1"
    assert out["access_token"] == "t1"
    assert out["app_key"] == "k1"


def test_signed_taobao_connector_executes(client, workspace, monkeypatch):
    from app.signing import sign_taobao

    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        f"/workspaces/{ws}/connectors",
        json={
            "connector_type": "ecommerce_marketplace",
            "name": "taobao",
            "config": {
                "signer": "taobao",
                "endpoint_url": "https://gw.api.taobao.com/router/rest",
                "app_key": "1000",
                "app_secret": "secret",
                "actions": {"create_order": {"method": "taobao.trade.create"}},
            },
        },
        headers=h,
    )
    assert r.status_code == 201

    calls = []

    async def fake_request(method, url, *, headers=None, json_body=None, params=None, form_body=None, timeout=None):
        calls.append({"method": method, "url": url, "form": form_body})
        return {"status": 200, "headers": {}, "json": {"tid": "TB1001", "status": "SUCCESS"}, "text": ""}

    monkeypatch.setattr("app.functions.request_http", fake_request)

    r = client.post(
        "/gateway/execute",
        json={"tool": "create_order", "params": {"channel": "ecommerce_marketplace", "items": [{"sku": "A1"}]}, "idempotency_key": "tb-1"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    assert body["result"]["receipt_status"] == "SUCCEEDED"
    assert body["result"]["order_reference"] == "TB1001"
    assert body["result"]["verification"]["status"] == "SUCCEEDED"
    sent = calls[0]["form"]
    assert sent["method"] == "taobao.trade.create"
    assert sent["app_key"] == "1000"
    assert sent["sign"]
    params_without_sign = {k: v for k, v in sent.items() if k != "sign"}
    assert sent["sign"] == sign_taobao(params_without_sign, "secret")


def test_signed_douyin_read(monkeypatch):
    from app.connectors import SignedApiConnector

    calls = []

    async def fake_request(method, url, *, headers=None, json_body=None, params=None, form_body=None, timeout=None):
        calls.append({"method": method, "url": url, "form": form_body})
        return {"status": 200, "headers": {}, "json": {"err_no": 0, "data": {"order_id": "DY1"}}, "text": ""}

    monkeypatch.setattr("app.functions.request_http", fake_request)
    conn = SignedApiConnector(
        "ecommerce_marketplace",
        "douyin",
        config={
            "endpoint_url": "https://openapi-fxg.jinritemai.com/router",
            "app_id": "app123",
            "app_secret": "secret456",
            "access_token": "tok",
            "actions": {"read": {"method": "order.list"}},
        },
    )
    result = asyncio.run(conn.read({"order_status": 10}))
    assert result["connected"] is True
    assert result["body"]["data"]["order_id"] == "DY1"
    sent = calls[0]["form"]
    assert sent["method"] == "order.list"
    assert sent["app_id"] == "app123"
    assert sent["param_json"]
    assert sent["sign"]


def test_signed_sfexpress_receipt_tool(client, workspace, monkeypatch):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        f"/workspaces/{ws}/connectors",
        json={
            "connector_type": "logistics",
            "name": "sf",
            "config": {
                "signer": "sfexpress",
                "endpoint_url": "https://bsp-oisp.sf-express.com/bsp-oisp/sfexpressService",
                "partner_id": "CLIENT",
                "checkword": "secret789",
                "actions": {"read": {"service_code": "EXP_RECE_SEARCH"}},
            },
        },
        headers=h,
    )
    assert r.status_code == 201

    calls = []

    async def fake_request(method, url, *, headers=None, json_body=None, params=None, form_body=None, timeout=None):
        calls.append({"method": method, "url": url, "form": form_body})
        return {"status": 200, "headers": {}, "json": {"apiResultCode": "A1000", "waybillNo": "SF888"}, "text": ""}

    monkeypatch.setattr("app.functions.request_http", fake_request)

    r = client.post(
        "/gateway/execute",
        json={"tool": "get_channel_receipt", "params": {"channel": "logistics", "params": {"tracking_no": "SF888"}}},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOWED"
    result = body["result"]
    assert result["connected"] is True
    assert result["body"]["waybillNo"] == "SF888"
    sent = calls[0]["form"]
    assert sent["partnerID"] == "CLIENT"
    assert sent["serviceCode"] == "EXP_RECE_SEARCH"
    assert sent["msgDigest"]


def test_signed_secrets_masked_in_listing(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.post(
        f"/workspaces/{ws}/connectors",
        json={
            "connector_type": "logistics",
            "name": "sf",
            "config": {"signer": "sfexpress", "endpoint_url": "https://bsp-oisp.sf-express.com/x", "partner_id": "CLIENT", "checkword": "topsecret"},
        },
        headers=h,
    )
    assert r.status_code == 201
    r = client.get(f"/workspaces/{ws}/connectors", headers=h)
    sf = next(c for c in r.json()["connectors"] if c["connector_type"] == "logistics")
    assert sf["signer"] == "sfexpress"
    assert sf["has_secret"] is True
    assert sf["partner_id"] == "CLIENT"
    assert sf["actions"] == {}
    assert "topsecret" not in str(sf)
    assert "checkword_enc" not in str(sf)


def test_configure_generic_http_connector_roundtrip_masks_token(client, workspace):
    """PUT /connectors/{type} 登记通用 HTTP 渠道：令牌加密落库、列表不回显明文。"""
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.put(
        f"/workspaces/{ws}/connectors/custom_logistics",
        json={
            "name": "自建物流",
            "endpoint_url": "https://api.example.com/logistics",
            "method": "POST",
            "auth": {"type": "bearer"},
            "access_token": "plain-should-not-echo",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint_url"] == "https://api.example.com/logistics"
    assert body["method"] == "POST"
    assert body["auth_type"] == "bearer"

    listing = client.get(f"/workspaces/{ws}/connectors", headers=h).json()["connectors"]
    row = next(c for c in listing if c["connector_type"] == "custom_logistics")
    assert row["configured"] is True
    assert row["has_secret"] is True
    assert "plain-should-not-echo" not in str(row)
    assert "access_token_enc" not in str(row)
