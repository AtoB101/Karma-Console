"""Skill marketplace: list, install, uninstall market-validated skills."""
def test_skills_catalog_lists_verified_skills(client, workspace):
    data = client.get("/skills", headers=workspace["headers"]).json()
    skills = data["skills"]
    assert len(skills) >= 10
    assert all(s["verified"] for s in skills)
    assert all(s["installed"] is False for s in skills)


def test_install_and_uninstall_skill(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    installed = client.post(f"/workspaces/{ws}/skills/product_listing/install", headers=h)
    assert installed.status_code == 201, installed.text
    body = installed.json()
    assert body["installed"] is True
    assert body["functions"] == ["product_listing"]

    skills = client.get("/skills", headers=h).json()["skills"]
    item = next(s for s in skills if s["id"] == "product_listing")
    assert item["installed"] is True

    layout = client.get(f"/workspaces/{ws}/layout", headers=h).json()["areas"]
    assert any(a["target_type"] == "skill" and a["target_id"] == "product_listing" for a in layout)

    catalog = client.get(f"/workspaces/{ws}/catalog", headers=h).json()["catalog"]
    assert any(c.get("type") == "function" and c.get("name") == "product_listing" for c in catalog)

    removed = client.post(f"/workspaces/{ws}/skills/product_listing/uninstall", headers=h)
    assert removed.status_code == 200
    assert removed.json()["installed"] is False

    skills = client.get("/skills", headers=h).json()["skills"]
    item = next(s for s in skills if s["id"] == "product_listing")
    assert item["installed"] is False


def test_unknown_skill_returns_404(client, workspace):
    r = client.post(f"/workspaces/{workspace['workspace_id']}/skills/nope/install", headers=workspace["headers"])
    assert r.status_code == 404


def test_installed_skill_is_really_invokable(client, workspace, monkeypatch):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "test-key"}},
        headers=h,
    )
    client.post(f"/workspaces/{ws}/skills/inventory_control/install", headers=h)

    import app.llm as llm
    monkeypatch.setattr(
        llm,
        "generate",
        lambda prompt="", context="", model_config=None: '{"steps":[{"tool":"list_inventory","params":{}}],"reply":"ok","done":true}',
    )
    r = client.post("/gateway/execute", json={"tool": "inventory_control", "params": {}}, headers=h).json()
    assert r["decision"] == "ALLOWED"
    assert r["result"]["kind"] == "agent"
    assert any(s["tool"] == "list_inventory" and s["decision"] == "ALLOWED" for s in r["result"]["steps"])


def test_installed_skill_enforces_allowed_tools(client, workspace, monkeypatch):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    client.put(
        f"/workspaces/{ws}/settings",
        json={"model": {"provider": "openai_compat", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "test-key"}},
        headers=h,
    )
    client.post(f"/workspaces/{ws}/skills/inventory_control/install", headers=h)

    import app.llm as llm
    monkeypatch.setattr(
        llm,
        "generate",
        lambda prompt="", context="", model_config=None: '{"steps":[{"tool":"make_payment","params":{"amount":1}}],"reply":"ok","done":true}',
    )
    r = client.post("/gateway/execute", json={"tool": "inventory_control", "params": {}}, headers=h).json()
    assert r["decision"] == "ALLOWED"
    denied = [s for s in r["result"]["steps"] if s["tool"] == "make_payment"]
    assert denied and denied[0]["decision"] == "DENIED"
