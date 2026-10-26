"""文案写作 Agent：本地卖点骨架、合规提醒、草稿待拍板测试。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


# ---------- 纯函数 ----------

def test_build_brief_pure():
    from app import copywriter as cw
    b = cw.build_copy_brief(
        product="棉麻衬衫", audience="25-35 岁女性",
        selling_points=["透气不闷", "不易起皱"], tone="清新")
    assert len(b["headlines"]) == 3
    assert "透气不闷" in b["points"]
    assert b["versions"]["detail"] and b["versions"]["short"] and b["versions"]["social"]
    assert b["compliance_notes"] == []


def test_compliance_watch_hits():
    from app import copywriter as cw
    b = cw.build_copy_brief("某某神器", "上班族", ["全网最佳选择"])
    assert "最佳" in b["compliance_notes"]


def test_empty_points_no_fabrication():
    from app import copywriter as cw
    b = cw.build_copy_brief("新品", "", [])
    assert b["points"] == []
    assert "待补充" in b["point_lines"][0]


# ---------- 端点 ----------

def test_copy_draft_endpoint(workspace):
    w = workspace
    r = _post(w, _url(w, "/content/copy/draft"), {
        "product": "棉麻衬衫", "audience": "25-35 岁女性",
        "selling_points": ["透气不闷", "不易起皱"], "tone": "清新",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    item = body["item"]
    assert item["kind"] == "copy"
    assert item["status"] == "draft"
    assert "透气不闷" in item["body"]
    assert body["model_configured"] is False
    assert "compliance_notes" in body


def test_copy_draft_not_publishable_before_approval(workspace):
    w = workspace
    r = _post(w, _url(w, "/content/copy/draft"), {"product": "衬衫", "selling_points": ["柔软"]})
    cid = r.json()["item"]["id"]
    # draft 不可交接待发布
    assert _post(w, _url(w, f"/content/{cid}/to-ready")).status_code == 409


def test_registered_model_recognized_by_draft(workspace):
    """登记一条已配密钥模型后，文案草稿按 model_id 识别为已配置；列表不泄漏密文。"""
    w = workspace
    reg = _post(w, _url(w, "/models"), {
        "label": "文案主力", "provider": "openai", "model": "gpt-x",
        "api_key": "sk-secret-1234567890", "capabilities": ["TEXT"],
    })
    assert reg.status_code == 200, reg.text
    mid = reg.json()["model"]["id"]
    # 脱敏列表：无密文、无明文密钥
    lst = w["client"].get(_url(w, "/models"), headers=w["headers"]).json()["models"]
    assert "api_key_enc" not in lst[0]
    assert "sk-secret-1234567890" not in str(lst)
    # 草稿带 model_id → model_configured True
    r = _post(w, _url(w, "/content/copy/draft"), {
        "product": "衬衫", "selling_points": ["柔软"], "model_id": mid})
    assert r.status_code == 200, r.text
    assert r.json()["model_configured"] is True
