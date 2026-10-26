"""P13 scheduler auto-patrol: live managers run on cycle, in-cycle dedup, test excluded."""
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentMemory, Role, User
from app.scheduler import _patrol_live_agents
from app.security import Actor


def _mk(client, ws, h, **ov):
    payload = {
        "name": "巡检", "role": "inspector",
        "allowed_tools": ["list_products"], "status": "live",
    }
    payload.update(ov)
    return client.post(f"/workspaces/{ws}/agents", json=payload, headers=h).json()


def _actor(workspace):
    db = SessionLocal()
    user = db.execute(select(User).where(User.id == workspace["user_id"])).scalar_one()
    return db, Actor(user=user, workspace_id=workspace["workspace_id"], role=Role.OWNER)


def test_live_agent_patrolled_once_then_dedup(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    a = _mk(client, ws, h)
    db, actor = _actor(workspace)
    try:
        first = asyncio.run(_patrol_live_agents(db, actor))
        assert first == [a["id"]]
        stamp = db.execute(
            select(AgentMemory).where(
                AgentMemory.workspace_id == ws,
                AgentMemory.key == f"agent:{a['id']}:last_patrol_at",
            )
        ).scalars().first()
        assert stamp is not None
        second = asyncio.run(_patrol_live_agents(db, actor))
        assert second == []
    finally:
        db.close()


def test_test_status_not_auto_patrolled(client, workspace):
    ws, h = workspace["workspace_id"], workspace["headers"]
    _mk(client, ws, h, status="test")
    db, actor = _actor(workspace)
    try:
        ran = asyncio.run(_patrol_live_agents(db, actor))
        assert ran == []
    finally:
        db.close()
