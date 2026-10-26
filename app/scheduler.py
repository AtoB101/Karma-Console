"""Background jobs: Karma reconciliation and stale-task recovery.

Runs as an in-process asyncio loop (no external broker). Every action inside a
job still flows through the unified gateway, so permission, policy, approval,
idempotency and audit apply exactly as they do for interactive execution.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import utcnow

# In-memory consecutive-failure counters per workspace for reconciliation
# backoff. A workspace that keeps failing is skipped for a few cycles instead
# of hammering an unhealthy external service; a clean pass resets the counter.
# Counters live in process memory and reset on restart (acceptable: restart
# also re-runs reconciliation from a clean slate).
_RECONCILE_FAILURES: dict[str, int] = {}


async def _reconcile_with_backoff(db: Session, workspace_id: str, actor) -> list[dict]:
    failures = _RECONCILE_FAILURES.get(workspace_id, 0)
    threshold = settings.reconcile_max_consecutive_failures
    if failures >= threshold:
        return [{
            "skipped": True,
            "consecutive_failures": failures,
            "reason": "backoff after repeated reconciliation failures",
        }]
    results = await _reconcile_workspace(db, workspace_id, actor)
    errors = [r for r in results if r.get("decision") == "ERROR"]
    _RECONCILE_FAILURES[workspace_id] = failures + len(errors) if errors else 0
    return results


async def _reconcile_workspace(db: Session, workspace_id: str, actor) -> list[dict]:
    from .gateway import execute
    from .models import Order

    orders = db.execute(
        select(Order).where(
            Order.workspace_id == workspace_id,
            Order.payment_status == "paid",
            Order.provider_reference != "",
        )
    ).scalars().all()
    results = []
    for order in orders:
        try:
            res = await execute(
                db,
                actor,
                "reconcile_payment",
                {"provider_reference": order.provider_reference},
                idempotency_key=f"bg-reconcile:{order.id}",
            )
        except Exception as exc:  # noqa: BLE001 - one bad order must not stop the batch
            results.append({"order_id": order.id, "decision": "ERROR", "error": str(exc)})
            continue
        results.append({"order_id": order.id, "decision": res.decision.value, "result": res.result})
    return results


async def _recover_stale_workspace(db: Session, actor) -> list[dict]:
    from .models import Task
    from .workflow import RUNNABLE_STATUSES, _is_stale, execute_task

    tasks = db.execute(
        select(Task).where(Task.workspace_id == actor.workspace_id, Task.status.in_(RUNNABLE_STATUSES))
    ).scalars().all()
    recovered = []
    for task in tasks:
        if not _is_stale(task):
            continue
        try:
            await execute_task(db, actor, task.id)
            recovered.append({"task_id": task.id, "status": task.status})
        except Exception as exc:  # noqa: BLE001 - resume is best-effort per task
            recovered.append({"task_id": task.id, "status": "ERROR", "error": str(exc)})
    return recovered


async def _remind_pending_approvals(db: Session) -> list[dict]:
    """Email the owner when a pending approval stays undecided past the threshold."""
    from .models import ApprovalRequest, ApprovalStatus
    from .notify import notify_owner_for_workspace

    threshold = utcnow() - timedelta(minutes=settings.approval_reminder_minutes)
    rows = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
            ApprovalRequest.reminder_sent.is_(False),
            ApprovalRequest.created_at < threshold,
        )
    ).scalars().all()
    sent = []
    for req in rows:
        code = req.confirm_code or ""
        notify_owner_for_workspace(
            db,
            req.workspace_id,
            f"[Krama] 待审批已超时：{req.action}",
            f"动作「{req.action}」已等待超过 {settings.approval_reminder_minutes} 分钟仍未处理，请尽快批准或拒绝。\n"
            f"批准：在控制台待办中点击「批准」，或使用确认码 {code}。\n"
            f"拒绝：在控制台点击「拒绝」。",
        )
        req.reminder_sent = True
        sent.append(req.id)
    if sent:
        db.commit()
    return [{"approval_id": approval_id} for approval_id in sent]


async def _remind_pending_todos(db: Session) -> list[dict]:
    """Email the owner when a must_confirm todo stays undecided past the threshold."""
    from .models import Todo
    from .notify import notify_owner_for_workspace

    threshold = utcnow() - timedelta(minutes=settings.approval_reminder_minutes)
    rows = db.execute(
        select(Todo).where(
            Todo.status == "pending",
            Todo.kind == "must_confirm",
            Todo.reminder_sent.is_(False),
            Todo.created_at < threshold,
        )
    ).scalars().all()
    sent = []
    for todo in rows:
        code = todo.confirm_code or ""
        notify_owner_for_workspace(
            db,
            todo.workspace_id,
            f"[Krama] 待确认已超时：{todo.title}",
            f"「{todo.title}」已等待超过 {settings.approval_reminder_minutes} 分钟仍未处理。\n"
            f"{todo.detail}\n"
            + (f"确认码：{code}\n" if code else "")
            + "请到控制台待办面板批准或拒绝。",
        )
        todo.reminder_sent = True
        sent.append(todo.id)
    if sent:
        db.commit()
    return [{"todo_id": todo_id} for todo_id in sent]


async def _monitor_workspace(db: Session, workspace_id: str) -> list[dict]:
    """Muse-style proactive monitoring: silent signals become owner-facing todos.

    The agent does not disturb the owner unless something actually needs a
    decision: low stock, a paid order not yet shipped, or an active goal that
    reached its target date. Every signal is deduplicated by (ref_type, ref_id).
    """
    from .models import Goal, Order, OrderStatus, Product, Script, Todo

    created: list[dict] = []

    def ensure_todo(kind: str, title: str, detail: str, ref_type: str, ref_id: str):
        existing = db.execute(
            select(Todo).where(
                Todo.workspace_id == workspace_id,
                Todo.status == "pending",
                Todo.ref_type == ref_type,
                Todo.ref_id == ref_id,
            )
        ).scalars().first()
        if existing is not None:
            return None
        todo = Todo(
            workspace_id=workspace_id,
            kind=kind,
            title=title,
            detail=detail,
            status="pending",
            ref_type=ref_type,
            ref_id=ref_id,
        )
        db.add(todo)
        return todo

    products = db.execute(select(Product).where(Product.workspace_id == workspace_id)).scalars().all()
    for product in products:
        if (product.stock or 0) <= 5:
            todo = ensure_todo(
                "needs_reply",
                f"库存预警：{product.name} 仅剩 {product.stock}",
                "建议补货或下架，避免超卖。",
                "monitor_low_stock",
                product.id,
            )
            if todo is not None:
                created.append({"type": "low_stock", "product_id": product.id})

    paid_orders = db.execute(
        select(Order).where(Order.workspace_id == workspace_id, Order.status == OrderStatus.PAID.value)
    ).scalars().all()
    for order in paid_orders:
        todo = ensure_todo(
            "needs_reply",
            f"待发货订单 {order.order_reference or order.id[:8]}",
            f"已收款 ¥{order.total_amount:g}，请尽快发货。",
            "monitor_pending_ship",
            order.id,
        )
        if todo is not None:
            created.append({"type": "pending_ship", "order_id": order.id})

    now = utcnow()
    goals = db.execute(
        select(Goal).where(Goal.workspace_id == workspace_id, Goal.status == "active")
    ).scalars().all()
    for goal in goals:
        if goal.due_at is not None and goal.due_at < now:
            todo = ensure_todo(
                "needs_reply",
                f"目标到期：{goal.title}",
                "该长期目标已到期限，请确认完成、延期或取消。",
                "monitor_goal",
                goal.id,
            )
            if todo is not None:
                created.append({"type": "goal_due", "goal_id": goal.id})

    for product in products:
        if product.status != "published":
            continue
        if (product.price or 0) <= 0:
            todo = ensure_todo(
                "needs_reply",
                f"商品未定价：{product.name}",
                "该商品已上架但售价为空，客户无法成交，请补充单价。",
                "monitor_unpriced",
                product.id,
            )
            if todo is not None:
                created.append({"type": "unpriced", "product_id": product.id})
        if not product.layout_area_id:
            todo = ensure_todo(
                "needs_reply",
                f"商品未归类：{product.name}",
                "该商品已上架但不在任何功能区，客户无法在店铺找到，请归类到产品区。",
                "monitor_uncategorized",
                product.id,
            )
            if todo is not None:
                created.append({"type": "uncategorized", "product_id": product.id})

    stale_unpaid = db.execute(
        select(Order).where(
            Order.workspace_id == workspace_id,
            Order.status == OrderStatus.CREATED.value,
            Order.payment_status != "paid",
            Order.created_at < utcnow() - timedelta(hours=24),
        )
    ).scalars().all()
    for order in stale_unpaid:
        todo = ensure_todo(
            "needs_reply",
            f"待跟进订单 {order.order_reference or order.id[:8]}",
            f"订单 ¥{order.total_amount:g} 创建超过 24 小时仍未付款，请确认继续跟进还是关闭。",
            "monitor_unpaid_order",
            order.id,
        )
        if todo is not None:
            created.append({"type": "unpaid_order", "order_id": order.id})

    top_scripts = db.execute(
        select(Script)
        .where(Script.workspace_id == workspace_id, Script.wins > 0)
        .order_by(Script.wins.desc(), Script.sends.desc())
        .limit(1)
    ).scalars().all()
    for script in top_scripts:
        todo = ensure_todo(
            "info",
            f"话术洞察：{script.title}",
            f"当前转化表现最好（{script.wins}胜/{script.sends}发），可继续沿用并作为报价/成交模板。",
            "monitor_top_script",
            script.id,
        )
        if todo is not None:
            created.append({"type": "top_script", "script_id": script.id})

    return created


async def _sop_auto_dispatch(db: Session, actor) -> list[dict]:
    """P0-2 后台 SOP 派单：为 urgent/due 未成交线索幂等派生跟进待办（无外部调用）。"""
    from .lead_score import score_customer
    from .sop import next_action
    from .models import Conversation, Customer, Message, Todo

    now = utcnow()
    out: list[dict] = []
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    convs = db.execute(select(Conversation).where(Conversation.workspace_id == actor.workspace_id)).scalars().all()
    owner = {c.id: c.customer_id for c in convs}
    msgs: dict[str, list] = {}
    for m in db.execute(select(Message).where(Message.workspace_id == actor.workspace_id)).scalars().all():
        o = owner.get(m.conversation_id)
        if o:
            msgs.setdefault(o, []).append({"content": m.content, "direction": m.direction})
    for c in customers:
        if c.stage == "won":
            continue
        ls = score_customer(c, msgs.get(c.id))
        na = next_action(ls.grade, c.stage, c.last_touch_at, c.next_follow_at, now)
        if na.urgency not in ("urgent", "due"):
            continue
        existing = db.execute(
            select(Todo).where(
                Todo.workspace_id == actor.workspace_id,
                Todo.ref_type == "customer", Todo.ref_id == c.id, Todo.status == "pending",
            )
        ).first()
        if existing:
            continue
        todo = Todo(
            workspace_id=actor.workspace_id, kind="needs_reply",
            title=f"跟进客户 {c.name or c.external_id or c.id}（{ls.grade}级）",
            detail=f"{na.action}\nSLA：{na.sla_text}",
            status="pending", ref_type="customer", ref_id=c.id,
        )
        db.add(todo)
        db.flush()
        out.append({"customer_id": c.id, "grade": ls.grade, "urgency": na.urgency})
    return out


async def _ticket_sla_watch(db: Session, actor) -> list[dict]:
    """P1 工单 SLA 盯防：P0 超15分钟 / P1 超60分钟未闭环，幂等派生提醒待办（不改状态、不外呼）。"""
    from .models import Ticket, Todo

    now = utcnow()
    rows = db.execute(
        select(Ticket).where(
            Ticket.workspace_id == actor.workspace_id,
            Ticket.status.in_(["new", "processing"]),
        )
    ).scalars().all()
    out: list[dict] = []
    for t in rows:
        minutes = 15 if t.priority == "P0" else 60 if t.priority == "P1" else 240
        threshold = timedelta(minutes=minutes)
        base = t.updated_at or t.created_at
        if base is None or now - base < threshold:
            continue
        existing = db.execute(
            select(Todo).where(
                Todo.workspace_id == actor.workspace_id,
                Todo.ref_type == "ticket", Todo.ref_id == t.id, Todo.status == "pending",
            )
        ).first()
        if existing:
            continue
        db.add(Todo(
            workspace_id=actor.workspace_id, kind="needs_reply",
            title=f"工单超时未闭环：{t.title or t.id[:8]}",
            detail=f"{t.priority} 工单已等待超过 {minutes} 分钟，请尽快受理或升级处理。",
            status="pending", ref_type="ticket", ref_id=t.id,
        ))
        out.append({"ticket_id": t.id, "priority": t.priority, "minutes": minutes})
    return out


async def _patrol_live_agents(db: Session, actor) -> list[str]:
    """Auto-patrol live managers on their configured cycle (read-only, idempotent).

    Cycle interval comes from ``execution_limits.patrol_minutes`` (default 360).
    The last-run timestamp is kept in durable AgentMemory, so a manager is not
    re-patrolled inside its own cycle. Every pass still goes through
    ``run_agent_once``'s allowlist / read-only / limits / gateway / audit bounds.
    """
    from .agent_runtime import run_agent_once
    from .models import AIAgent, AgentMemory

    now = utcnow()
    ran: list[str] = []
    agents = db.execute(
        select(AIAgent).where(
            AIAgent.workspace_id == actor.workspace_id,
            AIAgent.status == "live",
        )
    ).scalars().all()
    for agent in agents:
        minutes = int((agent.execution_limits or {}).get("patrol_minutes") or 360)
        stamp_key = f"agent:{agent.id}:last_patrol_at"
        stamp = db.execute(
            select(AgentMemory).where(
                AgentMemory.workspace_id == actor.workspace_id,
                AgentMemory.key == stamp_key,
            )
        ).scalars().first()
        if stamp is not None:
            try:
                last_dt = datetime.fromisoformat(stamp.value)
                if now - last_dt < timedelta(minutes=minutes):
                    continue
            except (ValueError, TypeError):
                pass
        await run_agent_once(db, actor, agent)
        if stamp is None:
            stamp = AgentMemory(
                workspace_id=actor.workspace_id,
                key=stamp_key,
                value=now.isoformat(),
                kind="note",
            )
            db.add(stamp)
        else:
            stamp.value = now.isoformat()
        ran.append(agent.id)
    db.commit()
    return ran


async def _run_scheduled_jobs(db: Session) -> list[dict]:
    """Execute due time-based owner-registered workflows through the gateway.

    Trigger types handled here: ``interval`` (recurring, advances next_run_at)
    and ``once`` (fires once, then disables itself). ``event`` jobs are not
    time-based and are dispatched by :func:`dispatch_event` instead.
    """
    from datetime import timedelta

    from .models import Membership, Role, ScheduledJob, User, utcnow
    from .security import Actor
    from .workflow import create_task, execute_task

    now = utcnow()
    rows = db.execute(
        select(ScheduledJob).where(
            ScheduledJob.enabled.is_(True),
            ScheduledJob.next_run_at <= now,
        )
    ).scalars().all()
    results = []
    for job in rows:
        trig = job.trigger or {}
        ttype = trig.get("type") or "interval"
        if ttype == "event":
            continue
        owner = db.execute(
            select(Membership).where(
                Membership.workspace_id == job.workspace_id,
                Membership.role == Role.OWNER.value,
            )
        ).scalars().first()
        if owner is None:
            continue
        user = db.get(User, owner.user_id)
        actor = Actor(user=user, workspace_id=job.workspace_id, role=Role.OWNER)
        try:
            task = create_task(db, actor, job.goal or job.name, steps=job.steps or None)
            await execute_task(db, actor, task.id)
            results.append({"job_id": job.id, "task_id": task.id, "status": task.status, "trigger": ttype})
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
            results.append({"job_id": job.id, "status": "ERROR", "error": str(exc)})
        job.last_run_at = now
        if ttype == "once":
            # One-shot: fire exactly once and disable; do not reschedule.
            job.enabled = False
        else:
            minutes = int(trig.get("minutes") or 60)
            job.next_run_at = now + timedelta(minutes=minutes)
    if results:
        db.commit()
    return results


async def dispatch_event(db: Session, actor, event_name: str, payload: dict | None = None) -> list[dict]:
    """Fire ``event``-type jobs registered for this event, through the gateway.

    A job's optional ``condition`` (structured op, same schema as a workflow
    branch) is evaluated against the event payload; a ``cooldown_seconds``
    window suppresses repeated firing and is stamped in AgentMemory. Each job
    runs independently; one failure never blocks the others.
    """
    from datetime import timedelta

    from .models import AgentMemory, ScheduledJob
    from .workflow import _eval_condition, create_task, execute_task

    payload = payload or {}
    now = utcnow()
    rows = db.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == actor.workspace_id,
            ScheduledJob.enabled.is_(True),
        )
    ).scalars().all()
    results = []
    for job in rows:
        trig = job.trigger or {}
        if trig.get("type") != "event" or trig.get("event") != event_name:
            continue
        cond = trig.get("condition")
        if cond is not None and not _eval_condition(cond, {}, {"result": payload}):
            results.append({"job_id": job.id, "status": "SKIPPED", "reason": "condition not met"})
            continue
        cooldown = int(trig.get("cooldown_seconds") or 0)
        stamp_key = f"event:{job.id}:last_fire_at"
        stamp = db.execute(
            select(AgentMemory).where(
                AgentMemory.workspace_id == actor.workspace_id,
                AgentMemory.key == stamp_key,
            )
        ).scalars().first()
        if cooldown > 0 and stamp is not None:
            try:
                last_dt = datetime.fromisoformat(stamp.value)
                if now - last_dt < timedelta(seconds=cooldown):
                    results.append({"job_id": job.id, "status": "COOLDOWN"})
                    continue
            except (ValueError, TypeError):
                pass
        try:
            task = create_task(db, actor, job.goal or job.name, steps=job.steps or None)
            await execute_task(db, actor, task.id)
            results.append({"job_id": job.id, "task_id": task.id, "status": task.status, "trigger": "event"})
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
            results.append({"job_id": job.id, "status": "ERROR", "error": str(exc)})
            continue
        job.last_run_at = now
        if stamp is None:
            db.add(AgentMemory(
                workspace_id=actor.workspace_id, key=stamp_key,
                value=now.isoformat(), kind="note",
            ))
        else:
            stamp.value = now.isoformat()
    db.commit()
    return results


async def run_due_jobs(db: Session) -> list[dict]:
    """Run reconciliation and stale-task recovery for every workspace."""
    from .models import Membership, Role, User, Workspace
    from .security import Actor

    report = []
    for ws in db.execute(select(Workspace)).scalars().all():
        owner = db.execute(
            select(Membership).where(
                Membership.workspace_id == ws.id,
                Membership.role == Role.OWNER.value,
            )
        ).scalars().first()
        if owner is None:
            continue
        user = db.get(User, owner.user_id)
        actor = Actor(user=user, workspace_id=ws.id, role=Role.OWNER)
        report.append(
            {
                "workspace_id": ws.id,
                "reconciled": await _reconcile_with_backoff(db, ws.id, actor),
                "recovered": await _recover_stale_workspace(db, actor),
                "monitored": await _monitor_workspace(db, ws.id),
                "patrolled": await _patrol_live_agents(db, actor),
                "sop_dispatched": await _sop_auto_dispatch(db, actor),
                "ticket_sla": await _ticket_sla_watch(db, actor),
            }
        )
    report.append({"approval_reminders": await _remind_pending_approvals(db), "todo_reminders": await _remind_pending_todos(db)})
    report.append({"scheduled_jobs": await _run_scheduled_jobs(db)})
    if settings.retention_enabled:
        from .retention import cleanup_all

        report.append({"retention": cleanup_all(db, overrides={
            "messages": settings.retention_messages_days,
            "conversations": settings.retention_conversations_days,
            "stock_movements": settings.retention_stock_movements_days,
            "todos": settings.retention_todos_days,
        })})
    db.commit()
    return report


async def _loop() -> None:
    from .db import SessionLocal

    while True:
        await asyncio.sleep(settings.scheduler_interval_seconds)
        db = SessionLocal()
        try:
            await run_due_jobs(db)
        except Exception:  # noqa: BLE001 - background loop must survive transient errors
            db.rollback()
        finally:
            db.close()


def start(app) -> asyncio.Task | None:
    if not settings.scheduler_enabled:
        return None
    task = asyncio.create_task(_loop())
    app.state.background_scheduler = task
    return task


async def stop(app) -> None:
    task = getattr(app.state, "background_scheduler", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
