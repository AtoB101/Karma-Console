"""Task board ledger: classify live work into a deterministic state-machine view.

Pure functions only — callers query the DB and pass plain rows in; this module
never touches the session. Columns mirror the requested lifecycle:
queued -> pending_approval -> executing -> completed, with failed and
escalated as side lanes. Cross-workspace isolation is enforced by the caller.
"""
from __future__ import annotations

from . import nervous

# Board columns in lifecycle order.
COLUMNS = ["queued", "pending_approval", "executing", "completed", "failed", "escalated"]


def _meta_for(tool: str) -> dict:
    try:
        return nervous.dispatch_meta(tool)
    except Exception:  # noqa: BLE001 - metadata is best-effort decoration
        return {}


def build_board(executions=None, approvals=None, todos=None) -> dict:
    """Classify executions / pending approvals / pending todos into board lanes."""
    executions = executions or []
    approvals = approvals or []
    todos = todos or []

    lanes: dict[str, list[dict]] = {c: [] for c in COLUMNS}

    for ex in executions:
        meta = _meta_for(ex.tool_name)
        item = {
            "id": ex.id,
            "kind": "execution",
            "title": ex.tool_name,
            "status": ex.status,
            "at": ex.created_at.isoformat() if ex.created_at else "",
            "owner": meta.get("dispatch_owner", ""),
            "priority": meta.get("dispatch_priority", ""),
            "escalate": bool(meta.get("dispatch_escalate")),
        }
        if ex.status == "RUNNING":
            lanes["executing"].append(item)
        elif ex.status == "SUCCEEDED":
            lanes["completed"].append(item)
        elif ex.status in ("FAILED", "UNKNOWN"):
            lanes["failed"].append(item)
        else:  # PENDING
            lanes["queued"].append(item)

    for ap in approvals:
        meta = _meta_for(ap.action)
        force = ap.action in nervous.FORCE_ESCALATE or bool(meta.get("dispatch_escalate"))
        item = {
            "id": ap.id,
            "kind": "approval",
            "title": ap.action,
            "status": ap.status,
            "at": ap.created_at.isoformat() if ap.created_at else "",
            "owner": meta.get("dispatch_owner", ""),
            "priority": meta.get("dispatch_priority", "P0" if force else ""),
            "escalate": force,
            "risk_level": ap.risk_level,
        }
        lanes["escalated" if force else "pending_approval"].append(item)

    for td in todos:
        meta = _meta_for(td.ref_type or "")
        item = {
            "id": td.id,
            "kind": f"todo:{td.kind}",
            "title": td.title,
            "detail": td.detail,
            "status": td.status,
            "at": td.created_at.isoformat() if td.created_at else "",
            "owner": meta.get("dispatch_owner", ""),
            "priority": meta.get("dispatch_priority", ""),
            "escalate": False,
        }
        if td.kind == "must_confirm" and td.status == "pending":
            lanes["pending_approval"].append(item)
        elif td.status == "pending":
            lanes["queued"].append(item)

    counts = {c: len(lanes[c]) for c in COLUMNS}
    return {"columns": COLUMNS, "counts": counts, "lanes": lanes}
