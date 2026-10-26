"""多模型注册表：脱敏、按 id 解析、密钥加密与兼容旧单模型的端点测试。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


def _del(w, path):
    return w["client"].delete(path, headers=w["headers"])


# ---------- 纯函数 ----------

def test_normalize_encrypts_key_and_keeps_existing():
    from app import model_registry as mr
    e = mr.normalize_entry({"label": "文案模型", "provider": "openai",
                            "model": "gpt-x", "api_key": "sk-secret-1234567890"})
    assert e["api_key_enc"] and "sk-secret" not in e["api_key_enc"]
    # 更新：不传 api_key 时保留已有密文；传新 api_key 则替换
    again = mr.normalize_entry({"model": "gpt-y"}, e)
    assert again["api_key_enc"] == e["api_key_enc"]
    fresh = mr.normalize_entry({"api_key": "sk-new-0000000000"}, e)
    assert fresh["api_key_enc"] != e["api_key_enc"]


def test_view_never_leaks_cipher_or_plaintext():
    from app import model_registry as mr
    e = mr.normalize_entry({"provider": "openai", "model": "gpt-x",
                            "api_key": "sk-secret-1234567890"})
    v = mr.view(e)
    assert "api_key_enc" not in v
    assert v["configured"] is True
    assert "sk-secret-1234567890" not in v["api_key"]
    assert v["api_key"]  # mask 非空


def test_resolve_by_id_and_fallback():
    from app import model_registry as mr
    a = mr.normalize_entry({"id": "a", "api_key": "sk-aaaa-1111111111", "model": "m-a"})
    b = mr.normalize_entry({"id": "b", "api_key": "sk-bbbb-2222222222", "model": "m-b"})
    entries = [a, b]
    r = mr.resolve(entries, "b")
    assert r["model"] == "m-b" and r["api_key"] == "sk-bbbb-2222222222"
    # 未配置密钥的 id 返回 None
    empty = mr.normalize_entry({"id": "c", "model": "m-c"})
    assert mr.resolve([empty], "c") is None
    # 无 id 回退首个已配置
    fb = mr.resolve([empty, a], None)
    assert fb["id"] == "a"


def test_migrate_legacy_keeps_cipher():
    from app import model_registry as mr
    legacy = {"api_key_enc": "cipher", "provider": "openai", "model": "gpt-x"}
    rows = mr.migrate_legacy(legacy)
    assert rows and rows[0]["api_key_enc"] == "cipher"
    assert mr.migrate_legacy(None) == []


# ---------- 端点 ----------

def test_models_list_empty(workspace):
    w = workspace
    r = w["client"].get(_url(w, "/models"), headers=w["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert "models" in body and "capabilities" in body


def test_models_upsert_mask_and_dedup(workspace):
    w = workspace
    r = _post(w, _url(w, "/models"), {
        "label": "文案模型", "provider": "openai", "model": "gpt-x",
        "base_url": "https://api.example.com", "capabilities": ["text"],
        "api_key": "sk-secret-1234567890",
    })
    assert r.status_code == 200, r.text
    mid = r.json()["model"]["id"]
    # 列表里密钥只 mask，无密文
    lst = w["client"].get(_url(w, "/models"), headers=w["headers"]).json()["models"]
    mine = [m for m in lst if m["id"] == mid]
    assert mine and mine[0]["configured"] is True
    assert "api_key_enc" not in mine[0]
    assert "sk-secret-1234567890" not in str(mine)
    # 同 id 更新不产生重复
    up = _post(w, _url(w, "/models"), {"id": mid, "model": "gpt-y"})
    assert up.status_code == 200, up.text
    lst2 = w["client"].get(_url(w, "/models"), headers=w["headers"]).json()["models"]
    assert sum(1 for m in lst2 if m["id"] == mid) == 1
    assert next(m for m in lst2 if m["id"] == mid)["model"] == "gpt-y"


def test_models_delete(workspace):
    w = workspace
    r = _post(w, _url(w, "/models"), {"label": "临时", "model": "m", "api_key": "sk-tmp-0000000000"})
    mid = r.json()["model"]["id"]
    d = _del(w, _url(w, f"/models/{mid}"))
    assert d.status_code == 200, d.text
    lst = w["client"].get(_url(w, "/models"), headers=w["headers"]).json()["models"]
    assert all(m["id"] != mid for m in lst)
    # 再删 404
    assert _del(w, _url(w, f"/models/{mid}")).status_code == 404
