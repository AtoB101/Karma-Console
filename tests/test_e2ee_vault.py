"""P42 owner-only E2EE vault: server stores ciphertext, holds no key, blind index exact match."""
from __future__ import annotations

import uuid

import app.e2ee_vault as vault


def _make_ws(client):
    email = f"e2ee{uuid.uuid4().hex[:10]}@example.com"
    uid = client.post("/users", json={"email": email, "display_name": "Owner"}).json()["id"]
    key = client.post(f"/users/{uid}/api-keys").json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    ws = client.post(
        "/workspaces", json={"name": "加密店", "region": "CN", "business_category": "general"}, headers=headers
    ).json()["id"]
    headers["X-Workspace-Id"] = ws
    return {"headers": headers, "workspace_id": ws}


def test_server_holds_no_decrypt_routine():
    # Boundary: the vault module must never carry a key or decrypt function.
    assert not hasattr(vault, "decrypt")
    assert not hasattr(vault, "secret_key")
    assert not hasattr(vault, "master_key")


def test_store_then_fetch_envelope_roundtrip(client):
    a = _make_ws(client)
    payload = {
        "owner_ref": "customer:c1", "field": "real_name",
        "ciphertext": "ENC::abc123", "nonce": "nonce-1", "alg": "aes-gcm",
        "blind_index": "bi-name-1",
    }
    r = client.put(f"/workspaces/{a['workspace_id']}/e2ee", json=payload, headers=a["headers"])
    assert r.status_code == 200
    got = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    )
    assert got.status_code == 200
    body = got.json()
    assert body["ciphertext"] == "ENC::abc123"
    assert body["nonce"] == "nonce-1"


def test_upsert_updates_in_place_no_duplicate(client):
    a = _make_ws(client)
    base = {"owner_ref": "customer:c1", "field": "real_name", "nonce": "n", "alg": "aes-gcm"}
    client.put(f"/workspaces/{a['workspace_id']}/e2ee", json={**base, "ciphertext": "v1"}, headers=a["headers"])
    client.put(f"/workspaces/{a['workspace_id']}/e2ee", json={**base, "ciphertext": "v2"}, headers=a["headers"])
    got = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    ).json()
    assert got["ciphertext"] == "v2"


def test_blind_index_exact_match(client):
    a = _make_ws(client)
    client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "customer:c1", "field": "real_phone", "ciphertext": "x", "nonce": "n", "blind_index": "bi-phone-9"},
        headers=a["headers"],
    )
    hit = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee/find",
        params={"field": "real_phone", "blind_index": "bi-phone-9"}, headers=a["headers"],
    ).json()
    assert [m["owner_ref"] for m in hit["matches"]] == ["customer:c1"]
    miss = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee/find",
        params={"field": "real_phone", "blind_index": "bi-phone-0"}, headers=a["headers"],
    ).json()
    assert miss["matches"] == []


def test_reject_unknown_field_and_empty_ciphertext(client):
    a = _make_ws(client)
    bad_field = client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "c1", "field": "plain_name", "ciphertext": "x"}, headers=a["headers"],
    )
    assert bad_field.status_code == 400
    empty = client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "c1", "field": "real_name", "ciphertext": ""}, headers=a["headers"],
    )
    assert empty.status_code == 400


def test_e2ee_isolated_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)
    client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "customer:c1", "field": "real_name", "ciphertext": "secret-a", "nonce": "n"},
        headers=a["headers"],
    )
    got = client.get(
        f"/workspaces/{b['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=b["headers"],
    )
    assert got.status_code == 404


def test_delete_single_envelope(client):
    a = _make_ws(client)
    client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "customer:c1", "field": "real_name", "ciphertext": "secret", "nonce": "n"},
        headers=a["headers"],
    )
    r = client.delete(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    )
    assert r.status_code == 200 and r.json()["deleted"] is True
    gone = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    )
    assert gone.status_code == 404
    # deleting again -> 404
    again = client.delete(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    )
    assert again.status_code == 404


def test_delete_owner_erases_all_fields(client):
    a = _make_ws(client)
    for field in ("real_name", "real_phone", "funding"):
        client.put(
            f"/workspaces/{a['workspace_id']}/e2ee",
            json={"owner_ref": "customer:c1", "field": field, "ciphertext": "s", "nonce": "n"},
            headers=a["headers"],
        )
    r = client.delete(
        f"/workspaces/{a['workspace_id']}/e2ee/owner",
        params={"owner_ref": "customer:c1"}, headers=a["headers"],
    )
    assert r.status_code == 200 and r.json()["deleted"] == 3
    listing = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee/list",
        params={"owner_ref": "customer:c1"}, headers=a["headers"],
    ).json()
    assert listing["fields"] == []


def test_list_fields_leaks_no_ciphertext(client):
    a = _make_ws(client)
    client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "customer:c1", "field": "real_name", "ciphertext": "SECRET-PAYLOAD", "nonce": "n"},
        headers=a["headers"],
    )
    listing = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee/list",
        params={"owner_ref": "customer:c1"}, headers=a["headers"],
    ).json()
    assert listing["fields"] == [{"field": "real_name", "alg": "aes-gcm"}]
    assert "SECRET-PAYLOAD" not in str(listing)


def test_find_rejects_empty_blind_index(client):
    a = _make_ws(client)
    r = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee/find",
        params={"field": "real_name", "blind_index": "   "}, headers=a["headers"],
    )
    assert r.status_code == 400


def test_delete_isolated_across_workspaces(client):
    a = _make_ws(client)
    b = _make_ws(client)
    client.put(
        f"/workspaces/{a['workspace_id']}/e2ee",
        json={"owner_ref": "customer:c1", "field": "real_name", "ciphertext": "secret-a", "nonce": "n"},
        headers=a["headers"],
    )
    # b cannot delete a's envelope
    r = client.delete(
        f"/workspaces/{b['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=b["headers"],
    )
    assert r.status_code == 404
    still = client.get(
        f"/workspaces/{a['workspace_id']}/e2ee",
        params={"owner_ref": "customer:c1", "field": "real_name"}, headers=a["headers"],
    )
    assert still.status_code == 200
