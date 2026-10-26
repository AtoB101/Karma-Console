"""P19 inbound boundary guard: empty / oversized messages are rejected
deterministically before any model call or action."""
from app.input_guard import MAX_INBOUND_CHARS, sanitize_inbound


def test_none_rejected():
    cleaned, err = sanitize_inbound(None)
    assert err and cleaned == ""


def test_empty_and_whitespace_rejected():
    assert sanitize_inbound("")[1]
    assert sanitize_inbound("   \n\t  ")[1]


def test_oversized_rejected_without_truncation():
    msg = "字" * (MAX_INBOUND_CHARS + 1)
    cleaned, err = sanitize_inbound(msg)
    assert err and "过长" in err
    assert len(cleaned) == MAX_INBOUND_CHARS + 1


def test_at_limit_accepted():
    msg = "字" * MAX_INBOUND_CHARS
    cleaned, err = sanitize_inbound(msg)
    assert err is None and cleaned == msg


def test_normal_accepted_and_trimmed():
    cleaned, err = sanitize_inbound("  帮我看看库存  ")
    assert err is None and cleaned == "帮我看看库存"


def test_empty_message_via_api(client, workspace):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/chat",
        json={"message": "   "},
        headers=workspace["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "rejected"
    assert body["actions"] == []
