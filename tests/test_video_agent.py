"""视频 Agent：分镜/参数单纯函数、凭证位、草稿待拍板测试。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


# ---------- 纯函数 ----------

def test_build_plan_pure():
    from app import video_agent as va
    p = va.build_video_plan("产品展示", "上班族", duration_sec=45, aspect="16:9")
    assert p["params"]["duration_sec"] == 45
    assert p["params"]["aspect"] == "16:9"
    assert len(p["storyboard"]) >= 1
    assert p["generation"]["status"] == "credential_required"
    assert "http" not in p["generation"].get("provider", "")


def test_aspect_fallback_and_duration_clamp():
    from app import video_agent as va
    p = va.build_video_plan("x", "", duration_sec=999, aspect="4:3")
    assert p["params"]["aspect"] == "9:16"
    assert p["params"]["duration_sec"] <= 180


def test_custom_storyboard_kept():
    from app import video_agent as va
    p = va.build_video_plan("t", "", shots=[{"shot": "开场", "voiceover": "你好", "duration_sec": 2}])
    assert p["storyboard"][0]["voiceover"] == "你好"


# ---------- 端点 ----------

def test_video_draft_endpoint(workspace):
    w = workspace
    r = _post(w, _url(w, "/content/video/draft"), {
        "topic": "棉麻衬衫展示", "audience": "女性", "duration_sec": 30,
        "aspect": "9:16", "style": "清新",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["kind"] == "video"
    assert body["item"]["status"] == "draft"
    assert body["generation"]["status"] == "credential_required"
    # 不伪造任何成片地址
    assert "video_url" not in body["item"]["assets"].get("plan", {})


def test_video_draft_not_publishable(workspace):
    w = workspace
    r = _post(w, _url(w, "/content/video/draft"), {"topic": "脚本"})
    cid = r.json()["item"]["id"]
    assert _post(w, _url(w, f"/content/{cid}/to-ready")).status_code == 409
