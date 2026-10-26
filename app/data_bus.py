"""M03 panel data bus: declarative, workspace-isolated read aggregation.

Panels and managers declare which named sources they need; the bus collects
them server-side under the caller's workspace, applies optional filters, field
projections and scalar aggregates, and returns one merged read-only payload.

This bus performs **no writes and no gateway side effects**. Unknown sources or
malformed spec entries are collected as readable errors rather than raising a
500, so a bad panel config never takes the whole bus down.

Spec shape (all sections optional)::

    {
      "select": {
        "low_stock":   {"source": "products", "fn": "count",
                        "where": {"field": "stock", "op": "lte", "value": 5}},
        "sales_total": {"source": "orders", "fn": "sum", "field": "total_amount",
                        "where": {"field": "payment_status", "op": "eq", "value": "paid"}},
        "open_orders": {"source": "orders", "fn": "count"}
      },
      "items": {
        "recent_orders": {
          "source": "orders", "limit": 10,
          "where": {"field": "status", "op": "eq", "value": "paid"},
          "project": {"id": "id", "ref": "order_reference", "total": "total_amount"}
        }
      }
    }
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

# --- named read collectors (workspace-isolated) -----------------------------

def _collect_products(db: Session, workspace_id: str) -> list[dict]:
    from .models import Product

    rows = db.execute(select(Product).where(Product.workspace_id == workspace_id)).scalars().all()
    return [
        {"id": p.id, "name": p.name, "category": p.category, "price": p.price,
         "status": p.status, "stock": p.stock, "layout_area_id": p.layout_area_id}
        for p in rows
    ]


def _collect_orders(db: Session, workspace_id: str) -> list[dict]:
    from .models import Order

    rows = db.execute(
        select(Order).where(Order.workspace_id == workspace_id).order_by(Order.created_at.desc())
    ).scalars().all()
    return [
        {"id": o.id, "channel": o.channel, "order_reference": o.order_reference,
         "status": o.status, "payment_status": o.payment_status,
         "total_amount": o.total_amount, "currency": o.currency, "customer": o.customer}
        for o in rows
    ]


def _collect_customers(db: Session, workspace_id: str) -> list[dict]:
    from .models import Customer

    rows = db.execute(
        select(Customer).where(Customer.workspace_id == workspace_id).order_by(Customer.updated_at.desc())
    ).scalars().all()
    return [
        {"id": c.id, "channel": c.channel, "external_id": c.external_id,
         "name": c.name, "stage": c.stage}
        for c in rows
    ]


def _collect_inventory(db: Session, workspace_id: str) -> list[dict]:
    from .models import Product

    rows = db.execute(select(Product).where(Product.workspace_id == workspace_id)).scalars().all()
    return [
        {"product_id": p.id, "name": p.name, "stock": p.stock, "price": p.price, "status": p.status}
        for p in rows
    ]


def _collect_todos(db: Session, workspace_id: str) -> list[dict]:
    from .models import Todo

    rows = db.execute(
        select(Todo).where(Todo.workspace_id == workspace_id).order_by(Todo.created_at.desc())
    ).scalars().all()
    return [
        {"id": t.id, "kind": t.kind, "title": t.title, "status": t.status,
         "ref_type": t.ref_type, "ref_id": t.ref_id}
        for t in rows
    ]


def _collect_approvals(db: Session, workspace_id: str) -> list[dict]:
    from .models import ApprovalRequest

    rows = db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.workspace_id == workspace_id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(100)
    ).scalars().all()
    return [
        {"id": r.id, "action": r.action, "risk_level": r.risk_level,
         "status": r.status, "reason": r.reason}
        for r in rows
    ]


def _collect_agents(db: Session, workspace_id: str) -> list[dict]:
    from .models import AIAgent

    rows = db.execute(
        select(AIAgent).where(AIAgent.workspace_id == workspace_id).order_by(AIAgent.updated_at.desc())
    ).scalars().all()
    return [
        {"id": a.id, "name": a.name, "role": a.role, "status": a.status}
        for a in rows
    ]


COLLECTORS: dict[str, callable] = {
    "products": _collect_products,
    "orders": _collect_orders,
    "customers": _collect_customers,
    "inventory": _collect_inventory,
    "todos": _collect_todos,
    "approvals": _collect_approvals,
    "agents": _collect_agents,
}


# --- predicate / projection helpers (no eval, side-effect-free) ------------

def _dig(row: dict, dotted: str):
    cur = row
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _match(row: dict, cond: dict) -> bool:
    """Structured row predicate; malformed -> False (safe exclude)."""
    if not isinstance(cond, dict):
        return True
    actual = _dig(row, cond.get("field", "")) if cond.get("field") else row
    op = str(cond.get("op") or "present").lower()
    expected = cond.get("value")
    if op == "present":
        return actual is not None
    if op == "absent":
        return actual is None
    if op == "truthy":
        return bool(actual)
    if op == "contains":
        try:
            return expected in actual
        except TypeError:
            return False
    if op == "in":
        try:
            return actual in expected
        except TypeError:
            return False
    a, b = _to_number(actual), _to_number(expected)
    if op in ("eq", "ne"):
        equal = (a is not None and b is not None and abs(a - b) < 1e-9) or actual == expected
        return equal if op == "eq" else not equal
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    return False


def _filtered(rows: list[dict], where: dict | None) -> list[dict]:
    if not where:
        return rows
    return [r for r in rows if _match(r, where)]


def _aggregate(rows: list[dict], fn: str, field: str | None):
    fn = (fn or "count").lower()
    if fn == "count":
        return len(rows)
    values = [_to_number(_dig(r, field)) for r in rows] if field else []
    values = [v for v in values if v is not None]
    if fn == "sum":
        return round(sum(values), 4)
    if fn == "avg":
        return round(sum(values) / len(values), 4) if values else 0
    if fn == "min":
        return min(values) if values else None
    if fn == "max":
        return max(values) if values else None
    if fn == "first":
        return _dig(rows[0], field) if rows and field else (rows[0] if rows else None)
    return None


def _project(row: dict, project: dict | None) -> dict:
    if not project:
        return dict(row)
    return {out_key: _dig(row, src_field) for out_key, src_field in project.items()}


# --- public resolve ---------------------------------------------------------

def resolve(db: Session, workspace_id: str, spec: dict) -> dict:
    """Resolve a declarative read spec for one workspace.

    Returns ``{"data": {...}, "errors": [...]}``. Never raises on bad input;
    per-entry failures are reported in ``errors`` and omitted from ``data``.
    """
    data: dict = {}
    errors: list[str] = []
    if not isinstance(spec, dict):
        return {"data": {}, "errors": ["spec must be an object"]}

    cache: dict[str, list[dict]] = {}

    def source(name: str) -> list[dict] | None:
        if name not in COLLECTORS:
            errors.append(f"unknown source: {name}")
            return None
        if name not in cache:
            cache[name] = COLLECTORS[name](db, workspace_id)
        return cache[name]

    for alias, item in (spec.get("select") or {}).items():
        if not isinstance(item, dict):
            errors.append(f"select.{alias} must be an object")
            continue
        rows = source(item.get("source", ""))
        if rows is None:
            continue
        rows = _filtered(rows, item.get("where"))
        value = _aggregate(rows, item.get("fn"), item.get("field"))
        if value is None and item.get("fn") not in (None, "count"):
            errors.append(f"select.{alias}: cannot compute {item.get('fn')} on field {item.get('field')}")
            continue
        data[alias] = value

    for alias, item in (spec.get("items") or {}).items():
        if not isinstance(item, dict):
            errors.append(f"items.{alias} must be an object")
            continue
        rows = source(item.get("source", ""))
        if rows is None:
            continue
        rows = _filtered(rows, item.get("where"))
        projected = [_project(r, item.get("project")) for r in rows]
        limit = item.get("limit")
        if isinstance(limit, int) and limit >= 0:
            projected = projected[:limit]
        data[alias] = projected

    return {"data": data, "errors": errors}
