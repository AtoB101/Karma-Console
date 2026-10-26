"""统一渠道账号接入：目录、加密入库、脱敏、状态判定、启停、删除。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


# ---------- 纯内核 ----------

def test_catalog_covers_main_channels():
    from app import channels as ch
    types = {c["type"] for c in ch.catalog()}
    for need in ["webchat", "telegram", "whatsapp_cloud", "instagram", "messenger",
                 "line", "slack", "discord", "email", "wecom", "douyin", "kuaishou", "sms"]:
        assert need in types
    # 个人号非官方桥接不得收录
    assert not ch.is_known("whatsapp_personal")
    assert not ch.is_known("wechat_personal")


def test_probe_status_no_network():
    from app.channels import probe_status, ST_CONFIGURED, ST_CREDENTIAL_REQUIRED, ST_ERROR
    assert probe_status("") == ST_CREDENTIAL_REQUIRED
    assert probe_status("enc::xyz") == ST_CONFIGURED
    assert probe_status("enc::xyz", last_error="401 unauthorized") == ST_ERROR


# ---------- 端点 ----------

def test_catalog_endpoint(workspace):
    w = workspace
    r = w["client"].get(_url(w, "/channels/catalog"), headers=w["headers"])
    assert r.status_code == 200, r.text
    assert len(r.json()["channels"]) >= 10


def test_create_list_toggle_probe_delete(workspace):
    w = workspace
    # 未知渠道 400
    bad = _post(w, _url(w, "/channels"), {"channel_type": "hacker_bot"})
    assert bad.status_code == 400

    # 创建一个带凭证的 telegram
    r = _post(w, _url(w, "/channels"), {
        "channel_type": "telegram", "account_label": "客服小助手",
        "identifier": "@my_bot", "credential": "123:ABCDEFSECRET",
    })
    assert r.status_code == 200, r.text
    ch = r.json()["channel"]
    cid = ch["id"]
    # 绝不回传密文
    assert "credential_enc" not in ch
    assert ch["has_credential"] is True
    assert ch["status"] == "configured"

    # 列表不含密文
    lst = w["client"].get(_url(w, "/channels"), headers=w["headers"])
    assert lst.status_code == 200
    row = [x for x in lst.json()["channels"] if x["id"] == cid][0]
    assert "credential_enc" not in row

    # toggle 停用
    t = _post(w, _url(w, f"/channels/{cid}/toggle"))
    assert t.status_code == 200
    assert t.json()["channel"]["enabled"] is False

    # probe 不联网，仍 configured
    p = _post(w, _url(w, f"/channels/{cid}/probe"))
    assert p.status_code == 200
    assert p.json()["channel"]["status"] == "configured"
    assert p.json()["channel"]["last_checked_at"]

    # 删除
    d = w["client"].delete(_url(w, f"/channels/{cid}"), headers=w["headers"])
    assert d.status_code == 200, d.text
    after = w["client"].get(_url(w, "/channels"), headers=w["headers"])
    assert all(x["id"] != cid for x in after.json()["channels"])


def test_create_without_credential_marks_required(workspace):
    w = workspace
    r = _post(w, _url(w, "/channels"), {
        "channel_type": "webchat", "account_label": "官网挂件",
    })
    assert r.status_code == 200, r.text
    ch = r.json()["channel"]
    assert ch["has_credential"] is False
    assert ch["status"] == "credential_required"
    # 清理
    w["client"].delete(_url(w, f"/channels/{ch['id']}"), headers=w["headers"])
