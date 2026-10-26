"""T6: main-brain SSE realtime event stream.

The stream is derived from the append-only audit log (single source of truth).
It emits ``ready``, ``snapshot`` and ``event`` frames and never blocks the
synchronous write path.
"""
from __future__ import annotations

import asyncio


def _sse_frames(text: str) -> list[tuple[str, dict]]:
    import json

    frames = []
    for block in text.split("\n\n"):
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[len("data: "):])
                except Exception:
                    data = None
        if event:
            frames.append((event, data))
    return frames


def test_brain_stream_http_smoke(client, workspace):
    h = workspace["headers"]
    ws = workspace["workspace_id"]
    # once=1 makes the stream finite so a normal GET can verify the wire format.
    r = client.get(f"/workspaces/{ws}/brain/stream?interval=0.2&once=1", headers=h)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    frames = _sse_frames(r.text)
    events = [f[0] for f in frames]
    assert "ready" in events
    assert "snapshot" in events
    assert frames[1][1] is not None and "events" in frames[1][1]


def test_brain_stream_emits_new_audit_event(client, workspace):
    from app.brain import brain_event_stream
    from app.db import SessionLocal

    h = workspace["headers"]
    ws = workspace["workspace_id"]
    user_id = workspace["user_id"]

    # Seed one audited action so the snapshot is non-empty.
    r = client.post("/gateway/execute", json={"tool": "list_products", "params": {}}, headers=h)
    assert r.status_code == 200

    stream = brain_event_stream(SessionLocal, ws, poll_interval=0.2)

    async def drive():
        it = stream.__aiter__()
        frames = [await it.__anext__(), await it.__anext__()]  # ready + snapshot

        def insert_new_row():
            from app.audit import record
            from app.models import Decision

            db = SessionLocal()
            try:
                record(
                    db, workspace_id=ws, actor_id=user_id, action="test_sse_ping",
                    risk_level="R1", decision=Decision.ALLOWED, resource="sse",
                    reason="SSE 实时推送验证", data_tier="INTERNAL",
                )
                db.commit()
            finally:
                db.close()

        await asyncio.get_event_loop().run_in_executor(None, insert_new_row)
        frames.append(await it.__anext__())  # event frame
        await it.aclose()
        return frames

    raw_frames = asyncio.run(drive())
    frames = []
    for raw in raw_frames:
        frames.extend(_sse_frames(raw))
    names = [name for name, _ in frames]
    assert names[0] == "ready"
    assert names[1] == "snapshot"
    assert names[2] == "event"
    assert frames[2][1]["action"] == "test_sse_ping"
    assert frames[2][1]["decision"] == "ALLOWED"


def test_brain_stream_requires_actor(client, workspace):
    ws = workspace["workspace_id"]
    r = client.get(f"/workspaces/{ws}/brain/stream", headers={})
    assert r.status_code == 401
