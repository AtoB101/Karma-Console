"""Tests for data classification, redaction and the data-map endpoint."""
from app.data_classification import (
    DataTier,
    build_data_map,
    classify_field,
    classify_key,
    redact_mapping,
    redact_text,
    table_tier,
)


def test_classify_key_secret():
    assert classify_key("api_key") in (DataTier.SENSITIVE, DataTier.CRITICAL)
    assert classify_key("token") is DataTier.SENSITIVE
    assert classify_key("password") is DataTier.SENSITIVE
    assert classify_key("karma_api_key") is DataTier.CRITICAL
    assert classify_key("private_key") is DataTier.CRITICAL
    assert classify_key("price") is DataTier.INTERNAL


def test_classify_field_override():
    assert classify_field("workspaces", "settings") is DataTier.CRITICAL
    assert classify_field("connector_instances", "config") is DataTier.CRITICAL
    assert classify_field("customers", "profile") is DataTier.SENSITIVE
    assert classify_field("workspaces", "region") is DataTier.PUBLIC


def test_table_tier_defaults():
    assert table_tier("api_keys") is DataTier.CRITICAL
    assert table_tier("orders") is DataTier.SENSITIVE
    assert table_tier("region_policy_rules") is DataTier.PUBLIC
    assert table_tier("unknown_table") is DataTier.INTERNAL


def test_redact_mapping_masks_secrets():
    payload = {
        "api_key": "karma-secret-key-123456",
        "token": "abc",
        "name": "张三",
        "nested": {"authorization": "Bearer xyz", "ok": "visible"},
        "items": [{"password": "pw12345678", "price": 99.0}],
    }
    out = redact_mapping(payload)
    assert out["api_key"] == "****"
    assert out["token"] == "****"
    assert out["name"] == "****"
    assert out["nested"]["authorization"] == "****"
    assert out["nested"]["ok"] == "visible"
    assert out["items"][0]["password"] == "****"
    assert out["items"][0]["price"] == 99.0


def test_redact_text_masks_embedded_secrets():
    text = "api_key=sk-1234567890 call me at https://user:pass@example.com/x"
    out = redact_text(text)
    assert "sk-1234567890" not in out
    assert "user:pass@" not in out
    assert "****" in out


def test_data_map_endpoint(client, workspace):
    client.post(
        f"/workspaces/{workspace['workspace_id']}/products",
        json={"name": "棉麻衬衫", "category": "clothing", "price": 99.0},
        headers=workspace["headers"],
    )
    r = client.get(f"/workspaces/{workspace['workspace_id']}/data-map", headers=workspace["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == workspace["workspace_id"]
    assert body["region"] == "CN"
    assert body["redaction_enabled"] is True
    assert body["counts"]["products"] >= 1
    assert "workspaces" in body["tiers"]["CRITICAL"]
    assert any(f["table"] == "workspaces" and f["field"] == "settings" for f in body["sensitive_fields"])


def test_data_map_requires_auth(client):
    r = client.get("/workspaces/nope/data-map")
    assert r.status_code == 401
