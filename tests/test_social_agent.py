"""社交运营 Agent：回复草稿、发布闸门、评论箱、热门建档测试。"""


def _url(w, suffix):
    return f"/workspaces/{w['workspace_id']}{suffix}"


def _post(w, path, body=None):
    return w["client"].post(path, headers=w["headers"], json=body or {})


# ---------- 纯函数 ----------

def test_sentiment_and_reply_pure():
    from app import social_agent as sa
    assert sa.sentiment("这个很好用，喜欢") == "positive"
    assert sa.sentiment("太差了，要退款投诉") == "negative"
    assert sa.sentiment("你好") == "neutral"
    d = sa.reply_draft("太贵了想退款", "negative")
    assert d["auto_sent"] is False
    assert "退款" not in d["draft"] or "私信" in d["draft"]


def test_publish_gate_pure():
    from app import social_agent as sa
    ok, _ = sa.publish_gate("draft", False)
    assert not ok
    ok, _ = sa.publish_gate("ready_to_publish", False)
    assert not ok  # 无渠道凭证拒绝
    ok, _ = sa.publish_gate("ready_to_publish", True)
    assert ok


def test_analyze_trending_pure():
    from app import social_agent as sa
    a = sa.analyze_trending("为什么这样？", {
        "views": 10000, "likes": 800, "comments": 300, "shares": 200})
    assert a["metrics"]["engagement"] == 1300
    assert a["hot_points"] and a["summary"]


# ---------- 端点：完整发布闸门流 ----------

def test_publish_blocked_without_channel(workspace):
    w = workspace
    r = _post(w, _url(w, "/content/copy/draft"), {"product": "衬衫", "selling_points": ["柔软"]})
    cid = r.json()["item"]["id"]
    _post(w, _url(w, f"/content/{cid}/submit"))
    _post(w, _url(w, f"/content/{cid}/approve"), {"note": "ok"})
    _post(w, _url(w, f"/content/{cid}/to-ready"))
    q = w["client"].get(_url(w, "/social/publish-queue"), headers=w["headers"]).json()
    assert any(i["id"] == cid for i in q["queue"])
    assert q["channel_configured"] is False
    p = _post(w, _url(w, f"/content/{cid}/publish"))
    assert p.status_code == 409, p.text  # 无凭证拒绝，不伪造成功


# ---------- 评论箱 ----------

def test_comment_record_and_approve_reply(workspace):
    w = workspace
    r = _post(w, _url(w, "/social/comments"), {"author": "甲", "text": "这个很好看，喜欢"})
    assert r.status_code == 200, r.text
    c = r.json()["comment"]
    assert c["sentiment"] == "positive"
    assert c["reply_draft"] and c["status"] == "draft_reply"
    scid = c["id"]
    a = _post(w, _url(w, f"/social/comments/{scid}/approve-reply"), {})
    assert a.status_code == 200, a.text
    assert a.json()["comment"]["status"] == "approved_reply"
    lst = w["client"].get(_url(w, "/social/comments"), headers=w["headers"]).json()
    assert any(x["id"] == scid for x in lst["comments"])


# ---------- 热门建档 ----------

def test_trending_archive(workspace):
    w = workspace
    r = _post(w, _url(w, "/social/trending"), {
        "platform": "douyin", "title": "为什么你一直踩坑？",
        "url": "https://x/v", "metrics": {"views": 20000, "likes": 1000, "comments": 600, "shares": 300},
    })
    assert r.status_code == 200, r.text
    v = r.json()["video"]
    assert v["hot_points"] and v["summary"]
    lst = w["client"].get(_url(w, "/social/trending"), headers=w["headers"]).json()
    assert any(x["id"] == v["id"] for x in lst["videos"])
