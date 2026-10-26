import socket

import pytest

import app.crypto as crypto
import app.functions as functions


def test_crypto_roundtrip_and_mask():
    secret = "sk-abcdef123456"
    token = crypto.encrypt(secret)
    assert token and token != secret
    assert crypto.decrypt(token) == secret
    assert crypto.decrypt("not-a-token") == ""
    assert crypto.mask(secret) == "sk-a...3456"
    assert crypto.mask("short") == "****"
    h = crypto.hash_value("abc")
    assert crypto.compare_hashes(h, crypto.hash_value("abc")) is True
    assert crypto.compare_hashes(h, crypto.hash_value("xyz")) is False


def test_settings_stored_encrypted(client, workspace):
    ws_id = workspace["workspace_id"]
    h = workspace["headers"]
    r = client.put(
        f"/workspaces/{ws_id}/settings",
        json={"model": {"api_key": "sk-secret-key", "model": "m1"},
              "chat": {"provider": "telegram", "token": "bot-secret-token", "enabled": True}},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["model"]["api_key"] == "sk-s...-key"

    from app.db import SessionLocal
    from app.models import Workspace

    db = SessionLocal()
    try:
        ws = db.get(Workspace, ws_id)
        model = ws.settings["model"]
        chat_cfg = ws.settings["chat"]
    finally:
        db.close()
    assert "api_key" not in model
    assert crypto.decrypt(model["api_key_enc"]) == "sk-secret-key"
    assert "token" not in chat_cfg
    assert crypto.decrypt(chat_cfg["token_enc"]) == "bot-secret-token"
    assert chat_cfg["token_hash"] == crypto.hash_value("bot-secret-token")


def test_chat_inbound_still_works_after_encryption(client, workspace):
    ws_id = workspace["workspace_id"]
    h = workspace["headers"]
    client.put(f"/workspaces/{ws_id}/settings", json={"chat": {"provider": "telegram", "token": "bot-enc-1", "enabled": True}}, headers=h)
    r = client.post("/chat/inbound", json={"token": "bot-enc-1", "message": "写一段棉麻衬衫的营销文案"})
    assert r.status_code == 200
    assert "mock" in r.json()["reply"]


def test_inbound_rate_limit(client, monkeypatch):
    from app.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr("app.ratelimit.limiter_inbound", SlidingWindowLimiter(2, 60))
    for _ in range(2):
        client.post("/chat/inbound", json={"token": "x", "message": "hi"})
    r = client.post("/chat/inbound", json={"token": "x", "message": "hi"})
    assert r.status_code == 429


def test_ssrf_resolves_private_address(monkeypatch):
    monkeypatch.setattr(
        functions.socket,
        "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(functions.FunctionError):
        functions.validate_url("https://public-looking.example.com/x")
    assert functions.validate_url("https://public-looking.example.com/x", allow_private=True)


def test_ssrf_allows_unresolvable_host(monkeypatch):
    def raise_gaierror(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(functions.socket, "getaddrinfo", raise_gaierror)
    assert functions.validate_url("https://unresolvable.example.com/x") == "https://unresolvable.example.com/x"