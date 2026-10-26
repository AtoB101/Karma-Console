"""P18 fault-injection / recovery drill: a stalled EXECUTING task after a
restart is resumed once by the scheduler; a fresh runnable task is not touched."""
import asyncio
from datetime import timedelta

from app.db import SessionLocal
from app.models import Role, Task, TaskStatus, User, utcnow
from app.scheduler import _recover_stale_workspace
from app.security import Actor


def _make_actor(workspace, db):
    user = db.get(User, workspace["user_id"])
    return Actor(user=user, workspace_id=workspace["workspace_id"], role=Role.OWNER)


def test_stale_executing_task_is_resumed_once(workspace, monkeypatch):
    db = SessionLocal()
    actor = _make_actor(workspace, db)
    try:
        stale = Task(
            workspace_id=workspace["workspace_id"],
            actor_id=workspace["user_id"],
            intent_id="intent-x",
            status=TaskStatus.EXECUTING.value,
            plan=[{"tool": "list_products", "params": {}}],
            result={},
        )
        stale.updated_at = utcnow() - timedelta(seconds=99999)
        db.add(stale)
        db.commit()
        db.refresh(stale)

        calls: list[str] = []

        async def fake_execute_task(_db, _actor, task_id):
            calls.append(task_id)

        monkeypatch.setattr("app.workflow.execute_task", fake_execute_task)
        out = asyncio.run(_recover_stale_workspace(db, actor))
        assert calls == [stale.id]
        assert any(r.get("task_id") == stale.id for r in out)
    finally:
        db.rollback()
        db.close()


def test_fresh_runnable_task_is_not_resumed(workspace, monkeypatch):
    db = SessionLocal()
    actor = _make_actor(workspace, db)
    try:
        fresh = Task(
            workspace_id=workspace["workspace_id"],
            actor_id=workspace["user_id"],
            intent_id="intent-y",
            status=TaskStatus.EXECUTING.value,
            plan=[{"tool": "list_products", "params": {}}],
            result={},
        )
        fresh.updated_at = utcnow()
        db.add(fresh)
        db.commit()
        db.refresh(fresh)

        calls: list[str] = []

        async def fake_execute_task(_db, _actor, task_id):
            calls.append(task_id)

        monkeypatch.setattr("app.workflow.execute_task", fake_execute_task)
        asyncio.run(_recover_stale_workspace(db, actor))
        assert calls == []
    finally:
        db.rollback()
        db.close()
