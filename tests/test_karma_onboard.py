"""Karma 结算接入向导：状态机纯函数 + 端点闭环（认证登记 / 自检 / 解除绑定）。"""
import pytest

from app.karma_onboard import derive_state


# ---------------- 状态机纯函数 ----------------

def test_state_not_started():
    st = derive_state(None)
    assert st["overall"] == "not_started"
    assert st["steps"][0]["id"] == "kyc" and st["steps"][0]["current"] is True
    assert st["steps"][4]["done"] is False


def test_state_kyc_confirmed_only():
    st = derive_state({"kyc_confirmed": True})
    assert st["overall"] == "kyc"
    assert st["steps"][0]["done"] is True
    assert st["steps"][1]["current"] is True


def test_state_identity_created_but_no_key():
    st = derive_state({"kyc_confirmed": True, "identity_id": "id-1", "agent_id": "ag-1"})
    assert st["overall"] == "kyc"  # 尚无 api_key，整体仍未激活
    assert st["steps"][1]["done"] is True
    assert st["steps"][2]["current"] is True


def test_state_bound_not_tested():
    st = derive_state({"kyc_confirmed": True, "identity_id": "id-1", "agent_id": "ag-1", "api_key_enc": "x"})
    assert st["overall"] == "bound"
    assert st["steps"][3]["done"] is True
    assert st["steps"][4]["current"] is True


def test_state_active():
    st = derive_state({"kyc_confirmed": True, "identity_id": "id-1", "agent_id": "ag-1",
                       "api_key_enc": "x", "test_ok": True})
    assert st["overall"] == "active"
    assert all(s["done"] for s in st["steps"])
    assert st["summary"]["tested"] is True


# ---------------- 端点闭环 ----------------

def test_onboard_endpoints_roundtrip(workspace):
    client, headers = workspace["client"], workspace["headers"]
    wid = workspace["workspace_id"]

    # 初始：未开始
    r = client.get(f"/workspaces/{wid}/karma/onboard", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["overall"] == "not_started"
    assert body["oauth_callback"]["supported"] is False  # 联调预留位，不伪造

    # 确认已实名认证
    r = client.post(f"/workspaces/{wid}/karma/onboard/confirm-kyc", headers=headers)
    assert r.status_code == 200
    assert r.json()["overall"] == "kyc"

    # 无凭证时自检：明确 not_connected，不伪造成功
    r = client.post(f"/workspaces/{wid}/karma/onboard/test", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["error"] == "no_api_key"

    # 写入凭证（走设置面板加密保存）
    r = client.put(f"/workspaces/{wid}/settings", headers=headers,
                   json={"karma": {"base_url": "https://api.karma.xyz/v1", "api_key": "k_test_secret",
                                   "agent_id": "ag-1", "identity_id": "id-1"}})
    assert r.status_code == 200
    out = r.json()["karma"]
    assert "k_test_secret" not in str(out)  # 密钥不原样回传

    r = client.get(f"/workspaces/{wid}/karma/onboard", headers=headers)
    assert r.json()["overall"] == "bound"

    # 自检：monkeypatch KarmaClient 避免真实外呼
    class FakeClient:
        def __init__(self, *a, **kw):
            self.agent_id = kw.get("agent_id", "")
            self.identity_id = kw.get("identity_id", "")

        def exchange_token(self):
            return "fake-token"

        def get_agent(self, aid):
            return {"id": aid, "name": "test-agent"}

        def get_capacity(self, iid):
            return {"identity_id": iid, "available": 1000}

        def close(self):
            pass

    import app.karma as karma_mod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(karma_mod, "KarmaClient", FakeClient)
    try:
        r = client.post(f"/workspaces/{wid}/karma/onboard/test", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["token_exchanged"] is True
        assert body["agent"]["id"] == "ag-1"
    finally:
        monkey.undo()

    # 自检通过后：active
    r = client.get(f"/workspaces/{wid}/karma/onboard", headers=headers)
    assert r.json()["overall"] == "active"

    # 解除绑定 → 回到未开始（凭证清除，审计留痕）
    r = client.post(f"/workspaces/{wid}/karma/onboard/unlink", headers=headers)
    assert r.status_code == 200
    assert r.json()["overall"] == "not_started"
    r = client.get(f"/workspaces/{wid}/settings", headers=headers)
    assert r.json()["karma"].get("api_key") in (None, "")


def test_onboard_test_credential_failure_is_not_faked(workspace):
    """凭证无效时自检必须返回 not_connected + invalid_credentials，绝不伪造成功。"""
    client, headers = workspace["client"], workspace["headers"]
    wid = workspace["workspace_id"]

    client.put(f"/workspaces/{wid}/settings", headers=headers,
               json={"karma": {"api_key": "k_bad_key", "agent_id": "ag-x", "identity_id": "id-x"}})

    class RejectClient:
        def __init__(self, *a, **kw):
            pass

        def exchange_token(self):
            from app.karma import KarmaClientError
            raise KarmaClientError("POST /v1/auth/token -> 401: invalid api key")

        def close(self):
            pass

    import app.karma as karma_mod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(karma_mod, "KarmaClient", RejectClient)
    try:
        r = client.post(f"/workspaces/{wid}/karma/onboard/test", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is False
        assert body["error"] == "invalid_credentials"
    finally:
        monkey.undo()
