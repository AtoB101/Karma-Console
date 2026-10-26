"""Scheduled (conditional) workflow tests (plan M05)."""
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ScheduledJob, Task, utcnow


def test_job_crud_and_due_execution(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]

    r = client.post(
        f"/workspaces/{ws}/jobs",
        json={"name": "每日盘点", "trigger": {"type": "interval", "minutes": 60}, "goal": "盘点", "steps": [{"tool": "workspace_summary", "params": {}}]},
        headers=h,
    )
    assert r.status_code == 201
    job = r.json()
    assert job["enabled"] is True

    lst = client.get(f"/workspaces/{ws}/jobs", headers=h).json()
    assert any(j["id"] == job["id"] for j in lst["jobs"])

    from app import scheduler

    db = SessionLocal()
    try:
        row = db.get(ScheduledJob, job["id"])
        row.next_run_at = utcnow()
        db.commit()
        results = asyncio.run(scheduler._run_scheduled_jobs(db))
    finally:
        db.close()
    assert results and results[0]["job_id"] == job["id"]
    assert results[0]["status"] == "COMPLETED"

    db = SessionLocal()
    try:
        row = db.get(ScheduledJob, job["id"])
        assert row.last_run_at is not None
        tasks = db.execute(select(Task).where(Task.workspace_id == ws)).scalars().all()
        assert any(t.plan and t.plan[0].get("tool") == "workspace_summary" for t in tasks)
    finally:
        db.close()


def test_job_update_disable_and_delete(client, workspace):
    ws = workspace["workspace_id"]
    h = workspace["headers"]
    job = client.post(f"/workspaces/{ws}/jobs", json={"name": "临时任务", "trigger": {"type": "interval", "minutes": 5}}, headers=h).json()

    patch = client.patch(f"/workspaces/{ws}/jobs/{job['id']}", json={"enabled": False, "name": "已停用"}, headers=h)
    assert patch.status_code == 200
    assert patch.json()["enabled"] is False
    assert patch.json()["name"] == "已停用"

    assert client.delete(f"/workspaces/{ws}/jobs/{job['id']}", headers=h).status_code == 204
    assert client.get(f"/workspaces/{ws}/jobs", headers=h).json()["jobs"] == []
