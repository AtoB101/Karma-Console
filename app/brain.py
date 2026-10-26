"""Main brain: global observation feed + advisory safety hints.

The "super console" model has one main brain (this module) and many panels
(each a nerve ending). Every action already flows through the unified gateway
and lands in the append-only AuditLog, so the brain derives its global feed
from that log instead of keeping a second source of truth.

The brain never replaces the gateway's hard blocks. It adds *advisory hints*
for actions that are safe but likely wrong (e.g. sending sensitive data to an
external receiver, irreversible deletes, funds without reconciliation). Those
hints are warnings, not denials.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog

# Sensitive customer fields that should never leave the workspace casually.
_SENSITIVE_FIELDS = ("phone", "mobile", "email", "address", "id_card", "identity", "wx", "wechat", "bank")
_EXTERNAL_ACTIONS = (
    "send_chat_message",
    "reply_customer",
    "create_order",
    "http_write",
    "post",
    "sync",
    "push_order",
    "make_payment",
    "make_refund",
)
_IRREVERSIBLE_PREFIXES = ("delete", "remove", "uninstall", "purge", "destroy", "drop")
_FUND_ACTIONS = ("make_payment", "make_refund", "dispute_payment", "reconcile_payment")


def preflight_hints(action: str, params: dict | None, workspace_id: str = "") -> list[dict]:
    """Return advisory hints for an intended action. Pure function (no DB)."""
    action = str(action or "").lower()
    params = params or {}
    hints: list[dict] = []

    flat = " ".join([*(str(k) for k in params.keys()), *(str(v) for v in params.values())]).lower()

    if action in _FUND_ACTIONS or any(k in action for k in ("pay", "refund", "dispute")):
        hints.append({
            "code": "funds_terminal",
            "severity": "warn",
            "message": "资金类操作：执行后必须以 provider 回执回查终态，不得仅凭本地状态标记完成。",
        })

    if any(action.startswith(p) for p in _IRREVERSIBLE_PREFIXES):
        hints.append({
            "code": "irreversible_delete",
            "severity": "warn",
            "message": "该操作可能不可恢复（删除/卸载/清空）。请确认没有误删关键业务数据。",
        })

    if any(k in _EXTERNAL_ACTIONS for k in (action,)) or any(k in action for k in _EXTERNAL_ACTIONS if k in action):
        touched_sensitive = [f for f in _SENSITIVE_FIELDS if f in flat]
        if touched_sensitive:
            hints.append({
                "code": "sensitive_to_external",
                "severity": "danger",
                "message": "正在把客户敏感资料（" + "、".join(touched_sensitive) + "）发往外部接收者，请确认接收方可信、且符合数据分级要求。",
            })

    if "api_key" in flat or "token" in flat or "password" in flat or "secret" in flat:
        hints.append({
            "code": "credential_exposure",
            "severity": "danger",
            "message": "参数中包含密钥/口令。密钥不应外发，请确认没有把凭证写入外部渠道或日志。",
        })

    if any(k in action for k in ("batch", "bulk", "mass")) or (isinstance(params.get("quantity"), (int, float)) and params.get("quantity") == 0):
        hints.append({
            "code": "mass_scope",
            "severity": "warn",
            "message": "批量操作影响范围较大，请先限定数量上限或先在小范围验证。",
        })

    return hints


def brain_events(db: Session, workspace_id: str, limit: int = 50) -> list[dict]:
    """Global, append-only observation feed for a workspace."""
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    out: list[dict] = []
    for row in rows:
        created = row.created_at.isoformat() if getattr(row, "created_at", None) else ""
        out.append({
            "id": row.id,
            "action": row.action,
            "decision": row.decision,
            "risk_level": row.risk_level,
            "resource": row.resource or "",
            "reason": row.reason or "",
            "data_tier": row.data_tier or "",
            "actor_id": row.actor_id or "",
            "created_at": created,
        })
    return out

def _event_dict(row) -> dict:
    created = row.created_at.isoformat() if getattr(row, "created_at", None) else ""
    return {
        "id": row.id,
        "action": row.action,
        "decision": row.decision,
        "risk_level": row.risk_level,
        "resource": row.resource or "",
        "reason": row.reason or "",
        "data_tier": row.data_tier or "",
        "actor_id": row.actor_id or "",
        "created_at": created,
    }


def _sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def brain_event_stream(session_factory, workspace_id: str, poll_interval: float = 0.8, once: bool = False):
    """SSE stream derived from the append-only audit log (single source of truth).

    Emits a ``snapshot`` event on connect, then ``event`` frames for each new
    audit row. This is a polling-backed stream: it reads new rows at a short
    interval and never blocks the synchronous write path.

    With ``once=True`` the stream ends after the snapshot, which makes the
    endpoint testable and debuggable over a normal (non-streaming) GET.
    """
    import asyncio

    from sqlalchemy import select

    from .models import AuditLog

    seen: set[str] = set()
    interval = max(0.2, min(float(poll_interval or 0.8), 5.0))
    yield _sse("ready", {"workspace_id": workspace_id, "mode": "audit_log"})

    while True:
        try:
            db = session_factory()
            try:
                rows = db.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .order_by(AuditLog.created_at.desc())
                    .limit(100)
                ).scalars().all()
                if not seen:
                    seen = {r.id for r in rows}
                    yield _sse("snapshot", {"events": [_event_dict(r) for r in rows]})
                else:
                    fresh = [r for r in rows if r.id not in seen]
                    for row in reversed(fresh):
                        seen.add(row.id)
                        yield _sse("event", _event_dict(row))
                if len(seen) > 2000:
                    seen = {r.id for r in rows}
            finally:
                db.close()
            if once:
                break
        except GeneratorExit:
            raise
        except Exception as exc:  # keep the stream alive; surface the error to the client
            yield _sse("error", {"detail": str(exc)})
        await asyncio.sleep(interval)

