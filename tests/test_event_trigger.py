"""P71 event / once / interval trigger layer (M05 trigger closure).

Event jobs fire through dispatch_event on a matching event name, may gate on a
structured condition against the payload, and respect a cooldown; ``once`` jobs
fire once then disable; the time sweep never runs ``event`` jobs.
"""
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ScheduledJob, Task, utcnow


def test_event_fires_matching_job(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    job = client.post(
        f"/workspaces/{ws}/jobs",
        json={
            "name": "支付成功后盘点",
            "trigger": {"type": "event", "event": "order.paid"},
            "goal": "盘点",
            "steps": [{"tool": "workspace_summary", "params": {}}],
        },
        headers=h,
    ).json()

    r = client.post(f"/workspaces/{ws}/events/order.paid", json={"amount": 200}, headers=h)
    assert r.status_code == 200
    fired = r.json()["fired"]
    assert len(fired) == 1
    assert fired[0]["job_id"] == job["id"]
    assert fired[0]["status"] == "COMPLETED"

    db = SessionLocal()
    try:
        tasks = db.execute(select(Task).where(Task.workspace_id == ws)).scalars().all()
        assert any(t.plan and t.plan[0].get("tool") == "workspace_summary" for t in tasks)
    finally:
        db.close()


def test_event_condition_blocks_and_passes(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    client.post(
        f"/workspaces/{ws}/jobs",
        json={
            "name": "大额复核",
            "trigger": {"type": "event", "event": "order.paid",
                        "condition": {"field": "amount", "op": "gt", "value": 100}},
            "goal": "复核",
            "steps": [{"tool": "workspace_summary", "params": {}}],
        },
        headers=h,
    )

    low = client.post(f"/workspaces/{ws}/events/order.paid", json={"amount": 50}, headers=h).json()
    assert low["fired"][0]["status"] == "SKIPPED"

    high = client.post(f"/workspaces/{ws}/events/order.paid", json={"amount": 200}, headers=h).json()
    assert high["fired"][0]["status"] == "COMPLETED"


def test_event_unknown_name_matches_nothing(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    client.post(
        f"/workspaces/{ws}/jobs",
        json={"name": "x", "trigger": {"type": "event", "event": "order.paid"},
              "steps": [{"tool": "workspace_summary", "params": {}}]},
        headers=h,
    )
    r = client.post(f"/workspaces/{ws}/events/refund.created", json={}, headers=h)
    assert r.json()["fired"] == []


def test_event_cooldown_suppresses_second_fire(client, workspace):
    from app import scheduler

    ws, h = workspace["workspace_id"], workspace["headers"]
    job = client.post(
        f"/workspaces/{ws}/jobs",
        json={
            "name": "冷却测试",
            "trigger": {"type": "event", "event": "evt", "cooldown_seconds": 3600},
            "steps": [{"tool": "workspace_summary", "params": {}}],
        },
        headers=h,
    ).json()

    db = SessionLocal()
    try:
        from app.security import Actor
        from app.models import Membership, Role, User
        owner = db.execute(
            select(Membership).where(Membership.workspace_id == ws, Membership.role == Role.OWNER.value)
        ).scalars().first()
        user = db.get(User, owner.user_id)
        actor = Actor(user=user, workspace_id=ws, role=Role.OWNER)

        first = asyncio.run(scheduler.dispatch_event(db, actor, "evt", {}))
        second = asyncio.run(scheduler.dispatch_event(db, actor, "evt", {}))
    finally:
        db.close()
    assert first[0]["status"] == "COMPLETED"
    assert second[0]["status"] == "COOLDOWN"
    assert second[0]["job_id"] == job["id"]


def test_once_job_fires_then_disabled(client, workspace):
    from app import scheduler

    ws, h = workspace["workspace_id"], workspace["headers"]
    job = client.post(
        f"/workspaces/{ws}/jobs",
        json={
            "name": "一次性提醒",
            "trigger": {"type": "once", "at": "2000-01-01T00:00:00"},
            "steps": [{"tool": "workspace_summary", "params": {}}],
        },
        headers=h,
    ).json()

    db = SessionLocal()
    try:
        row = db.get(ScheduledJob, job["id"])
        row.next_run_at = utcnow()
        db.commit()
        results = asyncio.run(scheduler._run_scheduled_jobs(db))
        row = db.get(ScheduledJob, job["id"])
        disabled = row.enabled is False
    finally:
        db.close()
    assert any(r.get("job_id") == job["id"] and r.get("trigger") == "once" for r in results)
    assert disabled is True


def test_time_sweep_skips_event_jobs(client, workspace):
    from app import scheduler

    ws, h = workspace["workspace_id"], workspace["headers"]
    client.post(
        f"/workspaces/{ws}/jobs",
        json={
            "name": "事件型",
            "trigger": {"type": "event", "event": "order.paid"},
            "steps": [{"tool": "workspace_summary", "params": {}}],
        },
        headers=h,
    )
    db = SessionLocal()
    try:
        results = asyncio.run(scheduler._run_scheduled_jobs(db))
    finally:
        db.close()
    assert results == []
