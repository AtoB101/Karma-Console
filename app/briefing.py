"""Local-only business briefing aggregation (daily / weekly).

Pure functions over already-loaded ORM rows. No network, no external payout
reconciliation; money figures are local order ``total_amount`` sums and are
never presented as a platform-settled amount or a real charge.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Order statuses that count as a realized/local-deal in the window.
_PAID_STATES = {"PAID", "SHIPPED", "DELIVERED"}
_PIPELINE_KEYS = ("new", "lead", "won", "lost")


def _now() -> datetime:
    # Match the codebase's naive UTC utcnow() convention.
    return datetime.utcnow()


def _in_window(created_at: datetime | None, start: datetime) -> bool:
    return created_at is not None and created_at >= start


def summarize(
    orders,
    customers,
    products,
    pending_todos: int = 0,
    pending_approvals: int = 0,
    goals=None,
    now: datetime | None = None,
    period: str = "daily",
    tickets=None,
) -> dict:
    """Return a read-only briefing dict for ``daily`` (today) or ``weekly`` (last 7d)."""
    now = now or _now()
    if period == "weekly":
        start = now - timedelta(days=7)
        label = "近 7 天"
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "今日"

    goals = goals or []
    win_orders = [o for o in orders if _in_window(getattr(o, "created_at", None), start)]
    revenue = round(
        sum(float(getattr(o, "total_amount", 0) or 0) for o in win_orders
            if getattr(o, "status", None) in _PAID_STATES),
        2,
    )
    new_customers = [c for c in customers if _in_window(getattr(c, "created_at", None), start)]

    pipeline = {k: 0 for k in _PIPELINE_KEYS}
    for c in customers:
        key = getattr(c, "stage", "new")
        pipeline[key if key in pipeline else "new"] += 1

    low_stock = [p for p in products if (getattr(p, "stock", 0) or 0) <= 5]
    pending_ship = sum(1 for o in orders if getattr(o, "status", None) == "PAID")

    # 客服工单块：窗口新建、未闭环、高优先待处理、满意度均值（全量样本）。
    tickets = tickets or []
    win_tk = [t for t in tickets if _in_window(getattr(t, "created_at", None), start)]
    open_tk = [t for t in tickets if getattr(t, "status", None) != "closed"]
    urgent_open = [
        t for t in open_tk
        if getattr(t, "priority", None) in ("P0", "P1")
        and getattr(t, "status", None) in ("new", "processing", "escalated")
    ]
    sats = [getattr(c, "satisfaction", None) for c in customers
            if getattr(c, "satisfaction", None) is not None]
    sat_avg = round(sum(sats) / len(sats), 2) if sats else None

    active_goals = [
        {
            "id": g.id,
            "title": g.title,
            "progress_note": (g.progress_note or ""),
            "due_at": g.due_at.isoformat() if g.due_at else None,
        }
        for g in goals if getattr(g, "status", None) == "active"
    ]

    highlights: list[str] = []
    if pending_approvals:
        highlights.append(f"{pending_approvals} 项待审批，需主人拍板（Agent 不自行执行）")
    if pending_ship:
        highlights.append(f"{pending_ship} 笔已付款订单待发货")
    if urgent_open:
        highlights.append(f"{len(urgent_open)} 条 P0/P1 客服工单待处理，需尽快受理或升级")
    if low_stock:
        highlights.append(f"{len(low_stock)} 个商品低库存（≤5），建议补货")
    if revenue > 0:
        highlights.append(f"{label}本地成交金额 ¥{revenue:.2f}（本地订单合计，非平台对账）")
    if not highlights:
        highlights.append("当前无紧急事项，系统平稳待命")

    return {
        "period": period,
        "label": label,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "orders_in_window": len(win_orders),
        "revenue_local": revenue,
        "new_customers": len(new_customers),
        "pending_ship": pending_ship,
        "pipeline": pipeline,
        "low_stock_count": len(low_stock),
        "low_stock_items": [
            {"id": p.id, "name": getattr(p, "name", ""), "stock": getattr(p, "stock", 0)}
            for p in low_stock[:10]
        ],
        "pending_todos": pending_todos,
        "pending_approvals": pending_approvals,
        "active_goals": active_goals,
        "customer_service": {
            "tickets_in_window": len(win_tk),
            "open_tickets": len(open_tk),
            "urgent_open": len(urgent_open),
            "satisfaction_avg": sat_avg,
            "satisfaction_count": len(sats),
        },
        "highlights": highlights,
        "caveat": "金额为本地订单合计，非真实扣款、非平台对账，真实结算以 Karma 回执为准。",
    }
