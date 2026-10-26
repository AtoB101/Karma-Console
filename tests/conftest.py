import os
import pathlib
import tempfile
import uuid

_tmp = tempfile.mkdtemp(prefix="krama_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{pathlib.Path(_tmp) / 'test.db'}"
os.environ["DEFAULT_MODEL_PROVIDER"] = "mock"
os.environ["ENABLE_BACKGROUND_SCHEDULER"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def workspace(client):
    email = f"u{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/users", json={"email": email, "display_name": "Owner"})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    r = client.post(f"/users/{user_id}/api-keys")
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]

    headers = {"Authorization": f"Bearer {key}"}
    r = client.post("/workspaces", json={"name": "衣小店", "region": "CN", "business_category": "clothing"}, headers=headers)
    assert r.status_code == 201, r.text
    ws_id = r.json()["id"]
    headers["X-Workspace-Id"] = ws_id
    return {"client": client, "headers": headers, "workspace_id": ws_id, "user_id": user_id}
