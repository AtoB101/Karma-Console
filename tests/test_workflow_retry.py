"""P1 workflow reliability: transient retry/backoff, permanent fail-fast,
unresolved-receipt exhaustion, and compensation idempotent de-duplication."""
import asyncio

from app.gateway import GatewayResult
from app.models import Decision
from app.retry import PERMANENT, RETRYABLE, backoff_seconds, classify_step_failure


# --- pure classifier / backoff unit tests -----------------------------------

def test_classify_unresolved_receipt_is_retryable():
    assert classify_step_failure(
        {"verification": {"status": "UNKNOWN"}}, decision="ALLOWED", idempotent=False
    ) == RETRYABLE
    assert classify_step_failure(
        {"verification": {"status": "PENDING"}}, decision="ALLOWED", idempotent=False
    ) == RETRYABLE
    assert classify_step_failure(
        {"needs_reconcile": True}, decision="ALLOWED", idempotent=False
    ) == RETRYABLE


def test_classify_transient_only_when_idempotent():
    err = {"error": "connection reset by peer"}
    assert classify_step_failure(err, decision="ALLOWED", idempotent=True) == RETRYABLE
    # Non-idempotent side effect must not be retried on a transient error.
    assert classify_step_failure(err, decision="ALLOWED", idempotent=False) == PERMANENT


def test_classify_permanent_errors_fail_fast():
    for msg in ["unknown tool: x", "permission denied", "invalid params", "404 not found"]:
        assert classify_step_failure(
            {"error": msg}, decision="ALLOWED", idempotent=True
        ) == PERMANENT, msg


def test_classify_non_allowed_decisions_and_success_are_none():
    assert classify_step_failure(None, decision="PENDING", idempotent=True) == "none"
    assert classify_step_failure(None, decision="DENIED", idempotent=True) == "none"
    assert classify_step_failure({"ok": True}, decision="ALLOWED", idempotent=True) == "none"


def test_backoff_grows_exponentially_and_caps():
    assert backoff_seconds(0, 1.0, 30.0) == 1.0
    assert backoff_seconds(1, 1.0, 30.0) == 2.0
    assert backoff_seconds(2, 1.0, 30.0) == 4.0
    assert backoff_seconds(10, 1.0, 30.0) == 30.0


# --- integration: retry through the task engine -----------------------------

def _run_task(client, workspace, steps):
    r = client.post(
        f"/workspaces/{workspace['workspace_id']}/tasks",
        json={"goal": "reliability probe", "steps": steps},
        headers=workspace["headers"],
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    return client.post(f"/tasks/{task_id}/run", headers=workspace["headers"])


def test_transient_error_retried_then_succeeds(client, workspace, monkeypatch):
    from app import workflow as wf

    calls: list[str] = []

    async def fake_execute(db, actor, tool, params, idempotency_key=None, task_id=""):
        calls.append(idempotency_key or "")
        if len(calls) == 1:
            return GatewayResult(
                Decision.ALLOWED, result={"error": "read timed out"}, execution_id="e1"
            )
        return GatewayResult(Decision.ALLOWED, result={"ok": True}, execution_id="e2")

    monkeypatch.setattr(wf, "execute", fake_execute)
    monkeypatch.setattr(wf, "_is_idempotent", lambda db, actor, tool: True)
    async def _noop_backoff(*_):
        return None
    monkeypatch.setattr(wf, "sleep_backoff", _noop_backoff)

    body = _run_task(
        client, workspace, [{"tool": "list_products", "params": {}}]
    ).json()
    assert body["status"] == "COMPLETED"
    # retried on the same idempotency key, never re-keyed
    assert len(calls) == 2
    assert calls[0] == calls[1]
    step = body["result"]["steps"][0]
    assert step["attempts"] == 2


def test_permanent_error_fails_fast_without_retry(client, workspace, monkeypatch):
    from app import workflow as wf

    calls: list[str] = []

    async def fake_execute(db, actor, tool, params, idempotency_key=None, task_id=""):
        calls.append(tool)
        return GatewayResult(
            Decision.ALLOWED, result={"error": "invalid params: missing required field"},
            execution_id="e1",
        )

    monkeypatch.setattr(wf, "execute", fake_execute)
    monkeypatch.setattr(wf, "_is_idempotent", lambda db, actor, tool: True)

    body = _run_task(
        client, workspace, [{"tool": "list_products", "params": {}}]
    ).json()
    assert body["status"] == "FAILED"
    assert calls == ["list_products"]  # no retry


def test_unresolved_receipt_exhausts_retries_to_unknown(client, workspace, monkeypatch):
    from app import workflow as wf

    calls: list[str] = []

    async def fake_execute(db, actor, tool, params, idempotency_key=None, task_id=""):
        calls.append(tool)
        return GatewayResult(
            Decision.ALLOWED,
            result={"verification": {"status": "UNKNOWN"}, "needs_reconcile": True},
            execution_id="e1",
        )

    monkeypatch.setattr(wf, "execute", fake_execute)
    monkeypatch.setattr(wf, "_is_idempotent", lambda db, actor, tool: True)
    async def _noop_backoff(*_):
        return None
    monkeypatch.setattr(wf, "sleep_backoff", _noop_backoff)

    body = _run_task(
        client, workspace, [{"tool": "list_products", "params": {}}]
    ).json()
    assert body["status"] == "UNKNOWN"
    # default max_retries=2 => initial + 2 retries = 3 attempts
    assert len(calls) == 3


# --- compensation idempotent de-duplication ----------------------------------

def test_compensation_skips_already_done(workspace, monkeypatch):
    from app import workflow as wf
    from app.db import SessionLocal
    from app.models import Role, Task, TaskStatus, User
    from app.security import Actor

    db = SessionLocal()
    try:
        user = db.get(User, workspace["user_id"])
        actor = Actor(user=user, workspace_id=workspace["workspace_id"], role=Role.OWNER)
        task = Task(
            workspace_id=workspace["workspace_id"],
            actor_id=workspace["user_id"],
            intent_id="intent-x",
            status=TaskStatus.FAILED.value,
            plan=[{"tool": "make_payment", "params": {"amount": 1}}],
            result={
                "compensation": {
                    "done": ["make_payment"],
                    "items": [{"tool": "make_payment", "status": "DONE"}],
                }
            },
        )
        db.add(task)
        db.flush()

        calls: list[str] = []

        async def fake_execute(db, actor, tool, params, idempotency_key=None, task_id=""):
            calls.append(tool)
            return GatewayResult(Decision.ALLOWED, result={"ok": True}, execution_id="x")

        monkeypatch.setattr(wf, "execute", fake_execute)
        completed = [
            {"tool": "make_payment", "execution_id": "e1", "result": {"provider_reference": "ref-1"}}
        ]
        out = asyncio.run(wf._run_compensations(db, actor, task, completed))
        assert calls == []  # already DONE: refund must not be re-issued
        assert out["status"] == "DONE"
    finally:
        db.rollback()
        db.close()
