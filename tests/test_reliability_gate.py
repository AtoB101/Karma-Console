"""P69 reliability gate: failure containment, read-path performance budget,
and a deterministic static security scan.

These tests are deliberately side-effect free and not flaky: they exercise the
gateway's exception containment, assert loose read-path latency ceilings on
local SQLite, and statically prove the source tree carries no dangerous shell /
eval / hardcoded-secret primitive. Real multi-process / distributed concurrency
is registered as a deployment-stage item (local single-connection SQLite).
"""
import asyncio
import dataclasses
import pathlib
import re
import time

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


# --- 1. failure containment: a handler exception must not bubble to HTTP -------

def test_handler_exception_returns_readable_error_not_raise(workspace, monkeypatch):
    from app.db import SessionLocal
    from app.gateway import execute
    from app.models import Role, User
    from app.security import Actor
    from app.tools import registry

    async def boom(_ctx):
        raise RuntimeError("simulated internal fault")

    spec = registry.get("list_products")
    fake = dataclasses.replace(spec, handler=boom)
    monkeypatch.setitem(registry._tools, "list_products", fake)
    db = SessionLocal()
    try:
        user = db.get(User, workspace["user_id"])
        actor = Actor(user=user, workspace_id=workspace["workspace_id"], role=Role.OWNER)
        result = asyncio.run(execute(db, actor, "list_products", {}))
        # Gateway catches, marks FAILED, returns a readable error; it does not raise.
        assert result.decision.value == "ALLOWED"
        assert "error" in result.result
        assert "simulated internal fault" in result.result["error"]
    finally:
        db.rollback()
        db.close()


# --- 2. read-path performance budget (loose ceilings on local SQLite) --------

def test_read_only_r0_call_under_budget(client, workspace):
    h = workspace["headers"]
    t0 = time.perf_counter()
    r = client.post("/gateway/execute", json={"tool": "list_products", "params": {}}, headers=h)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert r.json()["decision"] == "ALLOWED"
    assert elapsed < 2.0, f"list_products took {elapsed:.3f}s"


def test_tools_catalog_endpoint_under_budget(client, workspace):
    h, ws = workspace["headers"], workspace["workspace_id"]
    t0 = time.perf_counter()
    r = client.get(f"/workspaces/{ws}/tools", headers=h)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert isinstance(r.json()["tools"], list)
    assert elapsed < 2.0, f"tools catalog took {elapsed:.3f}s"


def test_list_orders_read_under_budget(client, workspace):
    h = workspace["headers"]
    t0 = time.perf_counter()
    r = client.post("/gateway/execute", json={"tool": "list_orders", "params": {}}, headers=h)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, f"list_orders took {elapsed:.3f}s"


# --- 3. deterministic static security scan -----------------------------------

_SAFE_SUBPROCESS_FILES = {"general.py", "devops_agent.py"}
# Dangerous primitives we must never reach for directly.
_FORBIDDEN = [
    (re.compile(r"shell\s*=\s*True"), "subprocess shell=True (shell injection risk)"),
    (re.compile(r"(?<![\w.])eval\s*\("), "raw eval()"),
    (re.compile(r"(?<![\w.])exec\s*\("), "raw exec()"),
]
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),     # embedded private key
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*[\"'][0-9a-f]{32,}[\"']"),
]


def _py_files():
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_shell_eval_or_hardcoded_secrets_in_source():
    findings = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        rel = path.name
        for pattern, label in _FORBIDDEN:
            if pattern.search(text):
                findings.append(f"{rel}: forbidden {label}")
        for pat in _SECRET_PATTERNS:
            if pat.search(text):
                findings.append(f"{rel}: possible hardcoded secret matched {pat.pattern}")
        if "subprocess" in text and rel not in _SAFE_SUBPROCESS_FILES:
            findings.append(f"{rel}: subprocess used outside the sandbox file {_SAFE_SUBPROCESS_FILES}")
    assert not findings, "\n".join(findings)


def test_subprocess_in_sandbox_is_argument_list_without_shell():
    general = (APP_DIR / "general.py").read_text(encoding="utf-8")
    assert "shell=True" not in general
    # The sandbox spawns an isolated interpreter by argument list, never a shell string.
    assert re.search(r"subprocess\.run\(\s*\[\s*sys\.executable", general, re.S)
