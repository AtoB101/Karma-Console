"""Tiered retention: purge volatile operational records past their age.

Only short-lived ledger/thread records are eligible. Business master data
(orders, customers, shipments), long-lived preference memory, credentials and
the audit trail are protected and are never auto-deleted. The audit log in
particular must survive for compliance review.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text as sql_text

from .models import utcnow

# table -> (retention_days, extra WHERE clause). Workspace scoping and the
# created_at cutoff are applied by cleanup_workspace; ``extra`` narrows further.
CLEANUP_RULES: dict[str, tuple[int, str]] = {
    "messages": (30, ""),
    "conversations": (30, "AND state != 'open'"),
    "stock_movements": (90, ""),
    "todos": (30, "AND status IN ('done','cancelled')"),
}

# Tables that must NEVER be auto-purged, regardless of age.
PROTECTED: set[str] = {
    "audit_logs",
    "agent_memories",
    "orders",
    "customers",
    "shipments",
    "executions",
    "api_keys",
    "connector_instances",
    "approval_requests",
    "workspaces",
    "products",
}


def cleanup_workspace(db, workspace_id: str, *, now=None, overrides: dict | None = None) -> dict:
    """Delete expired volatile records for one workspace. Returns per-table counts."""
    now = now or utcnow()
    report: dict[str, object] = {}
    for table, (default_days, extra) in CLEANUP_RULES.items():
        if table in PROTECTED:
            report[table] = "protected"
            continue
        days = (overrides or {}).get(table, default_days)
        cutoff = now - timedelta(days=days)
        try:
            res = db.execute(
                sql_text(
                    f"DELETE FROM {table} "
                    f"WHERE workspace_id = :ws AND created_at < :cut {extra}"
                ),
                {"ws": workspace_id, "cut": cutoff},
            )
            report[table] = res.rowcount
        except Exception as exc:  # noqa: BLE001 - missing table/column must not abort cleanup
            report[table] = f"skipped: {exc.__class__.__name__}"
    return report


def cleanup_all(db, *, now=None, overrides: dict | None = None) -> dict:
    """Run cleanup for every workspace present in the table scoped by workspace_id."""
    from sqlalchemy import select

    from .models import Workspace

    report: dict[str, object] = {}
    for ws in db.execute(select(Workspace)).scalars().all():
        report[ws.id] = cleanup_workspace(db, ws.id, now=now, overrides=overrides)
    return report
