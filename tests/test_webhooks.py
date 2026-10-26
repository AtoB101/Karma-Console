import app.webhooks as wh


def test_parse_telegram():
    norm = wh.normalize("telegram", {
        "message": {
            "chat": {"id": 12345},
            "from": {"first_name": "张三", "last_name": "三"},
            "text": "你好",
        }
    })
    assert norm["channel"] == "telegram"
    assert norm["external_id"] == "12345"
    assert norm["message"] == "你好"
    assert norm["customer_name"] == "张三 三"


def test_parse_wechat_xml():
    norm = wh.normalize("wechat", {
        "xml": "<xml><FromUserName><![CDATA[user_1]]></FromUserName><Content><![CDATA[在吗]]></Content></xml>"
    })
    assert norm["channel"] == "wechat"
    assert norm["external_id"] == "user_1"
    assert norm["message"] == "在吗"


def test_telegram_webhook_auto_reply(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"chat": {"enabled": True, "provider": "stub", "token": "tok123"}},
        headers=h,
    )

    r = client.post("/webhooks/telegram/tok123", json={
        "message": {
            "chat": {"id": 999},
            "from": {"first_name": "李四"},
            "text": "看看库存",
        }
    })
    body = r.json()
    assert body["ok"] is True
    assert body["workspace_id"] == ws
    assert body.get("reply")
    assert body["outbound"]["delivery"]["provider"] == "stub"

    customers = client.get(f"/workspaces/{ws}/customers", headers=h).json()["customers"]
    assert any(c["channel"] == "telegram" and c["external_id"] == "999" for c in customers)


def test_webhook_rejects_bad_token(client):
    r = client.post("/webhooks/telegram/bad-token", json={
        "message": {"chat": {"id": 1}, "from": {"first_name": "X"}, "text": "hi"}
    })
    body = r.json()
    assert body["ok"] is False
    assert "token" in body.get("error", "")

import hashlib
import hmac
import json as _json


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_webhook_rejects_bad_hmac_signature(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"chat": {"enabled": True, "provider": "stub", "token": "tok456", "webhook_secret": "s3cret"}},
        headers=h,
    )
    payload = {"channel": "webhook", "external_id": "9", "message": "看看库存"}
    body = _json.dumps(payload).encode("utf-8")

    bad = client.post("/webhooks/generic/tok456", content=body, headers={"X-Krama-Signature": "sha256=" + "0" * 64})
    assert bad.json()["ok"] is False
    assert "signature" in bad.json()["error"]

    good = client.post("/webhooks/generic/tok456", content=body, headers={"X-Krama-Signature": _sign("s3cret", body)})
    assert good.json()["ok"] is True


def test_telegram_webhook_secret_token(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"chat": {"enabled": True, "provider": "stub", "token": "tok789", "webhook_secret": "tgsec"}},
        headers=h,
    )
    body = _json.dumps({"message": {"chat": {"id": 888}, "from": {"first_name": "王五"}, "text": "看看库存"}}).encode("utf-8")

    missing = client.post("/webhooks/telegram/tok789", content=body, headers={})
    assert missing.json()["ok"] is False
    assert "signature" in missing.json()["error"]

    wrong = client.post("/webhooks/telegram/tok789", content=body, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert wrong.json()["ok"] is False

    ok = client.post("/webhooks/telegram/tok789", content=body, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsec"})
    assert ok.json()["ok"] is True
