"""FastAPI application and HTTP routes for the Krama MVP."""
from __future__ import annotations

import json
import pathlib
import secrets
import time
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schemas
from .config import settings
from .db import SessionLocal, get_db
from .gateway import GatewayError, GatewayResult, approve, execute, reject
from .models import (
    AIAgent,
    AgentMemory,
    ApprovalRequest,
    ApprovalStatus,
    CommercePolicy,
    ChangeRequest,
    ConnectorInstance,
    ContentItem,
    SocialComment,
    TrendingVideo,
    ChannelAccount,
    Conversation,
    Customer,
    Decision,
    Goal,
    LayoutArea,
    Membership,
    MerchantProfile,
    Message,
    Module,
    Order,
    Product,
    Role,
    ScheduledJob,
    Shipment,
    StockMovement,
    Task,
    Ticket,
    TicketEvent,
    Faq,
    Todo,
    User,
    Workspace,
    WorkflowGraph,
    WorkspaceFunction,
    WorkspacePanel,
    WorkspacePanelSnapshot,
    WorkspaceSettingSnapshot,
)
from .security import Actor, issue_api_key, require_actor, require_owner, require_user
from .seed import bootstrap
from .data_classification import build_data_map
from .observability import configure_logging, record_chat, record_tool, record_webhook
from .tools import ToolContext, _business_overview, registry
from .workflow import create_task, execute_task
from .audit import record as audit_record
from .backup import BackupError, create_backup, list_backups, prune_backups, restore_backup
from .lead_score import score_customer, grade_badge
from .sop import next_action as sop_next_action
from .cs_routing import needs_human, match_faq, recommend_script, ticket_sla
from . import dag as dag_core
from .dag import build_context, route_branch, pruned_by_branch, layerize


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    bootstrap()
    from .scheduler import start, stop

    start(app)
    from .backup import start_auto_backup, stop_auto_backup

    start_auto_backup(app)
    yield
    await stop_auto_backup(app)
    await stop(app)


app = FastAPI(title="Krama Agent Platform", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _observability_middleware(request: Request, call_next):
    from .request_id import clear_request_id, new_request_id, set_request_id
    from .observability import record_request

    request_id = request.headers.get("X-Request-ID") or new_request_id()
    set_request_id(request_id)
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        status = response.status_code if response is not None else 500
        record_request(request.method, request.url.path, status, duration_ms)
        if response is not None:
            response.headers["X-Request-ID"] = request_id
        clear_request_id()


_STATIC_DIR = pathlib.Path(__file__).parent / "static"


@app.get("/")
def console_root() -> FileResponse:
    r = FileResponse(_STATIC_DIR / "home.html")
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r


@app.get("/console")
def classic_console() -> RedirectResponse:
    """旧工作台已停用：任何旧入口都回到新控制台首页。"""
    return RedirectResponse(url="/", status_code=301)


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict:
    """Readiness probe: 200 when the DB answers SELECT 1, else 503."""
    from .db import ping_db

    ok, detail = ping_db()
    if not ok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"db not ready: {detail}")
    return {"status": "ready", "db": "ok"}


@app.get("/health/metrics")
def health_metrics() -> dict:
    from .observability import registry

    return registry.snapshot()


@app.get("/workspaces/{workspace_id}/metrics")
def workspace_metrics(actor: Actor = Depends(require_actor)) -> dict:
    from .observability import registry

    return {"workspace_id": actor.workspace_id, **registry.snapshot()}


@app.get("/workspaces/{workspace_id}/alerts")
def workspace_alerts(actor: Actor = Depends(require_actor)) -> dict:
    from .alerts import evaluate
    from .observability import registry

    snapshot = registry.snapshot()
    alerts = evaluate(snapshot["metrics"])
    return {"workspace_id": actor.workspace_id, "alerts": [a.as_dict() for a in alerts]}


@app.post("/workspaces/{workspace_id}/alerts/check")
def check_alerts(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    from .alerts import dispatch_alert, evaluate
    from .observability import registry

    snapshot = registry.snapshot()
    alerts = evaluate(snapshot["metrics"])
    deliveries = [dispatch_alert(db, actor.workspace_id, a) for a in alerts]
    return {
        "workspace_id": actor.workspace_id,
        "triggered": len(alerts),
        "alerts": [a.as_dict() for a in alerts],
        "deliveries": deliveries,
    }


@app.get("/workspaces/{workspace_id}/retention/policy")
def retention_policy(actor: Actor = Depends(require_actor)) -> dict:
    from .retention import CLEANUP_RULES, PROTECTED

    return {
        "workspace_id": actor.workspace_id,
        "enabled": settings.retention_enabled,
        "rules": {table: {"retention_days": days, "extra": extra.strip()}
                  for table, (days, extra) in CLEANUP_RULES.items()},
        "protected_tables": sorted(PROTECTED),
    }


@app.post("/workspaces/{workspace_id}/retention/run")
def run_retention(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    from .retention import cleanup_workspace

    overrides = {
        "messages": settings.retention_messages_days,
        "conversations": settings.retention_conversations_days,
        "stock_movements": settings.retention_stock_movements_days,
        "todos": settings.retention_todos_days,
    }
    report = cleanup_workspace(db, actor.workspace_id, overrides=overrides)
    db.commit()
    return {"workspace_id": actor.workspace_id, "purged": report}


@app.get("/workspaces/{workspace_id}/data-map")
def workspace_data_map(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .data_classification import build_data_map
    from sqlalchemy import select

    ws = db.get(Workspace, actor.workspace_id)
    region = ws.region if ws else "UNKNOWN"
    return build_data_map(db, actor.workspace_id, region)


@app.get("/workspaces/{workspace_id}/overview")
async def workspace_overview(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    return await _business_overview(ToolContext(db=db, actor=actor, params={}))


@app.get("/workspaces/{workspace_id}/brain/events")
def brain_events_route(limit: int = 50, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .brain import brain_events

    return {"workspace_id": actor.workspace_id, "events": brain_events(db, actor.workspace_id, limit)}


@app.get("/workspaces/{workspace_id}/audit/integrity")
def audit_integrity_route(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .audit import verify_workspace_chain

    result = verify_workspace_chain(db, actor.workspace_id)
    result["workspace_id"] = actor.workspace_id
    return result


@app.get("/workspaces/{workspace_id}/brain/stream")
def brain_stream(interval: float = 0.8, once: bool = False, actor: Actor = Depends(require_actor)) -> StreamingResponse:
    from .brain import brain_event_stream

    return StreamingResponse(
        brain_event_stream(SessionLocal, actor.workspace_id, interval, once),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/workspaces/{workspace_id}/brain/preflight")
def brain_preflight(payload: dict = Body(default={}), actor: Actor = Depends(require_actor)) -> dict:
    from .brain import preflight_hints

    action = payload.get("action") or payload.get("tool") or ""
    params = payload.get("params") or payload.get("payload") or {}
    return {"workspace_id": actor.workspace_id, "hints": preflight_hints(action, params, actor.workspace_id)}


@app.get("/panels/registry")
def panel_registry() -> dict:
    from .panels import registry

    return {"components": registry()}


@app.post("/workspaces/{workspace_id}/panels/validate")
def panel_validate(payload: dict = Body(default={}), actor: Actor = Depends(require_actor)) -> dict:
    from .panels import validate_panel_config

    ok, errors, normalized = validate_panel_config(payload.get("config") or payload)
    return {"workspace_id": actor.workspace_id, "ok": ok, "errors": errors, "panel": normalized}


def _panel_out(panel: WorkspacePanel, positions: dict | None = None) -> dict:
    positions = positions or {}
    return {
        "panel_id": panel.id,
        "type": panel.panel_type,
        "title": panel.title,
        "data_source": panel.data_source,
        "status": panel.status,
        "version": panel.version,
        "permissions": panel.permissions or {},
        "layout": positions.get(panel.id) or {},
        "parent_id": panel.parent_id,
    }


def _neural_positions(db: Session, workspace_id: str) -> dict:
    ws = db.get(Workspace, workspace_id)
    settings = dict(ws.settings or {}) if ws else {}
    return settings.get("neural_layout") or {}


@app.get("/workspaces/{workspace_id}/panels")
def list_panels(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(WorkspacePanel)
        .where(WorkspacePanel.workspace_id == actor.workspace_id)
        .order_by(WorkspacePanel.created_at)
    ).scalars().all()
    positions = _neural_positions(db, actor.workspace_id)
    return {"panels": [_panel_out(p, positions) for p in rows]}


@app.post("/workspaces/{workspace_id}/panels", status_code=201)
def create_panel(payload: schemas.PanelCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .panels import validate_panel_config

    ok, errors, normalized = validate_panel_config({
        "type": payload.type,
        "title": payload.title,
        "data_source": payload.data_source,
        "status": payload.status,
        "version": payload.version,
    })
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "；".join(errors))
    panel = WorkspacePanel(
        workspace_id=actor.workspace_id,
        panel_type=normalized["type"],
        title=normalized["title"],
        data_source=normalized["data_source"],
        status=normalized["status"],
        version=normalized["version"],
        permissions=payload.permissions or {},
        parent_id=payload.parent_id,
    )
    db.add(panel)
    audit_record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id, action="create_panel",
        risk_level="R1", decision=Decision.ALLOWED, resource=f"panel:{normalized['type']}",
        reason=f"创建面板 {normalized['title']}", data_tier="INTERNAL",
    )
    db.commit()
    db.refresh(panel)
    return {"panel": _panel_out(panel, _neural_positions(db, actor.workspace_id))}


@app.patch("/workspaces/{workspace_id}/panels/{panel_id}")
def update_panel(panel_id: str, payload: schemas.PanelUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .panels import validate_panel_config

    panel = db.get(WorkspacePanel, panel_id)
    if panel is None or panel.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel not found")

    merged = {
        "type": payload.type if payload.type is not None else panel.panel_type,
        "title": payload.title if payload.title is not None else panel.title,
        "data_source": payload.data_source if payload.data_source is not None else panel.data_source,
        "status": payload.status if payload.status is not None else panel.status,
        "version": payload.version if payload.version is not None else panel.version,
    }
    ok, errors, normalized = validate_panel_config(merged)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "；".join(errors))

    db.add(WorkspacePanelSnapshot(
        workspace_id=panel.workspace_id,
        panel_id=panel.id,
        version=panel.version,
        panel_type=panel.panel_type,
        title=panel.title,
        data_source=panel.data_source,
        status=panel.status,
        permissions=panel.permissions or {},
        parent_id=panel.parent_id,
        note="update",
    ))
    panel.panel_type = normalized["type"]
    panel.title = normalized["title"]
    panel.data_source = normalized["data_source"]
    panel.status = normalized["status"]
    panel.version = panel.version + 1
    if payload.permissions is not None:
        panel.permissions = payload.permissions
    if payload.parent_id is not None:
        panel.parent_id = payload.parent_id
    audit_record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id, action="update_panel",
        risk_level="R1", decision=Decision.ALLOWED, resource=f"panel:{panel.id}",
        reason=f"更新面板 {panel.title} -> v{panel.version}", data_tier="INTERNAL",
    )
    db.commit()
    db.refresh(panel)
    return {"panel": _panel_out(panel, _neural_positions(db, actor.workspace_id))}


@app.delete("/workspaces/{workspace_id}/panels/{panel_id}", status_code=204)
def delete_panel(panel_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    panel = db.get(WorkspacePanel, panel_id)
    if panel is None or panel.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel not found")
    audit_record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id, action="delete_panel",
        risk_level="R2", decision=Decision.ALLOWED, resource=f"panel:{panel.id}",
        reason=f"删除面板 {panel.title}", data_tier="INTERNAL",
    )
    db.delete(panel)
    db.commit()


@app.get("/workspaces/{workspace_id}/panels/{panel_id}/history")
def panel_history(panel_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    panel = db.get(WorkspacePanel, panel_id)
    if panel is None or panel.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel not found")
    rows = db.execute(
        select(WorkspacePanelSnapshot)
        .where(WorkspacePanelSnapshot.workspace_id == actor.workspace_id, WorkspacePanelSnapshot.panel_id == panel_id)
        .order_by(WorkspacePanelSnapshot.version.desc())
        .limit(50)
    ).scalars().all()
    return {
        "versions": [
            {
                "version": snap.version,
                "panel_type": snap.panel_type,
                "title": snap.title,
                "data_source": snap.data_source,
                "status": snap.status,
                "note": snap.note,
                "created_at": snap.created_at.isoformat(),
            }
            for snap in rows
        ]
    }


@app.post("/workspaces/{workspace_id}/panels/{panel_id}/rollback/{version}")
def panel_rollback(panel_id: str, version: int, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    if actor.role is not Role.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the workspace owner can rollback panels")
    panel = db.get(WorkspacePanel, panel_id)
    if panel is None or panel.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel not found")
    snap = db.execute(
        select(WorkspacePanelSnapshot).where(
            WorkspacePanelSnapshot.workspace_id == actor.workspace_id,
            WorkspacePanelSnapshot.panel_id == panel_id,
            WorkspacePanelSnapshot.version == version,
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel version not found")
    db.add(WorkspacePanelSnapshot(
        workspace_id=panel.workspace_id,
        panel_id=panel.id,
        version=panel.version,
        panel_type=panel.panel_type,
        title=panel.title,
        data_source=panel.data_source,
        status=panel.status,
        permissions=panel.permissions or {},
        parent_id=panel.parent_id,
        note=f"pre-rollback to v{version}",
    ))
    panel.panel_type = snap.panel_type
    panel.title = snap.title
    panel.data_source = snap.data_source
    panel.status = snap.status
    panel.version = panel.version + 1
    panel.permissions = snap.permissions or {}
    if snap.parent_id:
        panel.parent_id = snap.parent_id
    audit_record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id, action="rollback_panel",
        risk_level="R2", decision=Decision.ALLOWED, resource=f"panel:{panel.id}",
        reason=f"回滚面板 {panel.title} 到 v{version}", data_tier="INTERNAL",
    )
    db.commit()
    db.refresh(panel)
    return {"panel": _panel_out(panel, _neural_positions(db, actor.workspace_id))}


@app.get("/workspaces/{workspace_id}/neural/layout")
def get_neural_layout(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    return {"workspace_id": actor.workspace_id, "positions": _neural_positions(db, actor.workspace_id)}


@app.put("/workspaces/{workspace_id}/neural/layout")
def put_neural_layout(payload: schemas.NeuralLayoutUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    ws = db.get(Workspace, actor.workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    settings = dict(ws.settings or {})
    settings["neural_layout"] = payload.positions or {}
    ws.settings = settings
    audit_record(
        db, workspace_id=actor.workspace_id, actor_id=actor.user.id, action="save_neural_layout",
        risk_level="R1", decision=Decision.ALLOWED, resource="neural_layout",
        reason="保存神经控制台布局", data_tier="INTERNAL",
    )
    db.commit()
    db.refresh(ws)
    return {"workspace_id": actor.workspace_id, "positions": (ws.settings or {}).get("neural_layout", {})}



@app.post("/users", status_code=201)
def create_user(payload: schemas.UserCreate, db: Session = Depends(get_db)) -> dict:
    user = User(email=payload.email, display_name=payload.display_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "email": user.email}


@app.post("/users/{user_id}/api-keys", status_code=201)
def create_api_key(user_id: str, db: Session = Depends(get_db)) -> schemas.ApiKeyOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    secret, prefix = issue_api_key(db, user)
    return schemas.ApiKeyOut(api_key=secret, prefix=prefix)


@app.post("/workspaces", status_code=201)
def create_workspace(payload: schemas.WorkspaceCreate, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    from .connectors import ensure_default_connectors

    ws = Workspace(name=payload.name, region=payload.region, business_category=payload.business_category)
    db.add(ws)
    db.flush()
    db.add(Membership(workspace_id=ws.id, user_id=user.id, role=Role.OWNER.value))
    ensure_default_connectors(db, ws.id)
    db.commit()
    return {"id": ws.id, "name": ws.name, "region": ws.region}


@app.post("/workspaces/{workspace_id}/merchant", status_code=201)
def create_merchant(payload: schemas.MerchantCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    profile = MerchantProfile(
        workspace_id=actor.workspace_id,
        shop_name=payload.shop_name,
        category=payload.category,
        region=payload.region,
        currency=payload.currency,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return {"id": profile.id, "shop_name": profile.shop_name}


@app.put("/workspaces/{workspace_id}/merchant")
def upsert_merchant(payload: schemas.MerchantCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """更新（或首次创建）本工作区店铺资料。owner/member 可写，留空字段保持原值。"""
    profile = db.execute(
        select(MerchantProfile).where(MerchantProfile.workspace_id == actor.workspace_id)
    ).scalars().first()
    if profile is None:
        profile = MerchantProfile(workspace_id=actor.workspace_id)
        db.add(profile)
    if payload.shop_name:
        profile.shop_name = payload.shop_name
    if payload.category is not None:
        profile.category = payload.category
    if payload.region is not None:
        profile.region = payload.region
    if payload.currency is not None:
        profile.currency = payload.currency
    db.commit()
    db.refresh(profile)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="merchant_profile_updated", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"merchant:{profile.id}", reason="更新店铺资料", evidence={}, data_tier="INTERNAL")
    return {"id": profile.id, "shop_name": profile.shop_name, "category": profile.category, "region": profile.region}


@app.get("/workspaces/{workspace_id}/products")
def list_products(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id)).scalars().all()
    return {
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "status": p.status,
                "stock": p.stock,
                "photo_url": p.photo_url,
                "specs": p.specs or {},
                "detail": p.detail,
                "qualifications": p.qualifications or [],
                "layout_area_id": p.layout_area_id,
            }
            for p in rows
        ]
    }


@app.post("/workspaces/{workspace_id}/products", status_code=201)
def add_product(payload: schemas.ProductCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    product = Product(
        workspace_id=actor.workspace_id,
        name=payload.name,
        category=payload.category,
        price=payload.price,
        status="draft",
        photo_url=payload.photo_url,
        specs=payload.specs,
        detail=payload.detail,
        qualifications=payload.qualifications,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "status": product.status,
        "photo_url": product.photo_url,
        "specs": product.specs,
        "detail": product.detail,
        "qualifications": product.qualifications,
        "layout_area_id": product.layout_area_id,
    }


@app.patch("/workspaces/{workspace_id}/products/{product_id}")
def update_product(product_id: str, payload: schemas.ProductUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None or product.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "product not found")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "price" and value is not None:
            product.price = float(value)
        else:
            setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "status": product.status,
        "photo_url": product.photo_url,
        "specs": product.specs,
        "detail": product.detail,
        "qualifications": product.qualifications,
        "layout_area_id": product.layout_area_id,
    }


@app.get("/workspaces/{workspace_id}/orders")
def list_orders(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Order).where(Order.workspace_id == actor.workspace_id).order_by(Order.created_at.desc())).scalars().all()
    return {
        "orders": [
            {
                "id": o.id,
                "channel": o.channel,
                "order_reference": o.order_reference,
                "provider_reference": o.provider_reference,
                "status": o.status,
                "payment_status": o.payment_status,
                "total_amount": o.total_amount,
                "currency": o.currency,
                "customer": o.customer,
                "items": o.items,
            }
            for o in rows
        ]
    }


@app.get("/workspaces/{workspace_id}/orders/{order_id}")
def get_order(order_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    order = db.get(Order, order_id)
    if order is None or order.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    return {
        "id": order.id,
        "channel": order.channel,
        "order_reference": order.order_reference,
        "provider_reference": order.provider_reference,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_amount": order.total_amount,
        "currency": order.currency,
        "customer": order.customer,
        "items": order.items,
    }


@app.get("/workspaces/{workspace_id}/inventory")
def list_inventory(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id).order_by(Product.created_at)).scalars().all()
    movements = db.execute(select(StockMovement).where(StockMovement.workspace_id == actor.workspace_id).order_by(StockMovement.created_at.desc()).limit(50)).scalars().all()
    return {
        "inventory": [
            {"product_id": p.id, "name": p.name, "stock": p.stock, "price": p.price, "status": p.status}
            for p in rows
        ],
        "recent_movements": [
            {"id": m.id, "product_id": m.product_id, "order_id": m.order_id, "delta": m.delta, "reason": m.reason}
            for m in movements
        ],
    }


@app.get("/workspaces/{workspace_id}/shipments")
def list_shipments(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Shipment).where(Shipment.workspace_id == actor.workspace_id).order_by(Shipment.created_at.desc())).scalars().all()
    return {
        "shipments": [
            {"id": s.id, "order_id": s.order_id, "carrier": s.carrier, "tracking_number": s.tracking_number, "status": s.status}
            for s in rows
        ]
    }


@app.get("/workspaces/{workspace_id}/customers")
def list_customers(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id).order_by(Customer.updated_at.desc())).scalars().all()
    return {
        "customers": [
            {"id": c.id, "channel": c.channel, "external_id": c.external_id, "name": c.name, "stage": c.stage, "profile": c.profile}
            for c in rows
        ]
    }


@app.get("/workspaces/{workspace_id}/leads")
def list_leads(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P0 线索分级只读视图：本地规则评分现算，不写库、不调用外部渠道。"""
    rows = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    convs = db.execute(select(Conversation).where(Conversation.workspace_id == actor.workspace_id)).scalars().all()
    conv_owner = {c.id: c.customer_id for c in convs}
    all_msgs = db.execute(select(Message).where(Message.workspace_id == actor.workspace_id)).scalars().all()
    msgs_by_cust: dict[str, list[dict]] = {}
    for m in all_msgs:
        owner = conv_owner.get(m.conversation_id)
        if owner:
            msgs_by_cust.setdefault(owner, []).append({"content": m.content, "direction": m.direction})
    leads: list[dict] = []
    distribution = {"H": 0, "A": 0, "B": 0, "C": 0}
    for c in rows:
        ls = score_customer(c, msgs_by_cust.get(c.id))
        distribution[ls.grade] = distribution.get(ls.grade, 0) + 1
        badge = grade_badge(ls.grade)
        na = sop_next_action(ls.grade, c.stage, c.last_touch_at, c.next_follow_at, datetime.utcnow())
        leads.append({
            "id": c.id, "name": c.name, "channel": c.channel, "external_id": c.external_id,
            "stage": c.stage, "score": ls.score, "grade": ls.grade, "label": ls.label,
            "signals": ls.signals, "source": c.source, "badge": badge,
            "next_follow_at": c.next_follow_at.isoformat() if c.next_follow_at else None,
            "next_action": {
                "action": na.action, "sla": na.sla_text,
                "urgency": na.urgency, "due_in_hours": na.due_in_hours,
            },
        })
    leads.sort(key=lambda x: (-x["score"], x["name"]))
    return {"leads": leads, "distribution": distribution}


@app.get("/workspaces/{workspace_id}/tickets")
def list_tickets(status: str | None = None, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P1 工单只读列表：可按状态过滤，按优先级与时间排序，不写库。"""
    query = select(Ticket).where(Ticket.workspace_id == actor.workspace_id)
    if status:
        query = query.where(Ticket.status == status)
    rows = db.execute(query.order_by(Ticket.priority.asc(), Ticket.created_at.desc())).scalars().all()
    return {"tickets": [_ticket_out(t) for t in rows]}


def _ticket_out(t: Ticket) -> dict:
    from .models import utcnow
    sla = ticket_sla(t.priority, t.created_at, t.status, utcnow())
    return {
        "id": t.id, "customer_id": t.customer_id, "order_id": t.order_id,
        "channel": t.channel, "type": t.type, "priority": t.priority,
        "status": t.status, "assignee_id": t.assignee_id,
        "title": t.title, "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "sla": {
            "state": sla.state, "age_minutes": sla.age_minutes,
            "limit_minutes": sla.limit_minutes,
            "remaining_minutes": sla.remaining_minutes, "label": sla.label,
        },
    }


def _ticket_event(db: Session, t: Ticket, event: str, actor, note: str = "",
                  from_status: str = "", to_status: str = "") -> None:
    """追加一条工单审计时间线，并同步写入全系统哈希链审计（同事务，不单独提交）。"""
    user = getattr(actor, "user", None)
    label = ""
    if user is not None:
        label = getattr(user, "username", None) or getattr(user, "email", None) or str(user.id)
    db.add(TicketEvent(
        workspace_id=t.workspace_id, ticket_id=t.id, event=event,
        from_status=from_status, to_status=to_status,
        actor_label=label, note=str(note)[:500],
    ))
    audit_record(
        db,
        workspace_id=t.workspace_id,
        actor_id=getattr(user, "id", "") or "system",
        action=f"ticket_{event}",
        risk_level="R0",
        decision=Decision.ALLOWED,
        resource=f"ticket:{t.id}",
        reason=str(note)[:300],
        evidence={"ticket_id": t.id, "event": event, "from": from_status, "to": to_status,
                  "priority": t.priority, "type": t.type},
        data_tier="INTERNAL",
    )


@app.post("/workspaces/{workspace_id}/tickets", status_code=201)
def create_ticket(payload: dict = Body(...), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    priority = payload.get("priority") if payload.get("priority") in ("P0", "P1", "P2") else "P2"
    ticket = Ticket(
        workspace_id=actor.workspace_id,
        customer_id=str(payload.get("customer_id") or ""),
        order_id=str(payload.get("order_id") or ""),
        channel=str(payload.get("channel") or "chat"),
        type=str(payload.get("type") or "other"),
        priority=priority,
        status="new",
        title=str(payload.get("title") or "")[:200],
        description=str(payload.get("description") or ""),
    )
    db.add(ticket)
    db.flush()
    _ticket_event(db, ticket, "created", actor, note=ticket.title)
    db.commit()
    db.refresh(ticket)
    return _ticket_out(ticket)


@app.post("/workspaces/{workspace_id}/tickets/{ticket_id}/status")
def update_ticket_status(ticket_id: str, payload: dict = Body(...), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
    new_status = payload.get("status")
    valid = {"new", "processing", "waiting_reply", "closed", "escalated"}
    if new_status not in valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid status")
    old_status = ticket.status
    ticket.status = new_status
    if new_status == "closed":
        ticket.closed_at = datetime.utcnow()
        _ticket_event(db, ticket, "closed", actor, note=str(payload.get("note") or "工单关闭"),
                      from_status=old_status, to_status=new_status)
    elif new_status == "escalated":
        _ticket_event(db, ticket, "escalated", actor, note="升级处理",
                      from_status=old_status, to_status=new_status)
    else:
        _ticket_event(db, ticket, "status_change", actor, note=str(payload.get("note") or ""),
                      from_status=old_status, to_status=new_status)
    db.commit()
    db.refresh(ticket)
    return _ticket_out(ticket)


@app.post("/workspaces/{workspace_id}/tickets/{ticket_id}/claim")
def claim_ticket(ticket_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P1-2 认领闭环：认领人记录 assignee_id，new 单自动转 processing。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
    old_status = ticket.status
    ticket.assignee_id = str(actor.user.id)
    if ticket.status == "new":
        ticket.status = "processing"
    _ticket_event(db, ticket, "claimed", actor, note="认领受理", from_status=old_status, to_status=ticket.status)
    db.commit()
    db.refresh(ticket)
    return _ticket_out(ticket)


@app.get("/workspaces/{workspace_id}/tickets/{ticket_id}/events")
def ticket_events(ticket_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """工单审计时间线只读：创建/认领/流转/关闭全程留痕。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
    rows = db.execute(
        select(TicketEvent)
        .where(TicketEvent.workspace_id == actor.workspace_id, TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
    ).scalars().all()
    return {"ticket_id": ticket_id, "events": [
        {"id": e.id, "event": e.event, "from_status": e.from_status, "to_status": e.to_status,
         "actor_label": e.actor_label, "note": e.note,
         "created_at": e.created_at.isoformat() if e.created_at else None}
        for e in rows
    ]}


@app.post("/workspaces/{workspace_id}/cs/diagnose")
def cs_diagnose(payload: dict = Body(...), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """接待诊断：对一条客户消息做转人工判定 + FAQ 命中 + 话术推荐，纯计算不写库。"""
    text = str(payload.get("text") or "").strip()
    route = needs_human(text)
    faq_rows = [{"question": f.question, "answer": f.answer, "tags": f.tags}
                for f in db.execute(select(Faq).where(Faq.workspace_id == actor.workspace_id)).scalars().all()]
    faq = match_faq(text, faq_rows)
    rec = recommend_script(text, payload.get("scripts") or [])
    return {
        "route": route.route,
        "need_human": route.need_human,
        "reason": route.reason,
        "priority": route.priority,
        "action": route.action,
        "faq": {"matched": faq.matched, "question": faq.question, "answer": faq.answer, "score": faq.score},
        "script": ({"title": rec.get("title"), "content": rec.get("content"), "tags": rec.get("tags")} if rec else None),
    }


@app.post("/workspaces/{workspace_id}/cs/handle")
def cs_handle(payload: dict = Body(...), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """接待落库：客户消息入库 → FAQ命中回标准答复(计命中)；敏感/售后自动建工单转人工，不承诺退款/赔偿。"""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty text")
    customer_id = str(payload.get("customer_id") or "")
    channel = str(payload.get("channel") or "chat")

    conv = None
    if customer_id:
        conv = db.execute(
            select(Conversation).where(
                Conversation.workspace_id == actor.workspace_id,
                Conversation.customer_id == customer_id,
                Conversation.state == "open",
            ).order_by(Conversation.updated_at.desc())
        ).scalars().first()
    if conv is None:
        conv = Conversation(workspace_id=actor.workspace_id, customer_id=customer_id, channel=channel, state="open")
        db.add(conv)
        db.flush()

    db.add(Message(workspace_id=actor.workspace_id, conversation_id=conv.id,
                   direction="in", content=text, meta={"source": "cs_handle"}))

    route = needs_human(text)
    faq_rows = [{"id": f.id, "question": f.question, "answer": f.answer, "tags": f.tags}
                for f in db.execute(select(Faq).where(Faq.workspace_id == actor.workspace_id)).scalars().all()]
    faq = match_faq(text, faq_rows)

    mode = "fallback"
    reply = "已收到你的消息，稍后为你跟进。"
    ticket_id = None

    if faq.matched:
        mode = "faq"
        reply = faq.answer
        db.add(Message(workspace_id=actor.workspace_id, conversation_id=conv.id,
                       direction="out", content=reply, meta={"mode": "faq", "faq_question": faq.question}))
        hit = next((f for f in faq_rows if f["question"] == faq.question), None)
        if hit:
            fobj = db.get(Faq, hit["id"])
            if fobj:
                fobj.hits = (fobj.hits or 0) + 1
    elif route.need_human:
        mode = "human"
        reason = route.reason
        ttype = "complaint"
        if "退款" in reason or "退货" in reason:
            ttype = "refund"
        elif "售后" in reason or "质量" in reason:
            ttype = "aftersale"
        ticket = Ticket(
            workspace_id=actor.workspace_id, customer_id=customer_id, channel=channel,
            type=ttype, priority=route.priority, status="new",
            title=text[:200], description=reason,
        )
        db.add(ticket)
        db.flush()
        _ticket_event(db, ticket, "created", actor, note=f"接待自动建单：{reason}")
        ticket_id = ticket.id
        reply = "你的问题已为你转接人工客服，稍后专人跟进，请稍候。"
        db.add(Message(workspace_id=actor.workspace_id, conversation_id=conv.id,
                       direction="out", content=reply, meta={"mode": "human", "ticket_id": ticket_id}))
    else:
        db.add(Message(workspace_id=actor.workspace_id, conversation_id=conv.id,
                       direction="out", content=reply, meta={"mode": "fallback"}))

    # 回写客户最近触达时间（供 SOP 节奏与评分），并把本次接待写入哈希链审计。
    if customer_id:
        cust = db.get(Customer, customer_id)
        if cust is not None:
            from .models import utcnow
            cust.last_touch_at = utcnow()
    audit_record(
        db,
        workspace_id=actor.workspace_id,
        actor_id=getattr(actor.user, "id", "") or "system",
        action="cs_handle",
        risk_level="R0",
        decision=Decision.ALLOWED,
        resource=f"conversation:{conv.id}",
        reason=f"接待处理：{mode}",
        evidence={"mode": mode, "faq_question": faq.question if faq.matched else "",
                  "ticket_id": ticket_id, "need_human": route.need_human},
        data_tier="INTERNAL",
    )

    db.commit()
    return {
        "mode": mode, "need_human": route.need_human, "reason": route.reason,
        "priority": route.priority, "reply": reply, "ticket_id": ticket_id,
        "faq": {"matched": faq.matched, "question": faq.question},
    }


@app.get("/workspaces/{workspace_id}/faqs")
def list_faqs(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Faq).where(Faq.workspace_id == actor.workspace_id).order_by(Faq.hits.desc())).scalars().all()
    return {"faqs": [
        {"id": f.id, "question": f.question, "answer": f.answer, "tags": f.tags, "hits": f.hits} for f in rows
    ]}


@app.post("/workspaces/{workspace_id}/faqs", status_code=201)
def create_faq(payload: dict = Body(...), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    faq = Faq(
        workspace_id=actor.workspace_id,
        question=str(payload.get("question") or "").strip(),
        answer=str(payload.get("answer") or ""),
        tags=payload.get("tags") or [],
    )
    db.add(faq)
    db.commit()
    db.refresh(faq)
    return {"id": faq.id, "question": faq.question, "answer": faq.answer, "tags": faq.tags, "hits": faq.hits}


@app.delete("/workspaces/{workspace_id}/faqs/{faq_id}")
def delete_faq(faq_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    faq = db.get(Faq, faq_id)
    if faq is None or faq.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "faq not found")
    db.delete(faq)
    db.commit()
    return {"ok": True}


# ===== 可执行 DAG 工作流图（类Coze）：落库 + 拓扑规划 + 只读执行，危险动作只待拍板 =====
def _dag_snapshot(action: str, db: Session, ws: str) -> dict:
    """为 auto 节点采集真实只读计数快照；不写库、不联网。"""
    if action == "leads":
        rows = db.execute(select(Customer).where(Customer.workspace_id == ws)).scalars().all()
        grades = {g: sum(1 for r in rows if getattr(r, "grade", "") == g) for g in ("H", "A", "B", "C")}
        return {"customers": len(rows), "grades": grades}
    if action == "tickets":
        rows = db.execute(select(Ticket).where(Ticket.workspace_id == ws)).scalars().all()
        open_ = [t for t in rows if t.status != "closed"]
        return {"total": len(rows), "open": len(open_),
                "p0p1": sum(1 for t in open_ if t.priority in ("P0", "P1"))}
    if action == "faqs":
        n = len(db.execute(select(Faq).where(Faq.workspace_id == ws)).scalars().all())
        return {"faqs": n}
    if action == "inventory":
        rows = db.execute(select(Product).where(Product.workspace_id == ws)).scalars().all()
        low = sum(1 for r in rows if (r.stock or 0) <= 5)
        return {"products": len(rows), "low_stock": low}
    if action == "orders":
        n = len(db.execute(select(Order).where(Order.workspace_id == ws)).scalars().all())
        return {"orders": n}
    if action == "customers":
        n = len(db.execute(select(Customer).where(Customer.workspace_id == ws)).scalars().all())
        return {"customers": n}
    if action in ("draft_product", "draft_copy"):
        return {"status": "ready_local_draft"}
    return {}


@app.get("/workspaces/{workspace_id}/graphs")
def list_graphs(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(WorkflowGraph).where(WorkflowGraph.workspace_id == actor.workspace_id)
                      .order_by(WorkflowGraph.updated_at.desc())).scalars().all()
    return {"graphs": [
        {"id": g.id, "name": g.name, "status": g.status,
         "nodes": len(g.nodes or []), "edges": len(g.edges or []),
         "updated_at": g.updated_at.isoformat() if g.updated_at else ""} for g in rows
    ]}


@app.post("/workspaces/{workspace_id}/graphs", status_code=201)
def save_graph(workspace_id: str, payload: dict = Body(...), actor: Actor = Depends(require_actor),
               db: Session = Depends(get_db)) -> dict:
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    check = dag_core.validate_graph(nodes, edges)
    if not check["valid"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"detail": "图校验失败", "errors": check["errors"]})
    gid = str(payload.get("id") or "")
    graph = db.get(WorkflowGraph, gid) if gid else None
    if graph is not None and graph.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graph not found")
    if graph is None:
        graph = WorkflowGraph(workspace_id=actor.workspace_id)
        db.add(graph)
    graph.name = str(payload.get("name") or "未命名工作流")[:200]
    graph.nodes = nodes
    graph.edges = edges
    graph.status = str(payload.get("status") or "draft")
    db.commit()
    db.refresh(graph)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
                 action="save_workflow_graph", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"graph:{graph.id}", reason=f"保存工作流图 {graph.name}", data_tier="INTERNAL")
    db.commit()
    return {"id": graph.id, "name": graph.name, "nodes": graph.nodes, "edges": graph.edges,
            "status": graph.status}


@app.get("/workspaces/{workspace_id}/graphs/{graph_id}")
def get_graph(graph_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    g = db.get(WorkflowGraph, graph_id)
    if g is None or g.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graph not found")
    return {"id": g.id, "name": g.name, "status": g.status, "nodes": g.nodes, "edges": g.edges}


@app.delete("/workspaces/{workspace_id}/graphs/{graph_id}")
def delete_graph(graph_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    g = db.get(WorkflowGraph, graph_id)
    if g is None or g.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graph not found")
    db.delete(g)
    db.commit()
    return {"ok": True}


@app.post("/workspaces/{workspace_id}/graphs/{graph_id}/run")
def run_graph(graph_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    g = db.get(WorkflowGraph, graph_id)
    if g is None or g.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graph not found")
    plan = dag_core.plan_run(g.nodes or [], g.edges or [])
    if not plan["ok"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"detail": "图无法运行", "errors": plan["errors"]})
    # 先采集全部安全节点快照，构建上下文供分支求值
    all_results = []
    for step in plan["auto"]:
        snap = _dag_snapshot(step["action"], db, actor.workspace_id)
        all_results.append({**step, "state": "ok", "snapshot": snap})
    context = build_context(all_results)
    # 条件分支求值：每个 branch 按上游快照决定 true/false
    decisions: dict = {}
    for c in plan.get("control", []):
        if c["action"] == "branch":
            cond = (c.get("config") or {}).get("condition") or {}
            decisions[c["id"]] = route_branch(cond, context)
    skipped = pruned_by_branch(g.nodes or [], g.edges or [], decisions)
    results = [r for r in all_results if r["id"] not in skipped]
    skipped_steps = [r for r in all_results if r["id"] in skipped]
    blocked = plan["needs_approval"]
    unknown = plan["unknown"]
    control = plan.get("control", [])
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id,
                 action="run_workflow_graph", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"graph:{g.id}",
                 reason=f"运行工作流图 {g.name}：auto {len(results)} 步，待拍板 {len(blocked)}，路由 {len(control)}，未知 {len(unknown)}",
                 data_tier="INTERNAL")
    db.commit()
    return {"graph_id": g.id, "name": g.name, "order": plan["order"],
            "results": results, "skipped": skipped_steps, "decisions": decisions,
            "needs_approval": blocked, "unknown": unknown,
            "control": control, "layers": layerize(g.nodes or [], g.edges or []),
            "blocked_count": len(blocked)}


@app.get("/workspaces/{workspace_id}/cs/board")
def cs_board(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P2 客服获客看板：本地只读聚合，不调外部、不写库。"""
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    convs = db.execute(select(Conversation).where(Conversation.workspace_id == actor.workspace_id)).scalars().all()
    conv_owner = {c.id: c.customer_id for c in convs}
    msgs: dict[str, list] = {}
    conv_msgs: dict[str, list] = {}
    for m in db.execute(select(Message).where(Message.workspace_id == actor.workspace_id)).scalars().all():
        owner = conv_owner.get(m.conversation_id)
        if owner:
            msgs.setdefault(owner, []).append({"content": m.content, "direction": m.direction})
        conv_msgs.setdefault(m.conversation_id, []).append(m)
    grade_dist = {"H": 0, "A": 0, "B": 0, "C": 0}
    stages = {"new": 0, "lead": 0, "won": 0, "lost": 0}
    for c in customers:
        ls = score_customer(c, msgs.get(c.id))
        grade_dist[ls.grade] = grade_dist.get(ls.grade, 0) + 1
        stages[c.stage if c.stage in stages else "new"] = stages.get(c.stage if c.stage in stages else "new", 0) + 1
    tickets = db.execute(select(Ticket).where(Ticket.workspace_id == actor.workspace_id)).scalars().all()
    by_status: dict = {}
    by_priority = {"P0": 0, "P1": 0, "P2": 0}
    by_type: dict = {}
    closed = 0
    close_durations: list[float] = []
    for t in tickets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
        by_priority[t.priority] = by_priority.get(t.priority, 0) + 1
        by_type[t.type] = by_type.get(t.type, 0) + 1
        if t.status == "closed":
            closed += 1
            if t.closed_at and t.created_at:
                close_durations.append((t.closed_at - t.created_at).total_seconds() / 3600)
    # 首响时长：按会话最早入站到其后首个出站（分钟）；conv_msgs 已在上方一次查询构建
    first_reply: list[float] = []
    for c in convs:
        ms = sorted(conv_msgs.get(c.id, []), key=lambda x: x.created_at)
        inn = next((x for x in ms if (x.direction or "in").lower().startswith("in")), None)
        out = next((x for x in ms if inn and x.created_at > inn.created_at
                    and (x.direction or "").lower().startswith("out")), None)
        if inn and out:
            first_reply.append((out.created_at - inn.created_at).total_seconds() / 60)
    sat = [c.satisfaction for c in customers if c.satisfaction is not None]
    repurchase = [
        {"id": c.id, "name": c.name or c.external_id or c.id,
         "last_touch_at": c.last_touch_at.isoformat() if c.last_touch_at else None}
        for c in customers if c.stage == "won"
    ]
    faqs = db.execute(select(Faq).where(Faq.workspace_id == actor.workspace_id)).scalars().all()
    total = len(tickets)
    from .channel_adapters import all_readiness
    return {
        "leads": {"total": len(customers), "grade_dist": grade_dist},
        "stages": stages,
        "tickets": {"total": total, "by_status": by_status, "by_priority": by_priority,
                    "by_type": by_type, "closed_rate": round(closed / total * 100, 1) if total else 0.0,
                    "avg_close_hours": round(sum(close_durations) / len(close_durations), 2) if close_durations else None},
        "response": {"avg_first_reply_minutes": round(sum(first_reply) / len(first_reply), 1) if first_reply else None},
        "satisfaction": {"avg": round(sum(sat) / len(sat), 2) if sat else None, "count": len(sat)},
        "repurchase": repurchase,
        "faq": {"count": len(faqs), "total_hits": sum(f.hits for f in faqs)},
        "channels": all_readiness(),
    }


@app.get("/workspaces/{workspace_id}/playbooks")
def playbooks(actor: Actor = Depends(require_actor), q: str = "") -> dict:
    """决策预案库只读检索：触发信号/首选方案/B计划/成本/最坏结果/拍板人。"""
    from .playbook import search_playbook, must_escalate
    return {"items": search_playbook(q), "escalate_hits": must_escalate(q)}


# ---- 轨迹自学习（Trace Learning）：从执行历史沉淀经验，老板确认后注入规划 ----
@app.get("/workspaces/{workspace_id}/lessons")
def lessons_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """只读：本店已沉淀的执行经验（模式/成功率/置信度/是否已采纳）。"""
    from .learning import list_lessons
    return {"lessons": list_lessons(db, actor.workspace_id)}


@app.post("/workspaces/{workspace_id}/lessons/learn")
def lessons_learn(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """触发一次轨迹学习：扫描最近执行记录，聚合成功/失败模式，沉淀经验。"""
    from .learning import learn
    return learn(db, actor.workspace_id, limit=200)


@app.post("/workspaces/{workspace_id}/lessons/{lesson_id}/apply")
def lesson_apply(lesson_id: str, actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """老板采纳该经验：应用后进入规划提示词，成为系统记忆。"""
    from .models import Lesson
    row = db.execute(
        select(Lesson).where(
            Lesson.workspace_id == actor.workspace_id, Lesson.id == lesson_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lesson not found")
    row.applied = True
    db.commit()
    return {"id": row.id, "applied": True, "pattern_key": row.pattern_key}


@app.post("/workspaces/{workspace_id}/lessons/{lesson_id}/dismiss")
def lesson_dismiss(lesson_id: str, actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """丢弃该经验（错误/无用样本），同时从提示词注入中移除。"""
    from .models import Lesson
    row = db.execute(
        select(Lesson).where(
            Lesson.workspace_id == actor.workspace_id, Lesson.id == lesson_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lesson not found")
    db.delete(row)
    db.commit()
    return {"id": lesson_id, "deleted": True}


# ---- 配置化规则包（Rule Packs）：.yaml 扔进目录即生效，不改内核 ----
@app.get("/rules")
def rule_packs(actor: Actor = Depends(require_actor)) -> dict:
    """只读：规则包列表（内置/用户、激活状态、规则数）+ 当前生效规则。"""
    from .rulepack import active_rules, list_packs
    return {"packs": list_packs(refresh=True), "active_rules": active_rules()}


@app.post("/rules")
def rule_create(payload: dict = Body(...), actor: Actor = Depends(require_owner)) -> dict:
    """创建用户规则包（写入 app/rules/ 目录，id 须 user_ 开头）。"""
    from .rulepack import save_pack
    try:
        return save_pack(payload)
    except (ValueError, PermissionError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@app.put("/rules/{pack_id}/active")
def rule_toggle(pack_id: str, payload: dict = Body(...), actor: Actor = Depends(require_owner)) -> dict:
    """启用/停用规则包。"""
    from .rulepack import set_active
    try:
        return set_active(pack_id, bool(payload.get("active", True)))
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@app.delete("/rules/{pack_id}")
def rule_delete(pack_id: str, actor: Actor = Depends(require_owner)) -> dict:
    """删除用户规则包（内置包只可停用不可删）。"""
    from .rulepack import delete_pack
    try:
        return delete_pack(pack_id)
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))


# ---- 受控开发 Agent（DevOps）：变更单草案 / 回归门禁 / 老板审批，绝不自动写盘 ----
def _change_dict(c: ChangeRequest) -> dict:
    return {
        "id": c.id, "title": c.title, "requirement": c.requirement,
        "domains": c.domains or [], "change_points": c.change_points or [],
        "impact": c.impact or [], "test_plan": c.test_plan or [],
        "rollback": c.rollback, "draft": c.draft, "risk_level": c.risk_level,
        "status": c.status, "regression_status": c.regression_status,
        "forbidden_hits": c.forbidden_hits or [],
        "requested_by": c.requested_by, "reviewed_by": c.reviewed_by,
        "decision_note": c.decision_note,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
        "applied_at": c.applied_at.isoformat() if c.applied_at else None,
    }


def _load_change(db: Session, cid: str, actor: Actor) -> ChangeRequest:
    c = db.get(ChangeRequest, cid)
    if c is None or c.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "变更单不存在")
    return c


def _devops_audit(db: Session, actor: Actor, action: str, risk_level: str,
                  decision: Decision, resource: str, reason: str,
                  evidence: dict | None = None) -> None:
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action=action, risk_level=risk_level, decision=decision,
                 resource=resource, reason=reason, evidence=evidence or {},
                 data_tier="INTERNAL")


@app.get("/workspaces/{workspace_id}/devops/catalog")
def devops_catalog(actor: Actor = Depends(require_actor)) -> dict:
    """开发 Agent 能力白名单与禁止项（只读）。"""
    from .devops_agent import capabilities_catalog
    return capabilities_catalog()


@app.get("/workspaces/{workspace_id}/devops/regression-summary")
def devops_regression_summary(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """只读：汇总本工作区变更单回归门槛（pass/fail/pending，fail 阻断批准）。"""
    from .devops_agent import regression_gate_summary
    rows = db.execute(
        select(ChangeRequest).where(ChangeRequest.workspace_id == actor.workspace_id)
    ).scalars().all()
    tickets = [{
        "id": r.id, "title": r.title, "status": r.status,
        "regression_status": r.regression_status,
    } for r in rows]
    return regression_gate_summary(tickets)


@app.get("/workspaces/{workspace_id}/devops/backlog")
def devops_backlog(threshold_hours: float = 24.0, actor: Actor = Depends(require_actor),
                   db: Session = Depends(get_db)) -> dict:
    """只读：待批变更单滞留检测（proposed 超阈值标 stale；只标记，不自动催办/发信）。"""
    from .devops_agent import stale_changes, DEFAULT_STALE_HOURS
    rows = db.execute(
        select(ChangeRequest).where(
            ChangeRequest.workspace_id == actor.workspace_id,
            ChangeRequest.status == "proposed",
        )
    ).scalars().all()
    tickets = [{
        "id": r.id, "title": r.title, "status": r.status,
        "regression_status": r.regression_status,
        "submitted_at": r.submitted_at, "created_at": r.created_at,
    } for r in rows]
    return stale_changes(tickets, datetime.utcnow(), threshold_hours or DEFAULT_STALE_HOURS)


@app.post("/workspaces/{workspace_id}/devops/changes")
def devops_create(actor: Actor = Depends(require_actor), db: Session = Depends(get_db),
                  payload: dict = Body(...)) -> dict:
    """提交变更单：自动安全扫描 + 确定性拆单，仅存草案，不写盘、不执行。"""
    from .devops_agent import assess_safety, decompose_requirement, STATUS_DRAFT
    requirement = (payload.get("requirement") or "").strip()
    if not requirement:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "requirement 不能为空")
    draft = payload.get("draft") or ""
    safety = assess_safety(requirement, draft)
    dec = decompose_requirement(requirement)
    c = ChangeRequest(
        workspace_id=actor.workspace_id,
        title=(payload.get("title") or "").strip() or requirement[:40],
        requirement=requirement,
        domains=dec["domains"], change_points=dec["change_points"],
        impact=dec["impact"], test_plan=dec["test_plan"], rollback=dec["rollback"],
        draft=draft, risk_level=safety.risk_level, status=STATUS_DRAFT,
        regression_status="pending", forbidden_hits=safety.forbidden_hits,
        requested_by=actor.user.id or "",
    )
    db.add(c)
    _devops_audit(db, actor, "devops_change_created", safety.risk_level, Decision.ALLOWED,
                  f"change:{c.id}", f"新建变更单：{c.title}",
                  {"domains": dec["domains"], "forbidden_hits": safety.forbidden_hits})
    db.commit()
    db.refresh(c)
    return {"change": _change_dict(c), "safety": safety.as_dict()}


@app.get("/workspaces/{workspace_id}/devops/changes")
def devops_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db),
                status: str = "") -> dict:
    q = select(ChangeRequest).where(ChangeRequest.workspace_id == actor.workspace_id)
    if status:
        q = q.where(ChangeRequest.status == status)
    rows = db.execute(q.order_by(ChangeRequest.created_at.desc())).scalars().all()
    return {"changes": [_change_dict(r) for r in rows]}


@app.get("/workspaces/{workspace_id}/devops/changes/{cid}")
def devops_get(cid: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    return {"change": _change_dict(_load_change(db, cid, actor))}


@app.post("/workspaces/{workspace_id}/devops/changes/{cid}/submit")
def devops_submit(cid: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """draft -> proposed：提交进入审批队列（不落地）。"""
    from .devops_agent import STATUS_PROPOSED, can_transition
    c = _load_change(db, cid, actor)
    if not can_transition(c.status, STATUS_PROPOSED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"当前状态 {c.status} 不能提交")
    c.status = STATUS_PROPOSED
    if c.submitted_at is None:
        c.submitted_at = datetime.utcnow()
    _devops_audit(db, actor, "devops_change_submit", "R1", Decision.ALLOWED,
                  f"change:{c.id}", f"提交变更单进入审批：{c.title}")
    db.commit()
    return {"change": _change_dict(c)}


@app.post("/workspaces/{workspace_id}/devops/changes/{cid}/regression")
def devops_regression(cid: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db),
                      payload: dict = Body(...)) -> dict:
    """登记回归结果（实际回归由人/CI 跑，此处只记录 pass/fail，不自动执行）。"""
    c = _load_change(db, cid, actor)
    rs = payload.get("status")
    if rs not in ("pass", "fail", "pending"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "regression status 只能是 pass/fail/pending")
    c.regression_status = rs
    _devops_audit(db, actor, "devops_regression_recorded", "R1", Decision.ALLOWED,
                  f"change:{c.id}", f"登记回归结果：{rs}", {"regression_status": rs})
    db.commit()
    return {"change": _change_dict(c)}


@app.post("/workspaces/{workspace_id}/devops/changes/{cid}/approve")
def devops_approve(cid: str, actor: Actor = Depends(require_owner), db: Session = Depends(get_db),
                   payload: dict = Body(default={})) -> dict:
    """老板审批：回归 pass 可批；fail 禁止；pending 需显式豁免。批准只记录，不写盘。"""
    from .devops_agent import STATUS_APPROVED, can_approve, can_transition
    c = _load_change(db, cid, actor)
    ok, reason = can_approve(c.regression_status, bool(payload.get("waive")), payload.get("waive_note") or "")
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    if not can_transition(c.status, STATUS_APPROVED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"当前状态 {c.status} 不能批准")
    c.status = STATUS_APPROVED
    c.reviewed_by = actor.user.id or ""
    c.decision_note = payload.get("note") or reason
    c.decided_at = datetime.utcnow()
    _devops_audit(db, actor, "devops_change_approved", "R3", Decision.ALLOWED,
                  f"change:{c.id}", f"老板批准变更单：{c.title}",
                  {"regression_status": c.regression_status, "note": c.decision_note})
    db.commit()
    return {"change": _change_dict(c)}


@app.post("/workspaces/{workspace_id}/devops/changes/{cid}/reject")
def devops_reject(cid: str, actor: Actor = Depends(require_owner), db: Session = Depends(get_db),
                  payload: dict = Body(default={})) -> dict:
    from .devops_agent import STATUS_REJECTED, can_transition
    c = _load_change(db, cid, actor)
    if not can_transition(c.status, STATUS_REJECTED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"当前状态 {c.status} 不能拒绝")
    c.status = STATUS_REJECTED
    c.reviewed_by = actor.user.id or ""
    c.decision_note = payload.get("note") or "已拒绝"
    c.decided_at = datetime.utcnow()
    _devops_audit(db, actor, "devops_change_rejected", "R3", Decision.DENIED,
                  f"change:{c.id}", f"老板拒绝变更单：{c.title}",
                  {"note": c.decision_note})
    db.commit()
    return {"change": _change_dict(c)}


@app.post("/workspaces/{workspace_id}/devops/changes/{cid}/mark-applied")
def devops_mark_applied(cid: str, actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """人工受控落盘后，由老板手动标记 applied（系统不代为写盘）。"""
    from .devops_agent import STATUS_APPLIED, STATUS_APPROVED, can_transition
    c = _load_change(db, cid, actor)
    if c.status != STATUS_APPROVED or not can_transition(STATUS_APPROVED, STATUS_APPLIED):
        raise HTTPException(status.HTTP_409_CONFLICT, "仅已批准变更可标记已落地")
    c.status = STATUS_APPLIED
    c.applied_at = datetime.utcnow()
    _devops_audit(db, actor, "devops_change_marked_applied", "R1", Decision.ALLOWED,
                  f"change:{c.id}", f"人工受控落盘后标记已落地：{c.title}")
    db.commit()
    return {"change": _change_dict(c)}


@app.post("/workspaces/{workspace_id}/cs/sop/dispatch")
def sop_dispatch(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P0-2 SOP 自动派单：为 urgent/due 的未成交线索幂等派生跟进待办（同客户已有 pending 待办则跳过）。"""
    now = datetime.utcnow()
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    convs = db.execute(select(Conversation).where(Conversation.workspace_id == actor.workspace_id)).scalars().all()
    conv_owner = {c.id: c.customer_id for c in convs}
    msgs: dict[str, list] = {}
    for m in db.execute(select(Message).where(Message.workspace_id == actor.workspace_id)).scalars().all():
        owner = conv_owner.get(m.conversation_id)
        if owner:
            msgs.setdefault(owner, []).append({"content": m.content, "direction": m.direction})

    created: list[dict] = []
    for c in customers:
        if c.stage == "won":
            continue
        ls = score_customer(c, msgs.get(c.id))
        na = sop_next_action(ls.grade, c.stage, c.last_touch_at, c.next_follow_at, now)
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
        created.append({"todo_id": todo.id, "customer_id": c.id, "grade": ls.grade,
                        "urgency": na.urgency, "action": na.action})
    db.commit()
    return {"dispatched": len(created), "items": created}


@app.get("/workspaces/{workspace_id}/cs/learn")
def cs_learn(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P2 复盘：未被 FAQ 命中、重复出现的入站问题，给沉淀 FAQ 的建议（只读，不自动写）。"""
    from collections import Counter
    faq_rows = [{"question": f.question, "answer": f.answer, "tags": f.tags}
                for f in db.execute(select(Faq).where(Faq.workspace_id == actor.workspace_id)).scalars().all()]
    cnt: Counter = Counter()
    samples: dict[str, str] = {}
    for m in db.execute(select(Message).where(
            Message.workspace_id == actor.workspace_id, Message.direction == "in")).scalars().all():
        txt = (m.content or "").strip()
        if not txt or match_faq(txt, faq_rows).matched:
            continue
        norm = "".join(ch for ch in txt.lower() if ch.isalnum())
        if not norm:
            continue
        cnt[norm] += 1
        samples.setdefault(norm, txt)
    candidates = [{"question": samples[k], "count": v} for k, v in cnt.most_common(20) if v >= 2]
    return {"candidates": candidates}


@app.post("/workspaces/{workspace_id}/customers/{customer_id}/satisfaction")
def set_satisfaction(customer_id: str, payload: dict = Body(...),
                     actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """P2 满意度录入：1-5 分写回客户，供看板均值聚合。"""
    customer = db.get(Customer, customer_id)
    if customer is None or customer.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found")
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "score must be a number")
    if not 1 <= score <= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "score must be between 1 and 5")
    customer.satisfaction = score
    db.commit()
    return {"ok": True, "satisfaction": score}


@app.get("/workspaces/{workspace_id}/customers/{customer_id}")
def get_customer(customer_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found")
    conversations = db.execute(
        select(Conversation).where(Conversation.customer_id == customer.id).order_by(Conversation.updated_at.desc())
    ).scalars().all()
    memory = []
    for conv in conversations:
        messages = db.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at.desc()).limit(20)
        ).scalars().all()
        memory.append(
            {
                "conversation_id": conv.id,
                "state": conv.state,
                "summary": conv.summary,
                "messages": [{"direction": m.direction, "content": m.content, "created_at": m.created_at.isoformat()} for m in reversed(messages)],
            }
        )
    return {
        "id": customer.id,
        "channel": customer.channel,
        "external_id": customer.external_id,
        "name": customer.name,
        "profile": customer.profile,
        "conversations": memory,
    }


# --- AI 专属虚拟号映射（占位；真实转接走运营商隐私号服务，外部依赖） ---
@app.get("/workspaces/{workspace_id}/virtual-numbers")
def list_virtual_numbers(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .virtual_numbers import list_numbers
    return {"numbers": list_numbers(db, actor)}


@app.post("/workspaces/{workspace_id}/virtual-numbers", status_code=201)
def allocate_virtual_number(
    payload: dict = Body(default={}),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .virtual_numbers import VirtualNumberError, allocate
    try:
        rec = allocate(
            db, actor,
            real_encrypted=payload.get("real_encrypted", ""),
            customer_id=payload.get("customer_id", ""),
        )
        db.commit()
        db.refresh(rec)
    except VirtualNumberError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"id": rec.id, "virtual_number": rec.virtual_number, "status": rec.status,
            "bound_customer_id": rec.bound_customer_id, "has_real": bool(rec.real_encrypted)}


@app.post("/workspaces/{workspace_id}/virtual-numbers/bind")
def bind_virtual_number(
    payload: dict = Body(...),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .virtual_numbers import VirtualNumberError, bind
    try:
        rec = bind(
            db, actor,
            virtual_number=payload["virtual_number"],
            customer_id=payload["customer_id"],
            real_encrypted=payload.get("real_encrypted", ""),
        )
        db.commit()
    except VirtualNumberError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"virtual_number": rec.virtual_number, "status": rec.status,
            "bound_customer_id": rec.bound_customer_id, "has_real": bool(rec.real_encrypted)}


@app.post("/workspaces/{workspace_id}/virtual-numbers/release")
def release_virtual_number(
    payload: dict = Body(...),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .virtual_numbers import VirtualNumberError, release
    try:
        rec = release(db, actor, virtual_number=payload["virtual_number"])
        db.commit()
    except VirtualNumberError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"virtual_number": rec.virtual_number, "status": rec.status}


# --- Owner-only E2EE vault: server stores ciphertext, never decrypts ---
@app.put("/workspaces/{workspace_id}/e2ee")
def e2ee_store(
    payload: dict = Body(...),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .e2ee_vault import VaultError, envelope, store
    try:
        row = store(
            db, actor,
            owner_ref=payload.get("owner_ref", ""),
            field=payload.get("field", ""),
            ciphertext=payload.get("ciphertext", ""),
            nonce=payload.get("nonce", ""),
            alg=payload.get("alg", "aes-gcm"),
            blind_index=payload.get("blind_index", ""),
        )
        db.commit()
    except VaultError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return envelope(row)


@app.get("/workspaces/{workspace_id}/e2ee")
def e2ee_fetch(
    owner_ref: str, field: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .e2ee_vault import envelope, fetch
    row = fetch(db, actor, owner_ref=owner_ref, field=field)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "encrypted field not found")
    return envelope(row)


@app.get("/workspaces/{workspace_id}/e2ee/find")
def e2ee_find(
    field: str, blind_index: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .e2ee_vault import VaultError, find_by_blind_index
    try:
        rows = find_by_blind_index(db, actor, field=field, blind_index=blind_index)
    except VaultError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"matches": [{"owner_ref": r.owner_ref, "field": r.field} for r in rows]}


@app.get("/workspaces/{workspace_id}/e2ee/list")
def e2ee_list(
    owner_ref: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """List stored encrypted fields for an owner_ref without leaking ciphertext."""
    from .e2ee_vault import list_fields
    return {"owner_ref": owner_ref, "fields": list_fields(db, actor, owner_ref=owner_ref)}


@app.delete("/workspaces/{workspace_id}/e2ee")
def e2ee_delete(
    owner_ref: str, field: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """Erase one ciphertext envelope (owner_ref + field)."""
    from .e2ee_vault import delete
    removed = delete(db, actor, owner_ref=owner_ref, field=field)
    db.commit()
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "encrypted field not found")
    return {"deleted": True, "owner_ref": owner_ref, "field": field}


@app.delete("/workspaces/{workspace_id}/e2ee/owner")
def e2ee_delete_owner(
    owner_ref: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """Erase every encrypted field for one owner_ref (data minimization)."""
    from .e2ee_vault import delete_owner
    removed = delete_owner(db, actor, owner_ref=owner_ref)
    db.commit()
    return {"deleted": removed, "owner_ref": owner_ref}


# --- Device pairing registration for multi-device key sync ---
@app.post("/workspaces/{workspace_id}/devices/request", status_code=201)
def devices_request(
    payload: dict = Body(...),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .device_pairing import PairingError, request_pairing
    try:
        row = request_pairing(db, actor, device_name=payload.get("device_name", ""))
        db.commit()
    except PairingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"id": row.id, "pair_code": row.pair_code, "status": row.status}


@app.post("/workspaces/{workspace_id}/devices/authorize")
def devices_authorize(
    payload: dict = Body(...),
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .device_pairing import PairingError, authorize
    try:
        row = authorize(
            db, actor,
            pair_code=payload.get("pair_code", ""),
            device_name=payload.get("device_name", ""),
            credential_handle=payload.get("credential_handle", ""),
        )
        db.commit()
    except PairingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"id": row.id, "device_name": row.device_name, "status": row.status}


@app.post("/workspaces/{workspace_id}/devices/{pairing_id}/revoke")
def devices_revoke(
    pairing_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from .device_pairing import PairingError, revoke
    try:
        row = revoke(db, actor, pairing_id=pairing_id)
        db.commit()
    except PairingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"id": row.id, "status": row.status}


@app.get("/workspaces/{workspace_id}/devices")
def devices_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .device_pairing import list_devices
    return {"devices": list_devices(db, actor)}


@app.get("/workspaces/{workspace_id}/tools")
def list_tools(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    # Unified tool discovery: built-ins + this workspace's enabled functions,
    # same catalog as the MCP surface; credentials are never advertised.
    from .mcp_server import list_tools as mcp_list_tools

    tools = []
    for t in mcp_list_tools(db, actor):
        meta = t.get("_meta", {})
        tools.append({
            "name": t["name"],
            "description": t["description"],
            "risk_level": meta.get("risk_level"),
            "side_effect": meta.get("side_effect"),
            "idempotent": meta.get("idempotent"),
            "source": meta.get("source"),
            "kind": meta.get("kind"),
        })
    return {"tools": tools}


@app.get("/roles")
def list_team_roles() -> dict:
    from .roles import catalog

    return {"roles": catalog()}


@app.post("/workspaces/{workspace_id}/connectors", status_code=201)
def add_connector(payload: schemas.ConnectorInstall, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .connectors import encrypt_config_secrets, install_connector

    instance = install_connector(
        db, actor.workspace_id, payload.connector_type, payload.name, encrypt_config_secrets(payload.config)
    )
    db.commit()
    return {"id": instance.id, "connector_type": instance.connector_type, "name": instance.name}


@app.get("/workspaces/{workspace_id}/connectors")
def list_connectors(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ConnectorInstance).where(ConnectorInstance.workspace_id == actor.workspace_id)
    ).scalars().all()
    return {
        "connectors": [
            {
                "id": c.id,
                "connector_type": c.connector_type,
                "name": c.name,
                "status": c.status,
                "endpoint_url": (c.config or {}).get("endpoint_url") or "",
                "method": (c.config or {}).get("method") or "GET",
                "auth_type": ((c.config or {}).get("auth") or {}).get("type") or "none",
                "configured": bool((c.config or {}).get("endpoint_url") or (c.config or {}).get("auth") or (c.config or {}).get("signer")),
                "allow_private": bool((c.config or {}).get("allow_private")),
                "signer": (c.config or {}).get("signer") or "",
                "has_secret": bool((c.config or {}).get("app_secret_enc") or (c.config or {}).get("checkword_enc") or (c.config or {}).get("access_token_enc")),
                "app_key": (c.config or {}).get("app_key") or "",
                "app_id": (c.config or {}).get("app_id") or "",
                "partner_id": (c.config or {}).get("partner_id") or "",
                "api_method": (c.config or {}).get("api_method") or "",
                "sign_method": (c.config or {}).get("sign_method") or "",
                "service_code": (c.config or {}).get("service_code") or "",
                "actions": (c.config or {}).get("actions") or {},
            }
            for c in rows
        ]
    }


@app.put("/workspaces/{workspace_id}/connectors/{connector_type}")
def configure_connector(connector_type: str, payload: schemas.ConnectorConfig, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    instance = db.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.workspace_id == actor.workspace_id,
            ConnectorInstance.connector_type == connector_type,
        )
    ).scalars().first()
    if instance is None:
        instance = ConnectorInstance(
            workspace_id=actor.workspace_id,
            connector_type=connector_type,
            name=payload.name or connector_type,
            config={},
            status="active",
        )
        db.add(instance)
        db.flush()

    config: dict = dict(instance.config or {})
    if payload.name is not None:
        instance.name = payload.name
    if payload.endpoint_url is not None:
        config["endpoint_url"] = payload.endpoint_url.strip()
    if payload.method is not None:
        config["method"] = payload.method.upper()
    if payload.auth is not None:
        config["auth"] = payload.auth
    if payload.allow_private:
        config["allow_private"] = True
    else:
        config.pop("allow_private", None)
    for key in ("signer", "app_key", "app_id", "partner_id", "api_method", "sign_method", "service_code"):
        if getattr(payload, key) is not None:
            config[key] = getattr(payload, key)
    for key in ("actions", "verification"):
        if getattr(payload, key) is not None:
            config[key] = getattr(payload, key)
    from .connectors import encrypt_config_secrets

    secrets = {key: getattr(payload, key) for key in ("app_secret", "checkword", "access_token")}
    config = encrypt_config_secrets({**config, **{k: v for k, v in secrets.items() if v is not None}})
    instance.config = config
    db.commit()
    db.refresh(instance)
    return {
        "id": instance.id,
        "connector_type": instance.connector_type,
        "name": instance.name,
        "status": instance.status,
        "endpoint_url": (instance.config or {}).get("endpoint_url") or "",
        "method": (instance.config or {}).get("method") or "GET",
        "auth_type": ((instance.config or {}).get("auth") or {}).get("type") or "none",
        "allow_private": bool((instance.config or {}).get("allow_private")),
        "signer": (instance.config or {}).get("signer") or "",
        "has_secret": bool((instance.config or {}).get("app_secret_enc") or (instance.config or {}).get("checkword_enc") or (instance.config or {}).get("access_token_enc")),
    }


def _function_out(fn: WorkspaceFunction) -> dict:
    from .functions import effective_risk

    return {
        "id": fn.id,
        "name": fn.name,
        "description": fn.description,
        "kind": fn.kind,
        "category": fn.category,
        "endpoint_url": fn.endpoint_url,
        "method": fn.method,
        "risk_level": fn.risk_level,
        "effective_risk": effective_risk(fn).value,
        "side_effect": fn.side_effect,
        "idempotent": fn.idempotent,
        "enabled": fn.enabled,
        "stage": fn.stage,
        "parent_area_id": fn.parent_area_id,
        "display_name": (fn.config or {}).get("display_name") or fn.name,
        "acceptance": (fn.config or {}).get("acceptance") or "",
        "verification": fn.verification,
        "config": fn.config,
    }


@app.post("/workspaces/{workspace_id}/functions", status_code=201)
def add_function(payload: schemas.FunctionCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .functions import validate_name

    name = validate_name(payload.name)
    if registry.get(name) is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"function name conflicts with built-in tool: {name}")
    exists = db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == actor.workspace_id,
            WorkspaceFunction.name == name,
        )
    ).scalars().first()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "function name already exists in this workspace")

    fn = WorkspaceFunction(
        workspace_id=actor.workspace_id,
        name=name,
        description=payload.description,
        kind=payload.kind,
        category=payload.category,
        endpoint_url=payload.endpoint_url,
        method=payload.method.upper(),
        headers=payload.headers,
        auth=payload.auth,
        input_schema=payload.input_schema,
        risk_level=payload.risk_level or "R0",
        side_effect=payload.side_effect,
        idempotent=payload.idempotent,
        enabled=payload.enabled,
        verification=payload.verification,
        config=payload.config,
    )
    db.add(fn)
    db.commit()
    db.refresh(fn)
    return _function_out(fn)


@app.get("/workspaces/{workspace_id}/functions")
def list_functions(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(WorkspaceFunction).where(WorkspaceFunction.workspace_id == actor.workspace_id).order_by(WorkspaceFunction.name)
    ).scalars().all()
    return {"functions": [_function_out(fn) for fn in rows]}


@app.get("/workspaces/{workspace_id}/functions/{function_id}")
def get_function(function_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    fn = db.get(WorkspaceFunction, function_id)
    if fn is None or fn.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "function not found")
    return _function_out(fn)


@app.patch("/workspaces/{workspace_id}/functions/{function_id}")
def update_function(function_id: str, payload: schemas.FunctionUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    fn = db.get(WorkspaceFunction, function_id)
    if fn is None or fn.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "function not found")
    changes = payload.model_dump(exclude_unset=True)
    if "method" in changes and changes["method"]:
        changes["method"] = changes["method"].upper()
    for key, value in changes.items():
        setattr(fn, key, value)
    db.commit()
    db.refresh(fn)
    return _function_out(fn)


@app.delete("/workspaces/{workspace_id}/functions/{function_id}", status_code=204)
def delete_function(function_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    fn = db.get(WorkspaceFunction, function_id)
    if fn is None or fn.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "function not found")
    db.delete(fn)
    db.commit()


def _module_out(m: Module) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "kind": m.kind,
        "category": m.category,
        "endpoint_url": m.endpoint_url,
        "method": m.method,
        "risk_level": m.risk_level,
        "min_role": m.min_role,
        "visibility": m.visibility,
        "status": m.status,
        "version": m.version,
        "author_workspace_id": m.author_workspace_id,
        "verification": m.verification,
        "config": m.config,
    }


@app.post("/workspaces/{workspace_id}/modules", status_code=201)
def add_module(payload: schemas.ModuleCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service
    from .functions import validate_name

    validate_name(payload.name)
    try:
        module = module_service.create_module(db, actor, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    db.refresh(module)
    return _module_out(module)


@app.get("/workspaces/{workspace_id}/modules")
def list_modules(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Module).where(Module.author_workspace_id == actor.workspace_id).order_by(Module.updated_at.desc())
    ).scalars().all()
    return {"modules": [_module_out(m) for m in rows]}


@app.get("/workspaces/{workspace_id}/modules/{module_id}")
def get_module(module_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    return _module_out(module)


@app.patch("/workspaces/{workspace_id}/modules/{module_id}")
def update_module(module_id: str, payload: schemas.ModuleUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service

    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    changes = payload.model_dump(exclude_unset=True)
    publishing = changes.pop("visibility", None) == "published" or changes.pop("status", None) == "published"
    try:
        module_service._apply_fields(module, changes)
        module_service.validate_module_config(
            kind=module.kind,
            endpoint_url=module.endpoint_url,
            method=module.method,
            config=module.config,
            input_schema=module.input_schema,
            auth=module.auth,
            headers=module.headers,
        )
        if publishing:
            module_service.publish_module(db, actor, module)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    db.refresh(module)
    return _module_out(module)


@app.post("/workspaces/{workspace_id}/modules/{module_id}/submit-review")
def submit_module_review(module_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service

    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    try:
        module_service.submit_for_review(db, actor, module)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    db.refresh(module)
    return _module_out(module)


@app.post("/workspaces/{workspace_id}/modules/{module_id}/review")
def review_module_route(
    module_id: str,
    payload: schemas.ModuleReview,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    from . import modules as module_service

    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    try:
        module_service.review_module(db, actor, module, payload.decision, payload.note)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(module)
    return _module_out(module)


@app.post("/workspaces/{workspace_id}/modules/{module_id}/publish")
def publish_approved_module(module_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service

    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    try:
        module_service.publish_approved(db, actor, module)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(module)
    return _module_out(module)


@app.delete("/workspaces/{workspace_id}/modules/{module_id}", status_code=204)
def delete_module(module_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    module = db.get(Module, module_id)
    if module is None or module.author_workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "module not found")
    db.delete(module)
    db.commit()


VALID_AGENT_STATUS = {"draft", "test", "live", "disabled"}


def _agent_out(agent: AIAgent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "description": agent.description,
        "responsibilities": agent.responsibilities or [],
        "allowed_tools": agent.allowed_tools or [],
        "memory_policy": agent.memory_policy or {"scope": "workspace", "enabled": True},
        "execution_limits": agent.execution_limits or {"max_runtime_seconds": 300, "max_tool_calls": 20},
        "status": agent.status,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


@app.post("/workspaces/{workspace_id}/agents", status_code=201)
def add_agent(payload: schemas.AgentCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .tools import registry

    status_value = payload.status or "draft"
    if status_value not in VALID_AGENT_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid agent status")
    known_tools = {tool.name for tool in registry.all()}
    for tool in payload.allowed_tools or []:
        if tool not in known_tools:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown tool: {tool}")

    agent = AIAgent(
        workspace_id=actor.workspace_id,
        name=payload.name,
        role=payload.role or "store_manager",
        description=payload.description or "",
        responsibilities=payload.responsibilities or [],
        allowed_tools=payload.allowed_tools or [],
        memory_policy=payload.memory_policy or {"scope": "workspace", "enabled": True},
        execution_limits=payload.execution_limits or {"max_runtime_seconds": 300, "max_tool_calls": 20},
        status=status_value,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return _agent_out(agent)


@app.get("/workspaces/{workspace_id}/agents")
def list_agents(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(AIAgent).where(AIAgent.workspace_id == actor.workspace_id).order_by(AIAgent.updated_at.desc())
    ).scalars().all()
    return {"agents": [_agent_out(a) for a in rows]}


@app.get("/workspaces/{workspace_id}/agents/{agent_id}")
def get_agent(agent_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    agent = db.get(AIAgent, agent_id)
    if agent is None or agent.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return _agent_out(agent)


@app.patch("/workspaces/{workspace_id}/agents/{agent_id}")
def update_agent(agent_id: str, payload: schemas.AgentUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .tools import registry

    agent = db.get(AIAgent, agent_id)
    if agent is None or agent.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] not in VALID_AGENT_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid agent status")
    if changes.get("allowed_tools") is not None:
        known_tools = {tool.name for tool in registry.all()}
        for tool in changes["allowed_tools"]:
            if tool not in known_tools:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown tool: {tool}")
    for key, value in changes.items():
        setattr(agent, key, value)
    db.commit()
    db.refresh(agent)
    return _agent_out(agent)


@app.delete("/workspaces/{workspace_id}/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    agent = db.get(AIAgent, agent_id)
    if agent is None or agent.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    db.delete(agent)
    db.commit()


@app.post("/workspaces/{workspace_id}/agents/{agent_id}/run")
async def run_agent(
    agent_id: str,
    payload: schemas.AgentRun | None = None,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """Manually trigger one read-only patrol pass for a live/test manager."""
    agent = db.get(AIAgent, agent_id)
    if agent is None or agent.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    from .agent_runtime import AgentRunError, run_agent_once

    steps = [s.model_dump() for s in payload.steps] if payload and payload.steps else None
    try:
        return await run_agent_once(db, actor, agent, steps=steps)
    except AgentRunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


def _job_out(job: ScheduledJob) -> dict:
    return {
        "id": job.id,
        "name": job.name,
        "trigger": job.trigger or {},
        "goal": job.goal,
        "steps": job.steps or [],
        "enabled": job.enabled,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "created_at": job.created_at.isoformat(),
    }


@app.post("/workspaces/{workspace_id}/jobs", status_code=201)
def add_job(payload: schemas.JobCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .models import utcnow
    from datetime import datetime, timedelta

    trigger = payload.trigger or {"type": "interval", "minutes": 60}
    ttype = trigger.get("type") or "interval"
    if ttype == "once":
        # One-shot: resolve the ISO ``at`` into next_run_at; fall back to now
        # (fires on the next sweep) when unparseable rather than crashing.
        at_raw = trigger.get("at")
        try:
            next_run = datetime.fromisoformat(str(at_raw)).replace(tzinfo=None)
        except (TypeError, ValueError):
            next_run = utcnow().replace(tzinfo=None)
    elif ttype == "event":
        # Event jobs are not time-based; a far-future placeholder keeps the
        # row valid, and the time sweep skips type=event anyway.
        next_run = utcnow() + timedelta(days=3650)
    else:
        minutes = int(trigger.get("minutes") or 60)
        next_run = utcnow() + timedelta(minutes=minutes)
    job = ScheduledJob(
        workspace_id=actor.workspace_id,
        name=payload.name,
        trigger=trigger,
        goal=payload.goal or "",
        steps=payload.steps or [],
        enabled=payload.enabled,
        next_run_at=next_run,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_out(job)


@app.post("/workspaces/{workspace_id}/events/{event_name}")
async def fire_event(event_name: str, payload: dict = Body(default={}), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """Dispatch an in-app event to matching ``event``-type jobs (gateway-backed)."""
    from .scheduler import dispatch_event

    results = await dispatch_event(db, actor, event_name, payload)
    return {"event": event_name, "fired": results}


@app.post("/workspaces/{workspace_id}/bus/resolve")
def bus_resolve(payload: dict = Body(default={}), actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """Declarative read-only panel aggregation across sources (M03 data bus)."""
    from .data_bus import resolve as bus_resolve

    return bus_resolve(db, actor.workspace_id, payload)


@app.get("/workspaces/{workspace_id}/jobs")
def list_jobs(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == actor.workspace_id).order_by(ScheduledJob.created_at.desc())
    ).scalars().all()
    return {"jobs": [_job_out(j) for j in rows]}


@app.patch("/workspaces/{workspace_id}/jobs/{job_id}")
def update_job(job_id: str, payload: schemas.JobUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    job = db.get(ScheduledJob, job_id)
    if job is None or job.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(job, key, value)
    db.commit()
    db.refresh(job)
    return _job_out(job)


@app.delete("/workspaces/{workspace_id}/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    job = db.get(ScheduledJob, job_id)
    if job is None or job.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    db.delete(job)
    db.commit()


@app.get("/marketplace")
def browse_marketplace(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service

    return {"modules": [_module_out(m) for m in module_service.list_marketplace(db, actor)]}


@app.post("/marketplace/{module_id}/install", status_code=201)
def install_from_marketplace(module_id: str, payload: schemas.ModuleInstall, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from . import modules as module_service

    module = db.get(Module, module_id)
    if module is None or module.visibility != "published" or module.status != "published":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "published module not found")
    try:
        function = module_service.install_module(db, actor, module, payload)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(function)
    return {"id": function.id, "name": function.name, "kind": function.kind}


@app.get("/workspaces/{workspace_id}/layout")
def get_layout(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(LayoutArea).where(LayoutArea.workspace_id == actor.workspace_id).order_by(LayoutArea.sort_order)
    ).scalars().all()
    return {
        "areas": [
            {"id": a.id, "name": a.name, "kind": a.kind, "target_type": a.target_type, "target_id": a.target_id, "parent_id": a.parent_id, "config": a.config, "sort_order": a.sort_order, "enabled": a.enabled, "stage": a.stage}
            for a in rows
        ]
    }


@app.post("/workspaces/{workspace_id}/layout", status_code=201)
def upsert_layout_area(payload: schemas.LayoutAreaUpsert, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    area = LayoutArea(
        workspace_id=actor.workspace_id,
        name=payload.name,
        kind=payload.kind,
        target_type=payload.target_type,
        target_id=payload.target_id,
        parent_id=payload.parent_id,
        config=payload.config,
        sort_order=payload.sort_order,
        enabled=payload.enabled,
        stage=payload.stage,
    )
    db.add(area)
    db.commit()
    db.refresh(area)
    return {"id": area.id, "name": area.name}


def _layout_out(area: LayoutArea) -> dict:
    return {
        "id": area.id,
        "name": area.name,
        "kind": area.kind,
        "target_type": area.target_type,
        "target_id": area.target_id,
        "parent_id": area.parent_id,
        "config": area.config,
        "sort_order": area.sort_order,
        "enabled": area.enabled,
        "stage": area.stage,
    }


@app.patch("/workspaces/{workspace_id}/layout/{area_id}")
def update_layout_area(area_id: str, payload: schemas.LayoutAreaUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    area = db.get(LayoutArea, area_id)
    if area is None or area.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "layout area not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(area, key, value)
    if area.stage == "live" and area.target_type == "function" and area.target_id:
        fn = db.get(WorkspaceFunction, area.target_id)
        if fn is not None and fn.workspace_id == actor.workspace_id:
            fn.stage = "live"
    db.commit()
    db.refresh(area)
    return _layout_out(area)


@app.delete("/workspaces/{workspace_id}/layout/{area_id}", status_code=204)
def delete_layout_area(area_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    area = db.get(LayoutArea, area_id)
    if area is None or area.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "layout area not found")
    db.delete(area)
    db.commit()


@app.post("/workspaces/{workspace_id}/layout/reorder")
def reorder_layout(payload: schemas.LayoutAreaReorder, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    areas = db.execute(
        select(LayoutArea).where(LayoutArea.workspace_id == actor.workspace_id)
    ).scalars().all()
    by_id = {a.id: a for a in areas}
    ordered = []
    for index, area_id in enumerate(payload.area_ids):
        area = by_id.get(area_id)
        if area is not None:
            area.sort_order = index
            ordered.append(area)
    db.commit()
    return {"areas": [_layout_out(a) for a in sorted(ordered, key=lambda a: a.sort_order)]}


@app.get("/workspaces/{workspace_id}/approvals")
def list_approvals(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.workspace_id == actor.workspace_id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(100)
    ).scalars().all()
    return {
        "approvals": [
            {
                "id": r.id,
                "action": r.action,
                "risk_level": r.risk_level,
                "status": r.status,
                "reason": r.reason,
                "payload": r.payload,
                "confirm_code": r.confirm_code,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


def _todo_out(t: Todo) -> dict:
    return {
        "id": t.id,
        "kind": t.kind,
        "title": t.title,
        "detail": t.detail,
        "status": t.status,
        "confirm_code": t.confirm_code,
        "ref_type": t.ref_type,
        "ref_id": t.ref_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/workspaces/{workspace_id}/todos")
def list_todos(kind: str | None = None, status: str | None = "pending", actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    query = select(Todo).where(Todo.workspace_id == actor.workspace_id)
    if kind:
        query = query.where(Todo.kind == kind)
    if status:
        query = query.where(Todo.status == status)
    rows = db.execute(query.order_by(Todo.created_at.desc())).scalars().all()
    return {"todos": [_todo_out(t) for t in rows]}


@app.post("/workspaces/{workspace_id}/todos", status_code=201)
def create_todo(payload: schemas.TodoCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    kind = payload.kind if payload.kind in ("must_confirm", "needs_reply", "info") else "info"
    confirm_code = ""
    if kind == "must_confirm":
        from .notify import generate_code, notify_owner_for_workspace

        confirm_code = payload.confirm_code.strip() or generate_code()
        notify_owner_for_workspace(
            db,
            actor.workspace_id,
            f"[Krama] 待确认：{payload.title}",
            f"{payload.detail}\n确认码：{confirm_code}",
        )
    todo = Todo(
        workspace_id=actor.workspace_id,
        kind=kind,
        title=payload.title,
        detail=payload.detail,
        status="pending",
        confirm_code=confirm_code,
        ref_type=payload.ref_type,
        ref_id=payload.ref_id,
    )
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return _todo_out(todo)


@app.post("/workspaces/{workspace_id}/todos/{todo_id}/confirm")
def confirm_todo(todo_id: str, payload: schemas.TodoConfirm, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    todo = db.get(Todo, todo_id)
    if todo is None or todo.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "todo not found")
    if todo.status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"todo already {todo.status}")
    if todo.kind != "must_confirm":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only must_confirm items require a code")
    if not todo.confirm_code or not secrets.compare_digest(todo.confirm_code, payload.code.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "确认码错误")
    todo.status = "done"
    if todo.ref_type == "approval" and todo.ref_id:
        try:
            approve(db, actor, todo.ref_id)
        except GatewayError:
            pass
    db.commit()
    db.refresh(todo)
    return _todo_out(todo)


@app.post("/workspaces/{workspace_id}/todos/{todo_id}/status")
def update_todo_status(todo_id: str, payload: schemas.TodoUpdateStatus, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    todo = db.get(Todo, todo_id)
    if todo is None or todo.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "todo not found")
    new_status = payload.status if payload.status in ("done", "cancelled", "pending") else "done"
    if new_status in ("done", "cancelled") and todo.ref_type == "approval" and todo.ref_id:
        try:
            approve(db, actor, todo.ref_id) if new_status == "done" else reject(db, actor, todo.ref_id)
        except GatewayError:
            pass
        todo.status = new_status
    else:
        todo.status = new_status
    db.commit()
    db.refresh(todo)
    return _todo_out(todo)


def _goal_out(goal: Goal) -> dict:
    return {
        "id": goal.id,
        "title": goal.title,
        "detail": goal.detail,
        "success_criteria": goal.success_criteria,
        "progress_note": goal.progress_note,
        "status": goal.status,
        "due_at": goal.due_at.isoformat() if goal.due_at else None,
        "created_at": goal.created_at.isoformat() if goal.created_at else None,
    }


def _memory_out(memory: AgentMemory) -> dict:
    return {"key": memory.key, "value": memory.value, "kind": memory.kind}


@app.get("/workspaces/{workspace_id}/dashboard")
def dashboard(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    merchant = db.execute(
        select(MerchantProfile).where(MerchantProfile.workspace_id == actor.workspace_id)
    ).scalars().first()
    products = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id)).scalars().all()
    orders = db.execute(select(Order).where(Order.workspace_id == actor.workspace_id)).scalars().all()
    shipments = db.execute(select(Shipment).where(Shipment.workspace_id == actor.workspace_id)).scalars().all()
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    areas = db.execute(
        select(LayoutArea).where(LayoutArea.workspace_id == actor.workspace_id).order_by(LayoutArea.sort_order)
    ).scalars().all()
    rows = db.execute(
        select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending")
    ).scalars().all()
    counts = {"must_confirm": 0, "needs_reply": 0, "info": 0}
    for todo in rows:
        if todo.kind in counts:
            counts[todo.kind] += 1
    pending = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    ).scalars().all()
    devops_pending = db.execute(
        select(ChangeRequest).where(
            ChangeRequest.workspace_id == actor.workspace_id,
            ChangeRequest.status == "proposed",
        )
    ).scalars().all()

    pipeline = {"new": 0, "lead": 0, "won": 0, "lost": 0}
    for customer in customers:
        key = customer.stage if customer.stage in pipeline else "new"
        pipeline[key] += 1
    order_status: dict[str, int] = {}
    for order in orders:
        order_status[order.status] = order_status.get(order.status, 0) + 1
    shipment_status: dict[str, int] = {}
    for shipment in shipments:
        shipment_status[shipment.status] = shipment_status.get(shipment.status, 0) + 1
    goals = db.execute(
        select(Goal).where(Goal.workspace_id == actor.workspace_id).order_by(Goal.created_at.desc())
    ).scalars().all()
    memories = db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == actor.workspace_id).order_by(AgentMemory.updated_at.desc())
    ).scalars().all()
    workspace = db.get(Workspace, actor.workspace_id)
    model_cfg = (workspace.settings or {}).get("model") or {}
    model_configured = bool(model_cfg.get("api_key_enc") or model_cfg.get("api_key"))

    return {
        "merchant": {
            "shop_name": merchant.shop_name if merchant else None,
            "category": merchant.category if merchant else None,
            "region": merchant.region if merchant else None,
        },
        "kpis": {
            "products": len(products),
            "published": len([p for p in products if p.status == "published"]),
            "low_stock": len([p for p in products if (p.stock or 0) <= 5]),
            "orders": len(orders),
            "pending_ship": order_status.get("PAID", 0),
            "in_delivery": order_status.get("SHIPPED", 0),
            "delivered": order_status.get("DELIVERED", 0),
            "customers": len(customers),
            "pipeline": pipeline,
            "pending_approvals": len(pending),
            "devops_pending": len(devops_pending),
            "todos": counts,
        },
        "areas": [
            {
                "id": a.id,
                "name": a.name,
                "stage": a.stage,
                "kind": a.kind,
                "target_type": a.target_type,
                "parent_id": a.parent_id,
            }
            for a in areas
        ],
        "todos": counts,
        "pending_approvals": len(pending),
        "devops_pending": len(devops_pending),
        "pending_todos": [_todo_out(t) for t in rows],
        "goals": [_goal_out(g) for g in goals],
        "agent_memory": [_memory_out(m) for m in memories],
        "protection": {
            "model_configured": model_configured,
            "scheduler_enabled": settings.scheduler_enabled,
            "region": merchant.region if merchant else (workspace.region if workspace else "CN"),
            "approval_required_for_payments": True,
            "note": "所有外发 / 支付 / 不可逆动作均须主人确认；确认码只通过邮件发给主人，Agent 永不代确认。",
        },
    }


@app.get("/workspaces/{workspace_id}/goals")
def list_goals(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Goal).where(Goal.workspace_id == actor.workspace_id).order_by(Goal.created_at.desc())
    ).scalars().all()
    return {"goals": [_goal_out(g) for g in rows]}


@app.post("/workspaces/{workspace_id}/goals", status_code=201)
def create_goal_endpoint(payload: schemas.GoalCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "goal title is required")
    goal = Goal(
        workspace_id=actor.workspace_id,
        title=title,
        detail=payload.detail,
        success_criteria=payload.success_criteria,
        status="active",
    )
    if payload.due_at:
        try:
            goal.due_at = datetime.fromisoformat(payload.due_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return _goal_out(goal)


@app.patch("/workspaces/{workspace_id}/goals/{goal_id}")
def update_goal_endpoint(goal_id: str, payload: schemas.GoalUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    goal = db.get(Goal, goal_id)
    if goal is None or goal.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "goal not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if field == "due_at":
            try:
                value = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                continue
        setattr(goal, field, value)
    db.commit()
    db.refresh(goal)
    return _goal_out(goal)


@app.get("/workspaces/{workspace_id}/agent/memory")
def get_agent_memory(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == actor.workspace_id).order_by(AgentMemory.updated_at.desc())
    ).scalars().all()
    return {"memories": [_memory_out(m) for m in rows]}


@app.put("/workspaces/{workspace_id}/agent/memory")
def put_agent_memory(payload: schemas.MemoryUpsert, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    key = payload.key.strip()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory key is required")
    kind = payload.kind if payload.kind in ("preference", "fact", "note") else "preference"
    row = db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == actor.workspace_id, AgentMemory.key == key)
    ).scalars().first()
    if row is None:
        row = AgentMemory(workspace_id=actor.workspace_id, key=key, value=payload.value, kind=kind)
        db.add(row)
    else:
        row.value = payload.value
        row.kind = kind
    db.commit()
    from .memory_gov import enforce_capacity

    enforce_capacity(db, actor.workspace_id)
    db.commit()
    db.refresh(row)
    return _memory_out(row)


@app.delete("/workspaces/{workspace_id}/agent/memory/{key}", status_code=204)
def delete_agent_memory(key: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)):
    row = db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == actor.workspace_id, AgentMemory.key == key)
    ).scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    db.delete(row)
    db.commit()


@app.post("/workspaces/{workspace_id}/agent/memory/gc")
def memory_gc(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """Reclaim oldest note rows over the per-workspace memory ceiling."""
    from .memory_gov import enforce_capacity

    report = enforce_capacity(db, actor.workspace_id)
    db.commit()
    return report


@app.post("/workspaces/{workspace_id}/agent/memory/compact")
def memory_compact(payload: dict = Body(default={}), actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """Merge oldest over-threshold notes into a protected digest (M07 layered memory)."""
    from .memory_layers import compact_notes

    threshold = payload.get("threshold")
    batch = payload.get("batch")
    kwargs = {}
    if isinstance(threshold, int) and threshold > 0:
        kwargs["threshold"] = threshold
    if isinstance(batch, int) and batch > 0:
        kwargs["batch"] = batch
    report = compact_notes(db, actor.workspace_id, **kwargs)
    db.commit()
    return report


@app.get("/skills")
def list_skills(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .skills import catalog, installed_skill_ids

    installed = installed_skill_ids(db, actor.workspace_id)
    items = []
    for skill in catalog():
        item = dict(skill)
        item["installed"] = skill["id"] in installed
        items.append(item)
    return {"skills": items}


@app.post("/workspaces/{workspace_id}/skills/{skill_id}/install", status_code=201)
def install_skill_endpoint(skill_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .skills import install_skill

    try:
        result = install_skill(db, actor.workspace_id, skill_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    db.commit()
    return result


@app.post("/workspaces/{workspace_id}/skills/{skill_id}/uninstall")
def uninstall_skill_endpoint(skill_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .skills import uninstall_skill

    try:
        result = uninstall_skill(db, actor.workspace_id, skill_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    db.commit()
    return result


@app.get("/workspaces/{workspace_id}/catalog")
def get_catalog(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    tools = [
        {"type": "tool", "name": t.name, "description": t.description, "risk_level": t.risk_level.value, "side_effect": t.side_effect, "idempotent": t.idempotent}
        for t in registry.all()
    ]
    connectors = [
        {"type": "connector", "id": c.id, "connector_type": c.connector_type, "name": c.name, "status": c.status}
        for c in db.execute(select(ConnectorInstance).where(ConnectorInstance.workspace_id == actor.workspace_id)).scalars().all()
    ]
    functions = [
        {"type": "function", "id": f.id, "name": f.name, "display_name": (f.config or {}).get("display_name") or f.name, "description": f.description, "kind": f.kind, "category": f.category, "risk_level": f.risk_level, "enabled": f.enabled, "stage": f.stage, "acceptance": (f.config or {}).get("acceptance") or "", "goal": (f.config or {}).get("goal") or "", "instructions": (f.config or {}).get("instructions") or ""}
        for f in db.execute(select(WorkspaceFunction).where(WorkspaceFunction.workspace_id == actor.workspace_id).order_by(WorkspaceFunction.name)).scalars().all()
    ]
    return {"catalog": tools + connectors + functions}


@app.get("/workspaces/{workspace_id}/files")
def list_files(actor: Actor = Depends(require_actor)) -> dict:
    from .general import list_data

    return list_data(actor.workspace_id)


@app.post("/workspaces/{workspace_id}/files", status_code=201)
def upload_file(payload: schemas.FileUpload, actor: Actor = Depends(require_actor)) -> dict:
    from .general import save_upload

    try:
        return save_upload(actor.workspace_id, payload.name, payload.content_base64)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.get("/workspaces/{workspace_id}/files/{file_name}")
def read_file(file_name: str, actor: Actor = Depends(require_actor)) -> dict:
    from .general import read_data

    return read_data(actor.workspace_id, file_name)


@app.post("/workspaces/{workspace_id}/tasks", status_code=201)
def new_task(payload: schemas.TaskCreate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    task = create_task(
        db, actor, payload.goal,
        success_criteria=payload.success_criteria,
        constraints=payload.constraints,
        prohibited=payload.prohibited,
        budget_limit=payload.budget_limit,
        risk_level=payload.risk_level,
        steps=payload.steps,
    )
    return {"id": task.id, "status": task.status, "plan": task.plan}


@app.post("/tasks/{task_id}/run")
async def run_task(task_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    try:
        task = await execute_task(db, actor, task_id)
    except GatewayError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"id": task.id, "status": task.status, "result": task.result, "error": task.error}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None or task.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return {"id": task.id, "status": task.status, "plan": task.plan, "result": task.result, "error": task.error}


@app.post("/workspaces/{workspace_id}/chat")
async def chat(payload: schemas.ChatMessage, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .chat import handle_message
    from . import llm, model_registry

    start = time.perf_counter()
    # 按该 Agent 操作页选择的 model_id 路由到对应模型；未选则回退到已配置模型。
    ws = db.get(Workspace, actor.workspace_id)
    chosen = None
    if ws is not None:
        chosen = model_registry.resolve(_model_entries(ws), (payload.model_id or "").strip() or None)
    llm.set_request_model(chosen)
    try:
        result = await handle_message(db, actor, payload.message)
    except Exception:
        record_chat(actor.workspace_id, status="error", duration_ms=(time.perf_counter() - start) * 1000)
        raise
    finally:
        usage = llm.pop_usage()
        llm.set_request_model(None)
    from .brain import preflight_hints

    if usage:
        result["usage"] = usage
        result["model_used"] = (chosen or {}).get("model") or "默认/本地"

    hints: list[dict] = []
    seen: set = set()
    for action in result.get("actions") or []:
        for hint in preflight_hints(action.get("tool", ""), action.get("params") or {}, actor.workspace_id):
            if hint["code"] not in seen:
                seen.add(hint["code"])
                hints.append(hint)
    if hints:
        result["hints"] = hints
    record_chat(actor.workspace_id, status="ok", duration_ms=(time.perf_counter() - start) * 1000)
    return result


@app.post("/workspaces/{workspace_id}/chat/proposal")
async def chat_proposal(payload: schemas.ChatMessage, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """贾维斯请示层：解析指令 → 意图卡（计划动作/权限/负责agent/预案），绝不执行。

    主人看过请示卡说「授权」后，前端再走 /chat 真正执行。本端点只规划、零副作用。
    """
    from .chat import propose

    return propose(db, actor, payload.message)


MAX_SETTING_SNAPSHOTS = 20


def _snapshot_settings(db: Session, ws: Workspace, prior: dict, note: str = "") -> None:
    """Persist the prior settings state so a bad change can be rolled back."""
    from sqlalchemy import func

    version = int(
        db.execute(
            select(func.coalesce(func.max(WorkspaceSettingSnapshot.version), 0)).where(
                WorkspaceSettingSnapshot.workspace_id == ws.id
            )
        ).scalar_one()
    ) + 1
    db.add(WorkspaceSettingSnapshot(workspace_id=ws.id, version=version, settings=dict(prior or {}), note=note))
    excess = db.execute(
        select(WorkspaceSettingSnapshot)
        .where(WorkspaceSettingSnapshot.workspace_id == ws.id)
        .order_by(WorkspaceSettingSnapshot.version.desc())
        .offset(MAX_SETTING_SNAPSHOTS)
    ).scalars().all()
    for snap in excess:
        db.delete(snap)


def _settings_out(settings: dict) -> dict:
    from . import crypto

    model = dict(settings.get("model") or {})
    chat_cfg = dict(settings.get("chat") or {})
    karma = dict(settings.get("karma") or {})
    if model.get("api_key_enc"):
        model["api_key"] = crypto.mask(crypto.decrypt(model["api_key_enc"]))
    elif model.get("api_key"):
        model["api_key"] = crypto.mask(model["api_key"])  # legacy plaintext
    model.pop("api_key_enc", None)
    if chat_cfg.get("token_enc"):
        chat_cfg["token"] = crypto.mask(crypto.decrypt(chat_cfg["token_enc"]))
    elif chat_cfg.get("token"):
        chat_cfg["token"] = crypto.mask(chat_cfg["token"])  # legacy plaintext
    chat_cfg.pop("token_enc", None)
    chat_cfg.pop("token_hash", None)
    if chat_cfg.get("webhook_secret_enc"):
        chat_cfg["webhook_secret"] = crypto.mask(crypto.decrypt(chat_cfg["webhook_secret_enc"]))
    chat_cfg.pop("webhook_secret_enc", None)
    if karma.get("api_key_enc"):
        karma["api_key"] = crypto.mask(crypto.decrypt(karma["api_key_enc"]))
    elif karma.get("api_key"):
        karma["api_key"] = crypto.mask(karma["api_key"])  # legacy plaintext
    karma.pop("api_key_enc", None)
    smtp = dict(settings.get("smtp") or {})
    if smtp.get("password_enc"):
        smtp["password"] = crypto.mask(crypto.decrypt(smtp["password_enc"]))
    elif smtp.get("password"):
        smtp["password"] = crypto.mask(smtp["password"])  # legacy plaintext
    smtp.pop("password_enc", None)
    return {"model": model, "chat": chat_cfg, "karma": karma, "smtp": smtp, "owner_email": settings.get("owner_email") or ""}


@app.get("/workspaces/{workspace_id}/settings")
def get_settings(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    ws = db.get(Workspace, actor.workspace_id)
    return _settings_out(ws.settings or {})


@app.put("/workspaces/{workspace_id}/settings")
def put_settings(payload: schemas.SettingsUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    ws = db.get(Workspace, actor.workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    from . import crypto

    settings = dict(ws.settings or {})
    _snapshot_settings(db, ws, settings, note="settings update")
    if payload.model is not None:
        model = dict(payload.model)
        if model.get("api_key"):
            model["api_key_enc"] = crypto.encrypt(model.pop("api_key"))
        settings["model"] = {**(settings.get("model") or {}), **model}
    if payload.chat is not None:
        chat_cfg = dict(payload.chat)
        if chat_cfg.get("token"):
            chat_cfg["token_hash"] = crypto.hash_value(chat_cfg["token"])
            chat_cfg["token_enc"] = crypto.encrypt(chat_cfg.pop("token"))
        if chat_cfg.get("webhook_secret"):
            chat_cfg["webhook_secret_enc"] = crypto.encrypt(chat_cfg.pop("webhook_secret"))
        settings["chat"] = {**(settings.get("chat") or {}), **chat_cfg}
        if chat_cfg.get("token_hash"):
            from . import token_index

            token_index.upsert(db, ws.id, chat_cfg["token_hash"])
    if payload.karma is not None:
        karma_cfg = dict(payload.karma)
        if karma_cfg.get("api_key"):
            karma_cfg["api_key_enc"] = crypto.encrypt(karma_cfg.pop("api_key"))
        settings["karma"] = {**(settings.get("karma") or {}), **karma_cfg}
    if payload.smtp is not None:
        smtp_cfg = dict(payload.smtp)
        if smtp_cfg.get("password"):
            smtp_cfg["password_enc"] = crypto.encrypt(smtp_cfg.pop("password"))
        settings["smtp"] = {**(settings.get("smtp") or {}), **smtp_cfg}
    if payload.owner_email is not None:
        settings["owner_email"] = payload.owner_email
    ws.settings = settings
    db.commit()
    db.refresh(ws)
    return _settings_out(ws.settings)


# ---------- Karma 结算接入向导（认证状态机 / 连通自检 / 解除绑定） ----------

def _karma_cfg(ws: Workspace) -> dict:
    return dict((ws.settings or {}).get("karma") or {})


@app.get("/workspaces/{workspace_id}/karma/onboard")
def karma_onboard_status(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """只读：Karma 接入向导状态（步骤进度 + 已配置脱敏摘要），不触发任何外部调用。"""
    from . import karma_onboard
    ws = db.get(Workspace, actor.workspace_id)
    state = karma_onboard.derive_state(_karma_cfg(ws))
    state["oauth_callback"] = {
        "supported": False,
        "note": "授权回调需 Karma 官网提供 OAuth 授权页后启用（联调预留位，不伪造）",
    }
    return state


@app.post("/workspaces/{workspace_id}/karma/onboard/confirm-kyc")
def karma_onboard_confirm_kyc(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """owner 登记：用户已在 Karma 官网完成实名认证（KYC）。

    实名认证本身必须发生在 Karma 官网；这里只记录"用户确认已认证"，
    便于向导推进，绝不代替或伪造认证结果。
    """
    from . import karma_onboard
    ws = db.get(Workspace, actor.workspace_id)
    settings = dict(ws.settings or {})
    _snapshot_settings(db, ws, settings, note="karma kyc confirmed")
    karma = dict(settings.get("karma") or {})
    import time as _t
    karma["kyc_confirmed"] = True
    karma["kyc_at"] = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    settings["karma"] = karma
    ws.settings = settings
    db.commit()
    db.refresh(ws)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="karma_onboard_kyc_confirmed", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"workspace:{ws.id}", reason="登记已在 Karma 官网完成实名认证", evidence={}, data_tier="INTERNAL")
    return karma_onboard.derive_state(_karma_cfg(ws))


@app.post("/workspaces/{workspace_id}/karma/onboard/test")
def karma_onboard_test(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """owner 自检：用已保存的凭证真实调用 Karma API（token 交换），返回真实回执。

    失败不伪造成功：明确返回 not_connected 与具体原因（无凭证/凭证无效/网络不可达）。
    """
    from . import crypto, karma as karma_mod
    from .karma import KarmaClientError
    ws = db.get(Workspace, actor.workspace_id)
    karma = _karma_cfg(ws)
    api_key = ""
    if karma.get("api_key_enc"):
        try:
            api_key = crypto.decrypt(karma["api_key_enc"])
        except Exception:
            api_key = ""
    elif karma.get("api_key"):
        api_key = karma["api_key"]
    if not api_key:
        return {"connected": False, "error": "no_api_key", "detail": "请先在设置面板填写 Karma API Key"}
    base_url = karma.get("base_url") or settings.karma_api_base_url
    client = karma_mod.KarmaClient(
        base_url=base_url,
        api_key=api_key,
        agent_id=karma.get("agent_id") or "",
        identity_id=karma.get("identity_id") or "",
        timeout=10.0,
    )
    try:
        token = client.exchange_token()
        agent = {}
        capacity = {}
        try:
            if client.agent_id:
                agent = client.get_agent(client.agent_id)
        except Exception:
            agent = {}
        try:
            if client.identity_id:
                capacity = client.get_capacity(client.identity_id)
        except Exception:
            capacity = {}
        ok = bool(token)
        ws_settings = dict(ws.settings or {})
        _snapshot_settings(db, ws, ws_settings, note="karma onboard test")
        karma_cfg = dict(ws_settings.get("karma") or {})
        karma_cfg["test_ok"] = ok
        karma_cfg["test_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        ws_settings["karma"] = karma_cfg
        ws.settings = ws_settings
        db.commit()
        db.refresh(ws)
        audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                     action="karma_onboard_test", risk_level="R1", decision=Decision.ALLOWED,
                     resource=f"workspace:{ws.id}", reason="Karma 连通自检",
                     evidence={"connected": ok, "agent_id": bool(agent), "identity_id": bool(capacity)},
                     data_tier="INTERNAL")
        return {
            "connected": ok,
            "error": "" if ok else "test_failed",
            "token_exchanged": bool(token),
            "agent": {"id": agent.get("id") or agent.get("agent_id") or ""} if agent else {},
            "identity": {"capacity_lookup": bool(capacity)},
            "detail": "Karma API 连接成功" if ok else "凭证已连通但回执异常",
            "note": "真实 Karma API 自检回执",
        }
    except KarmaClientError as exc:
        msg = str(exc)
        err = "invalid_credentials" if ("401" in msg or "403" in msg) else "api_error"
        return {"connected": False, "error": err, "detail": msg[:300], "note": "真实 Karma API 自检回执"}
    except Exception as exc:  # httpx 网络/超时等
        return {"connected": False, "error": "unreachable", "detail": str(exc)[:300], "note": "真实 Karma API 自检回执"}
    finally:
        client.close()


@app.post("/workspaces/{workspace_id}/karma/onboard/unlink")
def karma_onboard_unlink(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """owner 解除绑定：清除本工作区的 Karma 凭证与认证标记，回到未接入（stub）状态。"""
    from . import karma_onboard
    ws = db.get(Workspace, actor.workspace_id)
    settings = dict(ws.settings or {})
    _snapshot_settings(db, ws, settings, note="karma unlink")
    settings["karma"] = {}
    ws.settings = settings
    db.commit()
    db.refresh(ws)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="karma_onboard_unlinked", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"workspace:{ws.id}", reason="解除 Karma 结算绑定，回到 stub 模式", evidence={}, data_tier="INTERNAL")
    return karma_onboard.derive_state(_karma_cfg(ws))


# ---------- 多模型注册表（总控制台可接入多个模型，Agent 按 model_id 选择） ----------

def _model_entries(ws: Workspace) -> list[dict]:
    """读 settings["models"]；为空则把旧单份 settings["model"] 临时迁移为一条视图。"""
    from . import model_registry
    settings = ws.settings or {}
    entries = list(settings.get("models") or [])
    if not entries:
        entries = model_registry.migrate_legacy(settings.get("model"))
    return entries


@app.get("/workspaces/{workspace_id}/models")
def models_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """只读：已登记模型脱敏列表（密钥仅 mask），供各 Agent 操作页选择。"""
    from . import model_registry
    ws = db.get(Workspace, actor.workspace_id)
    return {
        "models": model_registry.list_views(_model_entries(ws)),
        "capabilities": sorted(model_registry.ALL_CAPS),
    }


@app.post("/workspaces/{workspace_id}/models")
def models_upsert(payload: dict = Body(...), actor: Actor = Depends(require_owner),
                  db: Session = Depends(get_db)) -> dict:
    """登记/更新一条模型（owner）：api_key 明文入库即加密，不回传密文。"""
    from . import model_registry
    ws = db.get(Workspace, actor.workspace_id)
    settings = dict(ws.settings or {})
    entries = list(settings.get("models") or model_registry.migrate_legacy(settings.get("model")))
    target_id = (payload.get("id") or "").strip()
    existing = next((e for e in entries if e.get("id") == target_id), None) if target_id else None
    entry = model_registry.normalize_entry(payload, existing)
    if existing:
        entries = [entry if e.get("id") == entry["id"] else e for e in entries]
    else:
        entries.append(entry)
    settings["models"] = entries
    ws.settings = settings
    db.commit()
    db.refresh(ws)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="model_registered", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"model:{entry['id']}", reason=f"登记模型 {entry['label']} ({entry['model']})",
                 evidence={"provider": entry["provider"], "capabilities": entry["capabilities"]},
                 data_tier="INTERNAL")
    return {"model": model_registry.view(entry)}


@app.delete("/workspaces/{workspace_id}/models/{mid}")
def models_delete(mid: str, actor: Actor = Depends(require_owner),
                  db: Session = Depends(get_db)) -> dict:
    """删除一条已登记模型（owner）；不删被引用配置，仅移除注册表条目。"""
    from . import model_registry
    ws = db.get(Workspace, actor.workspace_id)
    settings = dict(ws.settings or {})
    entries = list(settings.get("models") or [])
    before = len(entries)
    entries = [e for e in entries if e.get("id") != mid]
    if len(entries) == before:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在")
    settings["models"] = entries
    ws.settings = settings
    db.commit()
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="model_removed", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"model:{mid}", reason="移除已登记模型", evidence={},
                 data_tier="INTERNAL")
    return {"removed": mid, "remaining": len(entries)}


# ---------- 内容流水线底座：文案/视频 draft -> review -> approved -> ready -> published ----------

def _content_dict(c: ContentItem) -> dict:
    from . import content_pipeline as cp
    return {
        "id": c.id, "kind": c.kind, "title": c.title, "topic": c.topic,
        "body": c.body, "assets": c.assets or {}, "model_id": c.model_id,
        "status": c.status, "stage": cp.stage_label(c.status),
        "reviewer_note": c.reviewer_note, "created_by": c.created_by,
        "reviewed_by": c.reviewed_by, "target_channels": c.target_channels or [],
        "source_ref": c.source_ref, "metrics": c.metrics or {},
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _load_content(db: Session, cid: str, actor: Actor) -> ContentItem:
    c = db.get(ContentItem, cid)
    if c is None or c.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "内容条目不存在")
    return c


@app.post("/workspaces/{workspace_id}/content")
def content_draft(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                  db: Session = Depends(get_db)) -> dict:
    """Agent 起草文案/视频草稿（R1，仅本地，未拍板不得发布）。"""
    from . import content_pipeline as cp
    kind = (payload.get("kind") or cp.KIND_COPY).strip()
    if not cp.valid_kind(kind):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知内容类型 {kind}")
    item = ContentItem(
        workspace_id=actor.workspace_id, kind=kind,
        title=(payload.get("title") or "").strip()[:200],
        topic=(payload.get("topic") or "").strip(),
        body=(payload.get("body") or "").strip(),
        assets=payload.get("assets") or {},
        model_id=(payload.get("model_id") or "").strip(),
        status=cp.DRAFT,
        created_by=actor.user.id or "",
        target_channels=payload.get("target_channels") or [],
        source_ref=(payload.get("source_ref") or "").strip(),
    )
    db.add(item)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_drafted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{item.id}", reason=f"起草{kind}草稿：{item.title}",
                 evidence={"kind": kind}, data_tier="INTERNAL")
    db.commit()
    db.refresh(item)
    return {"item": _content_dict(item)}


@app.get("/workspaces/{workspace_id}/content")
def content_list(kind: str | None = None, status: str | None = None,
                 actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    q = select(ContentItem).where(ContentItem.workspace_id == actor.workspace_id)
    if kind:
        q = q.where(ContentItem.kind == kind)
    if status:
        q = q.where(ContentItem.status == status)
    rows = db.execute(q.order_by(ContentItem.created_at.desc())).scalars().all()
    return {"items": [_content_dict(r) for r in rows]}


@app.get("/workspaces/{workspace_id}/content/{cid}")
def content_get(cid: str, actor: Actor = Depends(require_actor),
                db: Session = Depends(get_db)) -> dict:
    return {"item": _content_dict(_load_content(db, cid, actor))}


@app.post("/workspaces/{workspace_id}/content/{cid}/submit")
def content_submit(cid: str, actor: Actor = Depends(require_actor),
                   db: Session = Depends(get_db)) -> dict:
    """draft/rejected -> review：提交等待 CEO 拍板。"""
    from . import content_pipeline as cp
    c = _load_content(db, cid, actor)
    if not cp.can_transition(c.status, cp.REVIEW):
        raise HTTPException(status.HTTP_409_CONFLICT, f"状态 {c.status} 不能提交拍板")
    c.status = cp.REVIEW
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_submitted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{cid}", reason="提交内容等待 CEO 拍板",
                 evidence={"kind": c.kind}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"item": _content_dict(c)}


@app.post("/workspaces/{workspace_id}/content/{cid}/approve")
def content_approve(cid: str, payload: dict = Body(default={}),
                    actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """CEO/老板拍板批准：review -> approved（owner 专属）。"""
    from . import content_pipeline as cp
    c = _load_content(db, cid, actor)
    if not cp.can_transition(c.status, cp.APPROVED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"状态 {c.status} 不能批准")
    c.status = cp.APPROVED
    c.reviewed_by = actor.user.id or ""
    c.decided_at = datetime.utcnow()
    c.reviewer_note = (payload.get("note") or c.reviewer_note)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_approved", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"content:{cid}", reason=f"CEO 批准内容：{c.title}",
                 evidence={"kind": c.kind}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"item": _content_dict(c)}


@app.post("/workspaces/{workspace_id}/content/{cid}/reject")
def content_reject(cid: str, payload: dict = Body(default={}),
                   actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """CEO/老板驳回：review -> rejected（owner 专属，审计 DENIED）。"""
    from . import content_pipeline as cp
    c = _load_content(db, cid, actor)
    if not cp.can_transition(c.status, cp.REJECTED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"状态 {c.status} 不能驳回")
    c.status = cp.REJECTED
    c.reviewed_by = actor.user.id or ""
    c.decided_at = datetime.utcnow()
    c.reviewer_note = (payload.get("note") or "")
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_rejected", risk_level="R2", decision=Decision.DENIED,
                 resource=f"content:{cid}", reason=f"CEO 驳回内容：{c.title}",
                 evidence={"note": c.reviewer_note}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"item": _content_dict(c)}


@app.post("/workspaces/{workspace_id}/content/{cid}/to-ready")
def content_to_ready(cid: str, actor: Actor = Depends(require_actor),
                     db: Session = Depends(get_db)) -> dict:
    """approved -> ready_to_publish：交接给社交运营，仅内部状态，不对外发。"""
    from . import content_pipeline as cp
    c = _load_content(db, cid, actor)
    if not cp.can_transition(c.status, cp.READY):
        raise HTTPException(status.HTTP_409_CONFLICT, f"状态 {c.status} 不能交接待发布")
    c.status = cp.READY
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_ready", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{cid}", reason="批准内容交接为待发布",
                 evidence={"kind": c.kind}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"item": _content_dict(c)}


# ---------- 文案写作 Agent：本地卖点骨架，模型润色位（不联网、不虚构功效） ----------

@app.post("/workspaces/{workspace_id}/content/copy/draft")
def copy_draft(payload: dict = Body(...), actor: Actor = Depends(require_actor),
               db: Session = Depends(get_db)) -> dict:
    """文案 Agent：由商品/人群/卖点生成本地骨架草稿（draft，待 CEO 拍板）。"""
    from . import copywriter, model_registry
    brief = copywriter.build_copy_brief(
        product=payload.get("product", ""), audience=payload.get("audience", ""),
        selling_points=payload.get("selling_points") or [], tone=payload.get("tone", "专业"))
    ws = db.get(Workspace, actor.workspace_id)
    model_id = (payload.get("model_id") or "").strip()
    resolved = model_registry.resolve(_model_entries(ws), model_id) if model_id else None
    item = ContentItem(
        workspace_id=actor.workspace_id, kind="copy",
        title=brief["product"] or "文案草稿",
        topic=brief["audience"],
        body=brief["versions"]["detail"],
        assets={"brief": brief, "model_configured": bool(resolved), "model_id": model_id},
        model_id=model_id, status="draft",
        created_by=actor.user.id or "",
        target_channels=payload.get("target_channels") or [],
        source_ref=(payload.get("source_ref") or "").strip(),
    )
    db.add(item)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_drafted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{item.id}", reason=f"文案Agent起草：{item.title}",
                 evidence={"kind": "copy"}, data_tier="INTERNAL")
    db.commit()
    db.refresh(item)
    return {
        "item": _content_dict(item),
        "model_configured": bool(resolved),
        "compliance_notes": brief["compliance_notes"],
    }


# ---------- 视频 Agent：分镜脚本/参数单，真实生成凭证位（不伪造成片） ----------

@app.post("/workspaces/{workspace_id}/content/video/draft")
def video_draft(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                db: Session = Depends(get_db)) -> dict:
    """视频 Agent：生成分镜脚本/参数单草稿（draft，待 CEO 拍板；不生成真实视频）。"""
    from . import video_agent, model_registry
    plan = video_agent.build_video_plan(
        topic=payload.get("topic", ""), audience=payload.get("audience", ""),
        duration_sec=payload.get("duration_sec", 30), aspect=payload.get("aspect", "9:16"),
        style=payload.get("style", "纪实感"), shots=payload.get("shots") or [])
    ws = db.get(Workspace, actor.workspace_id)
    model_id = (payload.get("model_id") or "").strip()
    resolved = model_registry.resolve(_model_entries(ws), model_id) if model_id else None
    item = ContentItem(
        workspace_id=actor.workspace_id, kind="video",
        title=plan["topic"] or "视频脚本草稿",
        topic=plan["audience"],
        body=plan["script"],
        assets={"plan": plan, "model_configured": bool(resolved), "model_id": model_id},
        model_id=model_id, status="draft",
        created_by=actor.user.id or "",
        target_channels=payload.get("target_channels") or [],
        source_ref=(payload.get("source_ref") or "").strip(),
    )
    db.add(item)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_drafted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{item.id}", reason=f"视频Agent起草脚本：{item.title}",
                 evidence={"kind": "video"}, data_tier="INTERNAL")
    db.commit()
    db.refresh(item)
    return {
        "item": _content_dict(item),
        "model_configured": bool(resolved),
        "generation": plan["generation"],
    }


def _product_selling_points(p: Product) -> list[str]:
    """从产品库字段自动提取卖点：specs 键值优先，detail 关键词兜底。"""
    points: list[str] = []
    specs = p.specs or {}
    for k, v in specs.items():
        key = str(k).strip()
        val = str(v).strip()
        if key and val and val.lower() not in ("无", "none", "n/a", "-"):
            points.append(f"{key}：{val}" if not key.endswith(("：", ":")) else f"{key}{val}")
    detail = (p.detail or "").strip()
    if detail:
        lines = [ln.strip() for ln in detail.splitlines() if ln.strip()]
        for ln in lines[:3]:
            ln = ln.lstrip("0123456789.-、• ")
            if ln and len(ln) <= 40 and ln not in points:
                points.append(ln)
    return points[:8]


@app.post("/workspaces/{workspace_id}/content/copy/draft-from-product")
def copy_draft_from_product(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                            db: Session = Depends(get_db)) -> dict:
    """文案 Agent（产品库链路）：按 product_id 自动读库，不手工填产品/卖点。

    产品名/规格/详情自动进入 brief；audience/tone 可覆盖，缺省自动给默认。
    """
    from . import copywriter, model_registry
    product_id = str(payload.get("product_id") or "").strip()
    if not product_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "product_id 必填")
    p = db.get(Product, product_id)
    if p is None or p.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "product not found in workspace")
    points = _product_selling_points(p)
    audience = (payload.get("audience") or "").strip() or "目标客户"
    brief = copywriter.build_copy_brief(
        product=p.name, audience=audience,
        selling_points=points or ["核心优势待补充"],
        tone=(payload.get("tone") or "").strip() or "专业")
    ws = db.get(Workspace, actor.workspace_id)
    model_id = (payload.get("model_id") or "").strip()
    resolved = model_registry.resolve(_model_entries(ws), model_id) if model_id else None
    item = ContentItem(
        workspace_id=actor.workspace_id, kind="copy",
        title=brief["product"] or "文案草稿",
        topic=brief["audience"],
        body=brief["versions"]["detail"],
        assets={"brief": brief, "model_configured": bool(resolved), "model_id": model_id,
                "from_product": {"product_id": p.id, "name": p.name, "price": p.price,
                                 "category": p.category, "stock": p.stock}},
        model_id=model_id, status="draft",
        created_by=actor.user.id or "",
        target_channels=payload.get("target_channels") or [],
        source_ref=(payload.get("source_ref") or "").strip(),
    )
    db.add(item)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_drafted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{item.id}", reason=f"文案Agent从产品库起草：{p.name}",
                 evidence={"kind": "copy", "product_id": p.id}, data_tier="INTERNAL")
    db.commit()
    db.refresh(item)
    return {
        "item": _content_dict(item),
        "model_configured": bool(resolved),
        "compliance_notes": brief["compliance_notes"],
        "from_product": {"product_id": p.id, "name": p.name, "price": p.price,
                         "stock": p.stock, "selling_points": points},
    }


@app.post("/workspaces/{workspace_id}/content/video/draft-from-product")
def video_draft_from_product(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                             db: Session = Depends(get_db)) -> dict:
    """视频 Agent（产品库链路）：按 product_id 自动读库生成分镜脚本草稿。"""
    from . import video_agent, model_registry
    product_id = str(payload.get("product_id") or "").strip()
    if not product_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "product_id 必填")
    p = db.get(Product, product_id)
    if p is None or p.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "product not found in workspace")
    points = _product_selling_points(p)
    topic = (payload.get("topic") or "").strip() or f"{p.name} 产品展示"
    audience = (payload.get("audience") or "").strip() or "目标客户"
    plan = video_agent.build_video_plan(
        topic=topic, audience=audience,
        duration_sec=payload.get("duration_sec", 30), aspect=payload.get("aspect", "9:16"),
        style=(payload.get("style") or "").strip() or "纪实感",
        shots=payload.get("shots") or [])
    plan = {**plan, "selling_points": points, "product_id": p.id, "product_name": p.name}
    ws = db.get(Workspace, actor.workspace_id)
    model_id = (payload.get("model_id") or "").strip()
    resolved = model_registry.resolve(_model_entries(ws), model_id) if model_id else None
    item = ContentItem(
        workspace_id=actor.workspace_id, kind="video",
        title=plan["topic"] or "视频脚本草稿",
        topic=plan["audience"],
        body=plan["script"],
        assets={"plan": plan, "model_configured": bool(resolved), "model_id": model_id,
                "from_product": {"product_id": p.id, "name": p.name, "price": p.price,
                                 "category": p.category, "stock": p.stock}},
        model_id=model_id, status="draft",
        created_by=actor.user.id or "",
        target_channels=payload.get("target_channels") or [],
        source_ref=(payload.get("source_ref") or "").strip(),
    )
    db.add(item)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_drafted", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"content:{item.id}", reason=f"视频Agent从产品库起草脚本：{p.name}",
                 evidence={"kind": "video", "product_id": p.id}, data_tier="INTERNAL")
    db.commit()
    db.refresh(item)
    return {
        "item": _content_dict(item),
        "model_configured": bool(resolved),
        "generation": plan["generation"],
        "from_product": {"product_id": p.id, "name": p.name, "price": p.price,
                         "stock": p.stock, "selling_points": points},
    }


# ---------- 社交运营 Agent：发布队列/闸门、评论箱、回复草稿、热门建档 ----------

def _social_channel_configured(ws: Workspace) -> bool:
    channels = (ws.settings or {}).get("social_channels") or []
    return any(isinstance(ch, dict) and ch.get("configured") for ch in channels)


def _comment_dict(c: SocialComment) -> dict:
    return {
        "id": c.id, "content_id": c.content_id, "platform": c.platform,
        "external_id": c.external_id, "author": c.author, "text": c.text,
        "sentiment": c.sentiment, "status": c.status, "reply_draft": c.reply_draft,
        "received_at": c.received_at.isoformat() if c.received_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _trending_dict(t: TrendingVideo) -> dict:
    return {
        "id": t.id, "platform": t.platform, "url": t.url, "title": t.title,
        "category": t.category, "metrics": t.metrics or {},
        "hot_points": t.hot_points or [], "summary": t.summary,
        "analyzed_at": t.analyzed_at.isoformat() if t.analyzed_at else None,
    }


# ---------- 统一渠道账号接入（官方合规渠道，凭证加密，不伪造联网连通） ----------
def _channel_dict(ca: ChannelAccount) -> dict:
    # 绝不回传 credential_enc；只回是否已配置凭证
    return {
        "id": ca.id, "channel_type": ca.channel_type,
        "account_label": ca.account_label, "identifier": ca.identifier,
        "enabled": bool(ca.enabled), "status": ca.status,
        "has_credential": bool((ca.credential_enc or "").strip()),
        "risk_note": ca.risk_note, "capabilities": ca.capabilities or [],
        "last_checked_at": ca.last_checked_at.isoformat() if ca.last_checked_at else None,
        "created_at": ca.created_at.isoformat() if ca.created_at else None,
    }


@app.get("/workspaces/{workspace_id}/channels/catalog")
def channels_catalog(actor: Actor = Depends(require_actor)) -> dict:
    """只读：官方合规渠道目录（接入方式/风险/能力），供前端渲染。"""
    from . import channels as ch
    return {"channels": ch.catalog()}


@app.get("/workspaces/{workspace_id}/channels")
def channels_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ChannelAccount)
        .where(ChannelAccount.workspace_id == actor.workspace_id)
        .order_by(ChannelAccount.created_at.desc())
    ).scalars().all()
    return {"channels": [_channel_dict(r) for r in rows]}


@app.post("/workspaces/{workspace_id}/channels")
def channels_create(payload: dict = Body(default={}),
                    actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    from . import channels as ch
    from . import crypto
    ctype = (payload.get("channel_type") or "").strip()
    if not ch.is_known(ctype):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知或不支持的渠道：{ctype}")
    spec = ch.spec_for(ctype)
    cred = (payload.get("credential") or "").strip()
    ca = ChannelAccount(
        workspace_id=actor.workspace_id,
        channel_type=ctype,
        account_label=(payload.get("account_label") or spec.name).strip()[:80],
        identifier=(payload.get("identifier") or "").strip()[:160],
        credential_enc=crypto.encrypt(cred) if cred else "",
        enabled=bool(payload.get("enabled", True)),
        risk_note=spec.risk_note,
        capabilities=list(spec.capabilities),
        status=ch.probe_status(cred),
        created_by=actor.user.id or "",
    )
    db.add(ca)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="channel_connected", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"channel:{ca.id}", reason=f"接入渠道账号：{spec.name}",
                 evidence={"channel_type": ctype, "has_credential": bool(cred)},
                 data_tier="SENSITIVE")
    db.commit()
    db.refresh(ca)
    return {"channel": _channel_dict(ca)}


@app.post("/workspaces/{workspace_id}/channels/{cid}/toggle")
def channels_toggle(cid: str, actor: Actor = Depends(require_owner),
                     db: Session = Depends(get_db)) -> dict:
    ca = db.get(ChannelAccount, cid)
    if not ca or ca.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道账号不存在")
    ca.enabled = not ca.enabled
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="channel_toggled", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"channel:{cid}", reason=f"渠道账号{'启用' if ca.enabled else '停用'}",
                 evidence={"channel_type": ca.channel_type, "enabled": ca.enabled},
                 data_tier="INTERNAL")
    db.commit()
    db.refresh(ca)
    return {"channel": _channel_dict(ca)}


@app.post("/workspaces/{workspace_id}/channels/{cid}/probe")
def channels_probe(cid: str, actor: Actor = Depends(require_actor),
                   db: Session = Depends(get_db)) -> dict:
    """确定性连通性判定：只看是否已填凭证，不联网、不伪造 connected。"""
    from . import channels as ch
    ca = db.get(ChannelAccount, cid)
    if not ca or ca.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道账号不存在")
    ca.status = ch.probe_status(ca.credential_enc or "", enabled=ca.enabled)
    ca.last_checked_at = datetime.utcnow()
    db.commit()
    db.refresh(ca)
    return {"channel": _channel_dict(ca)}


@app.delete("/workspaces/{workspace_id}/channels/{cid}")
def channels_delete(cid: str, actor: Actor = Depends(require_owner),
                    db: Session = Depends(get_db)) -> dict:
    ca = db.get(ChannelAccount, cid)
    if not ca or ca.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "渠道账号不存在")
    db.delete(ca)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="channel_disconnected", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"channel:{cid}", reason="删除渠道账号接入",
                 evidence={"channel_type": ca.channel_type}, data_tier="INTERNAL")
    db.commit()
    return {"ok": True}


@app.get("/workspaces/{workspace_id}/social/publish-queue")
def publish_queue(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """只读：待发布队列（仅 ready_to_publish）+ 渠道凭证是否齐备。"""
    from . import content_pipeline as cp
    ws = db.get(Workspace, actor.workspace_id)
    rows = db.execute(
        select(ContentItem).where(
            ContentItem.workspace_id == actor.workspace_id,
            ContentItem.status == cp.READY,
        )
    ).scalars().all()
    return {
        "queue": [_content_dict(r) for r in rows],
        "channel_configured": _social_channel_configured(ws),
    }


@app.post("/workspaces/{workspace_id}/content/{cid}/publish")
def content_publish(cid: str, payload: dict = Body(default={}),
                    actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """发布闸门：仅 ready_to_publish 且渠道凭证齐备才可发布；否则拒绝，绝不伪造成功。"""
    from . import social_agent
    c = _load_content(db, cid, actor)
    ws = db.get(Workspace, actor.workspace_id)
    ok, reason = social_agent.publish_gate(c.status, _social_channel_configured(ws))
    if not ok:
        audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                     action="content_publish_blocked", risk_level="R2",
                     decision=Decision.DENIED, resource=f"content:{cid}",
                     reason=reason, evidence={"status": c.status}, data_tier="INTERNAL")
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    # 真实外发需渠道 API 回执；本机无凭证位时走到这里也只登记意图，不标记已发布
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="content_publish_intent", risk_level="R2",
                 decision=Decision.ALLOWED, resource=f"content:{cid}",
                 reason="进入发布（待渠道回执）", evidence={"channels": payload.get("channels") or []},
                 data_tier="INTERNAL")
    db.commit()
    return {"item": _content_dict(c), "status": "queued",
            "note": "待渠道回执后才回写 published，不伪造发布成功"}


@app.post("/workspaces/{workspace_id}/social/comments")
def comment_record(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                   db: Session = Depends(get_db)) -> dict:
    """录入评论建档（可手动录入；真实抓取留渠道凭证位），自动情感+回复草稿，不发送。"""
    from . import social_agent
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "评论内容为空")
    sentiment = social_agent.sentiment(text)
    draft = social_agent.reply_draft(text, sentiment, payload.get("tone", "友好"))
    c = SocialComment(
        workspace_id=actor.workspace_id,
        content_id=(payload.get("content_id") or "").strip(),
        platform=(payload.get("platform") or "manual").strip()[:24],
        external_id=(payload.get("external_id") or "").strip()[:80],
        author=(payload.get("author") or "").strip()[:80],
        text=text[:1000], sentiment=sentiment, status="draft_reply",
        reply_draft=draft["draft"], received_at=datetime.utcnow(),
    )
    db.add(c)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="comment_recorded", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"comment:{c.id}", reason="录入评论并生成回复草稿",
                 evidence={"sentiment": sentiment}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"comment": _comment_dict(c)}


@app.get("/workspaces/{workspace_id}/social/comments")
def comments_list(status: str | None = None, actor: Actor = Depends(require_actor),
                  db: Session = Depends(get_db)) -> dict:
    q = select(SocialComment).where(SocialComment.workspace_id == actor.workspace_id)
    if status:
        q = q.where(SocialComment.status == status)
    rows = db.execute(q.order_by(SocialComment.created_at.desc())).scalars().all()
    return {"comments": [_comment_dict(r) for r in rows]}


@app.post("/workspaces/{workspace_id}/social/comments/{scid}/approve-reply")
def comment_approve_reply(scid: str, payload: dict = Body(default={}),
                         actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """老板确认回复草稿（draft_reply -> approved_reply）；仅确认，不自动发送。"""
    c = db.get(SocialComment, scid)
    if c is None or c.workspace_id != actor.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "评论不存在")
    if c.status not in ("draft_reply", "approved_reply"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"状态 {c.status} 不可确认回复")
    if payload.get("reply_draft"):
        c.reply_draft = (payload["reply_draft"] or "").strip()[:1000]
    c.status = "approved_reply"
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="comment_reply_approved", risk_level="R2", decision=Decision.ALLOWED,
                 resource=f"comment:{scid}", reason="确认评论回复草稿（未发送）",
                 evidence={}, data_tier="INTERNAL")
    db.commit()
    db.refresh(c)
    return {"comment": _comment_dict(c)}


@app.post("/workspaces/{workspace_id}/social/trending")
def trending_record(payload: dict = Body(...), actor: Actor = Depends(require_actor),
                    db: Session = Depends(get_db)) -> dict:
    """热门视频建档：录入指标，确定性提炼热门点（不联网抓取、不编造数据）。"""
    from . import social_agent
    analysis = social_agent.analyze_trending(
        title=payload.get("title", ""), metrics=payload.get("metrics") or {},
        category=payload.get("category", ""))
    t = TrendingVideo(
        workspace_id=actor.workspace_id,
        platform=(payload.get("platform") or "").strip()[:24],
        url=(payload.get("url") or "").strip()[:500],
        title=analysis["title"], category=analysis["category"],
        metrics=analysis["metrics"], hot_points=analysis["hot_points"],
        summary=analysis["summary"], analyzed_at=datetime.utcnow(),
    )
    db.add(t)
    audit_record(db, workspace_id=actor.workspace_id, actor_id=actor.user.id or "",
                 action="trending_archived", risk_level="R1", decision=Decision.ALLOWED,
                 resource=f"trending:{t.id}", reason="热门视频建档并提炼热门点",
                 evidence={"points": analysis["hot_points"]}, data_tier="INTERNAL")
    db.commit()
    db.refresh(t)
    return {"video": _trending_dict(t)}


@app.get("/workspaces/{workspace_id}/social/trending")
def trending_list(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(TrendingVideo).where(TrendingVideo.workspace_id == actor.workspace_id)
        .order_by(TrendingVideo.created_at.desc())
    ).scalars().all()
    return {"videos": [_trending_dict(r) for r in rows]}


@app.get("/workspaces/{workspace_id}/settings/history")
def settings_history(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(WorkspaceSettingSnapshot)
        .where(WorkspaceSettingSnapshot.workspace_id == actor.workspace_id)
        .order_by(WorkspaceSettingSnapshot.version.desc())
        .limit(MAX_SETTING_SNAPSHOTS)
    ).scalars().all()
    return {
        "versions": [
            {"version": snap.version, "note": snap.note, "created_at": snap.created_at.isoformat()}
            for snap in rows
        ]
    }


@app.post("/workspaces/{workspace_id}/settings/rollback/{version}")
def settings_rollback(version: int, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    if actor.role is not Role.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the workspace owner can rollback settings")
    snap = db.execute(
        select(WorkspaceSettingSnapshot).where(
            WorkspaceSettingSnapshot.workspace_id == actor.workspace_id,
            WorkspaceSettingSnapshot.version == version,
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "settings version not found")
    ws = db.get(Workspace, actor.workspace_id)
    _snapshot_settings(db, ws, dict(ws.settings or {}), note=f"pre-rollback to v{version}")
    ws.settings = dict(snap.settings or {})
    db.commit()
    db.refresh(ws)
    return _settings_out(ws.settings)


@app.post("/workspaces/{workspace_id}/settings/test-email")
def test_email(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .notify import notify_owner_for_workspace

    return notify_owner_for_workspace(
        db,
        actor.workspace_id,
        "[Krama] 测试邮件",
        "如果你收到这封邮件，说明确认码邮件链路已通。",
    )


@app.get("/workspaces/{workspace_id}/readiness")
def readiness(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    ws = db.get(Workspace, actor.workspace_id)
    st = ws.settings or {}
    model_cfg = st.get("model") or {}
    smtp_cfg = st.get("smtp") or {}
    karma_cfg = st.get("karma") or {}
    chat_cfg = st.get("chat") or {}

    model_ok = bool(model_cfg.get("api_key") or model_cfg.get("api_key_enc"))
    smtp_ok = bool(smtp_cfg.get("host") and smtp_cfg.get("from") and (smtp_cfg.get("password") or smtp_cfg.get("password_enc")))
    owner_email_ok = bool((st.get("owner_email") or "") or settings.owner_email)
    karma_ok = bool(karma_cfg.get("api_key") or karma_cfg.get("api_key_enc"))
    chat_ok = bool(chat_cfg.get("enabled") and (chat_cfg.get("token_hash") or chat_cfg.get("token_enc") or chat_cfg.get("token")))
    receipt_ok = (
        db.execute(
            select(WorkspaceFunction).where(
                WorkspaceFunction.workspace_id == ws.id,
                WorkspaceFunction.category == "receipt",
                WorkspaceFunction.enabled.is_(True),
            )
        ).scalars().first()
        is not None
    )
    connectors = db.execute(select(ConnectorInstance).where(ConnectorInstance.workspace_id == ws.id)).scalars().all()
    real_connector_ok = any(
        (c.config or {}).get("endpoint_url") or (c.config or {}).get("auth") for c in connectors
    ) or karma_ok

    items = [
        {"key": "model", "label": "模型", "ok": model_ok, "detail": "已配置，Agent 可自主规划" if model_ok else "在设置里保存模型 Key"},
        {"key": "smtp", "label": "邮件确认码", "ok": smtp_ok and owner_email_ok, "detail": "确认码可邮件发送" if (smtp_ok and owner_email_ok) else "配置 SMTP 和收件邮箱"},
        {"key": "karma", "label": "Karma 收款", "ok": karma_ok, "detail": "已配置支付 Key" if karma_ok else "接入 Karma API Key"},
        {"key": "chat", "label": "聊天工具接入", "ok": chat_ok, "detail": "可自动回复客户" if chat_ok else "接入 Telegram/微信/Discord"},
        {"key": "receipt", "label": "回执网址功能", "ok": receipt_ok, "detail": "已添加回执读取功能" if receipt_ok else "在功能区添加回执网址"},
        {"key": "connectors", "label": "业务连接器", "ok": real_connector_ok, "detail": "已接真实渠道" if real_connector_ok else "接入外卖/电商/物流 API"},
    ]
    return {"items": items, "all_ready": all(item["ok"] for item in items)}


@app.get("/workspaces/{workspace_id}/nervous/tentacles")
def nervous_tentacles(actor: Actor = Depends(require_actor)) -> dict:
    """Read-only neural tentacle index: who owns each domain/tool/panel."""
    from . import nervous

    return {"tentacles": nervous.tentacles_index()}


@app.get("/workspaces/{workspace_id}/nervous/assignment")
def nervous_assignment(tool: str = "", actor: Actor = Depends(require_actor)) -> dict:
    """CEO dispatch card for one tool/action (who owns it, autonomy, plan, plan_b)."""
    from . import nervous

    return nervous.assign(tool)


@app.get("/workspaces/{workspace_id}/nervous/panel/{panel}")
def nervous_panel(panel: str, actor: Actor = Depends(require_actor)) -> dict:
    """Responsible owner card for a first-screen panel key."""
    from . import nervous

    return nervous.panel_owner(panel)


@app.get("/workspaces/{workspace_id}/nervous/overview")
def nervous_overview(actor: Actor = Depends(require_actor)) -> dict:
    """Global neural ledger: domain/tool counts, force-escalation list, rollups."""
    from . import nervous

    return nervous.overview()


@app.get("/workspaces/{workspace_id}/ledger/board")
def ledger_board(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """Unified task board: queued/pending_approval/executing/completed/failed/escalated."""
    from .ledger import build_board
    from .models import Execution

    executions = db.execute(
        select(Execution).where(Execution.workspace_id == actor.workspace_id)
        .order_by(Execution.created_at.desc()).limit(200)
    ).scalars().all()
    approvals = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        ).order_by(ApprovalRequest.created_at.desc())
    ).scalars().all()
    todos = db.execute(
        select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending")
        .order_by(Todo.created_at.desc())
    ).scalars().all()
    return build_board(executions=executions, approvals=approvals, todos=todos)


@app.get("/workspaces/{workspace_id}/integrations/status")
def integrations_status(actor: Actor = Depends(require_actor)) -> dict:
    """Read-only联调登记：声明式凭证位/待实现方法/本地配置态，不联网不伪造。"""
    from . import integrations

    return integrations.describe_all()


@app.get("/workspaces/{workspace_id}/ops/selfcheck")
def ops_selfcheck(actor: Actor = Depends(require_actor)) -> dict:
    """Read-only 本机运维就绪自检（离线，不联网，不伪造连通）。"""
    from .ops_check import run_checks

    return run_checks()


@app.get("/workspaces/{workspace_id}/system/logs")
def system_logs(
    workspace_id: str,
    actor: Actor = Depends(require_owner),
    lines: int = 80,
) -> dict:
    """Read-only 最近运行日志（owner-only，逐行脱敏，仅尾部，不写不删）。"""
    import pathlib

    from .log_view import read_recent_logs

    root = pathlib.Path(__file__).resolve().parent.parent
    return read_recent_logs(root / "logs", max_lines=lines)


@app.get("/workspaces/{workspace_id}/briefing")
def briefing(
    workspace_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
    period: str = "daily",
) -> dict:
    """Read-only 本地经营简报（今日/近7天），金额为本地口径，非平台对账。"""
    from .briefing import summarize

    orders = db.execute(select(Order).where(Order.workspace_id == actor.workspace_id)).scalars().all()
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    products = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id)).scalars().all()
    goals = db.execute(select(Goal).where(Goal.workspace_id == actor.workspace_id)).scalars().all()
    pending_todos = db.execute(
        select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending")
    ).scalars().all()
    pending_approvals = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    ).scalars().all()
    tickets = db.execute(select(Ticket).where(Ticket.workspace_id == actor.workspace_id)).scalars().all()
    return summarize(
        orders,
        customers,
        products,
        pending_todos=len(pending_todos),
        pending_approvals=len(pending_approvals),
        goals=goals,
        period=period if period in ("daily", "weekly") else "daily",
        tickets=tickets,
    )


@app.get("/workspaces/{workspace_id}/analytics/report")
def analytics_report(
    workspace_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
    days: int = 30,
    offset: int = 0,
) -> dict:
    """只读经营报表聚合：按天桶输出订单/本地营收/新增客户，支持任意窗口与偏移（offset=1 即昨日）。金额为本地订单口径，非平台对账。"""
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days or 30), 180))
    offset = max(0, int(offset or 0))
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=offset)
    start = end - timedelta(days=days - 1)

    orders = db.execute(select(Order).where(Order.workspace_id == actor.workspace_id)).scalars().all()
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    products = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id)).scalars().all()
    todos = db.execute(select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending")).scalars().all()
    approvals = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    ).scalars().all()

    def _utc(dt):
        if dt is None:
            return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    buckets = [
        {"date": (start + timedelta(days=i)).date().isoformat(), "orders": 0, "revenue": 0.0, "new_customers": 0}
        for i in range(days)
    ]
    idx = {b["date"]: i for i, b in enumerate(buckets)}
    order_status: dict[str, int] = {}
    for o in orders:
        cd = _utc(getattr(o, "created_at", None))
        if cd is None:
            continue
        key = cd.date().isoformat()
        if key not in idx:
            continue
        buckets[idx[key]]["orders"] += 1
        amt = float(getattr(o, "total_amount", 0) or 0)
        if getattr(o, "status", None) in ("PAID", "COMPLETED", "FULFILLED", "DONE"):
            buckets[idx[key]]["revenue"] += amt
        st = getattr(o, "status", "?") or "?"
        order_status[st] = order_status.get(st, 0) + 1
    for c in customers:
        cd = _utc(getattr(c, "created_at", None))
        if cd is None:
            continue
        key = cd.date().isoformat()
        if key in idx:
            buckets[idx[key]]["new_customers"] += 1

    revenue = round(sum(b["revenue"] for b in buckets), 2)
    low_stock = [
        {"id": p.id, "name": getattr(p, "name", ""), "stock": getattr(p, "stock", 0)}
        for p in products
        if (getattr(p, "stock", 0) or 0) <= 5
    ][:8]
    return {
        "range": {
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "days": days,
            "offset": offset,
            "label": "昨日" if offset == 1 else f"近 {days} 天",
        },
        "buckets": buckets,
        "summary": {
            "orders": sum(b["orders"] for b in buckets),
            "revenue": revenue,
            "new_customers": sum(b["new_customers"] for b in buckets),
            "pending_todos": len(todos),
            "pending_approvals": len(approvals),
            "total_orders": len(orders),
            "total_products": len(products),
            "low_stock_count": len(low_stock),
            "low_stock_items": low_stock,
            "order_status": order_status,
        },
        "caveat": "金额为本地订单口径，非平台对账；真实结算以 Karma 回执为准。",
    }


@app.get("/workspaces/{workspace_id}/analytics/insight")
def analytics_insight(
    workspace_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
    domain: str = "report",
    days: int = 30,
    offset: int = 0,
) -> dict:
    """统一生意域洞察聚合：一句话意图 → 结构化面板数据（指标/趋势/分布/明细）。

    domain: report|orders|finance|products|stock|customers|service|marketing|content|tasks|system
    全部数据来自本工作区本地库，只读、不伪造；金额为本地订单口径。
    """
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days or 30), 180))
    offset = max(0, int(offset or 0))
    now = datetime.now(timezone.utc)
    end = now - timedelta(days=offset)
    start = end - timedelta(days=days - 1)
    ws = actor.workspace_id

    def _utc(dt):
        if dt is None:
            return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _bucket_map(days_, offset_):
        b = [
            {"date": (start + timedelta(days=i)).date().isoformat(), "value": 0.0}
            for i in range(days_)
        ]
        return {x["date"]: x for x in b}, b

    def _load_orders():
        return db.execute(select(Order).where(Order.workspace_id == ws)).scalars().all()

    def _load_customers():
        return db.execute(select(Customer).where(Customer.workspace_id == ws)).scalars().all()

    def _load_products():
        return db.execute(select(Product).where(Product.workspace_id == ws)).scalars().all()

    revenue_status = ("PAID", "COMPLETED", "FULFILLED", "DONE")
    status_cn = {
        "CREATED": "已创建", "PENDING": "待处理", "PAID": "已支付", "SHIPPED": "已发货",
        "FULFILLED": "已完成", "COMPLETED": "已完成", "CANCELLED": "已取消", "REFUNDED": "已退款",
        "DONE": "已完成",
    }

    def _orders_panel():
        orders = _load_orders()
        products = _load_products()
        customers = _load_customers()
        todos = db.execute(select(Todo).where(Todo.workspace_id == ws, Todo.status == "pending")).scalars().all()
        approvals = db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == ws,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
        ).scalars().all()
        bmap, buckets = _bucket_map(days, offset)
        order_status: dict[str, int] = {}
        rev = 0.0
        for o in orders:
            cd = _utc(getattr(o, "created_at", None))
            if cd is None:
                continue
            k = cd.date().isoformat()
            if k in bmap:
                bmap[k]["value"] += 1.0
            st = str(getattr(o, "status", "") or "")
            order_status[status_cn.get(st, st or "未知")] = order_status.get(status_cn.get(st, st or "未知"), 0) + 1
            if st in revenue_status:
                rev += float(getattr(o, "total_amount", 0) or 0)
        new_customers = 0
        for c in customers:
            cd = _utc(getattr(c, "created_at", None))
            if cd and cd.date().isoformat() in bmap:
                new_customers += 1
        low = [p for p in products if (getattr(p, "stock", 0) or 0) <= 5]
        return {
            "kpis": [
                {"label": "订单数", "value": sum(b["value"] for b in buckets), "fmt": "单"},
                {"label": "本地营收", "value": round(rev, 2), "fmt": "¥"},
                {"label": "新增客户", "value": new_customers, "fmt": "位"},
                {"label": "待办/待审批", "value": f"{len(todos)}/{len(approvals)}", "fmt": ""},
            ],
            "trend": {"name": f"{label}订单数（本地口径）", "buckets": buckets},
            "dist": {"name": "订单状态分布", "items": [{"key": k, "value": v} for k, v in order_status.items()]},
            "list": {
                "name": "低库存预警（≤5 件）",
                "cols": [{"key": "name", "label": "商品"}, {"key": "stock", "label": "库存"}],
                "rows": [[getattr(p, "name", "—"), str(getattr(p, "stock", 0))] for p in low[:8]],
            },
            "caveat": "金额为本地订单口径，非平台对账；真实结算以 Karma 回执为准。",
        }

    def _orders():
        orders = _load_orders()
        total = round(sum(float(getattr(o, "total_amount", 0) or 0) for o in orders), 2)
        rev = round(sum(float(getattr(o, "total_amount", 0) or 0) for o in orders if str(getattr(o, "status", "") or "") in revenue_status), 2)
        waiting = [o for o in orders if str(getattr(o, "status", "") or "").upper() == "PAID"]
        order_status: dict[str, int] = {}
        for o in orders:
            st = str(getattr(o, "status", "") or "")
            order_status[status_cn.get(st, st or "未知")] = order_status.get(status_cn.get(st, st or "未知"), 0) + 1
        return {
            "kpis": [
                {"label": "订单总数", "value": len(orders), "fmt": "单"},
                {"label": "订单金额", "value": total, "fmt": "¥"},
                {"label": "成交金额", "value": rev, "fmt": "¥"},
                {"label": "待发货", "value": len(waiting), "fmt": "单"},
            ],
            "trend": None,
            "dist": {"name": "订单状态分布", "items": [{"key": k, "value": v} for k, v in order_status.items()]},
            "list": {
                "name": "最近订单",
                "cols": [{"key": "ref", "label": "单号"}, {"key": "amount", "label": "金额"}, {"key": "status", "label": "状态"}],
                "rows": [
                    [str(getattr(o, "order_reference", "") or getattr(o, "id", "") or "—"),
                     "¥" + format(float(getattr(o, "total_amount", 0) or 0), ",.2f"),
                     status_cn.get(str(getattr(o, "status", "") or ""), str(getattr(o, "status", "") or "—"))]
                    for o in sorted(orders, key=lambda x: _utc(getattr(x, "created_at", None)) or now, reverse=True)[:12]
                ],
            },
            "caveat": "金额为本地订单口径，非平台对账；真实结算以 Karma 回执为准。",
        }

    def _finance():
        orders = _load_orders()
        rev_status = [o for o in orders if str(getattr(o, "status", "") or "") in revenue_status]
        total_rev = round(sum(float(getattr(o, "total_amount", 0) or 0) for o in rev_status), 2)
        n = len(rev_status)
        bmap, buckets = _bucket_map(days, offset)
        for o in rev_status:
            cd = _utc(getattr(o, "created_at", None))
            if cd is None:
                continue
            k = cd.date().isoformat()
            if k in bmap:
                bmap[k]["value"] += float(getattr(o, "total_amount", 0) or 0)
        by_st: dict[str, float] = {}
        for o in rev_status:
            st = str(getattr(o, "status", "") or "")
            by_st[status_cn.get(st, st or "未知")] = by_st.get(status_cn.get(st, st or "未知"), 0) + float(getattr(o, "total_amount", 0) or 0)
        return {
            "kpis": [
                {"label": "本地营收", "value": total_rev, "fmt": "¥"},
                {"label": "成交订单", "value": n, "fmt": "单"},
                {"label": "客单价", "value": round(total_rev / n, 2) if n else 0, "fmt": "¥"},
                {"label": "回款单数", "value": n, "fmt": "单"},
            ],
            "trend": {"name": f"{label}营收走势（本地口径）", "buckets": buckets},
            "dist": {"name": "成交金额按订单状态", "items": [{"key": k, "value": round(v, 2)} for k, v in by_st.items()]},
            "list": {
                "name": "最近成交订单",
                "cols": [{"key": "ref", "label": "单号"}, {"key": "amount", "label": "金额"}, {"key": "status", "label": "状态"}],
                "rows": [
                    [str(getattr(o, "order_reference", "") or getattr(o, "id", "") or "—"),
                     "¥" + format(float(getattr(o, "total_amount", 0) or 0), ",.2f"),
                     status_cn.get(str(getattr(o, "status", "") or ""), str(getattr(o, "status", "") or "—"))]
                    for o in sorted(rev_status, key=lambda x: _utc(getattr(x, "created_at", None)) or now, reverse=True)[:12]
                ],
            },
            "caveat": "金额为本地订单口径，非平台对账；真实结算以 Karma 回执为准。",
        }

    def _products():
        products = _load_products()
        on = [p for p in products if str(getattr(p, "status", "") or "").lower() in ("", "published", "on", "active")]
        low = [p for p in products if (getattr(p, "stock", 0) or 0) <= 5]
        return {
            "kpis": [
                {"label": "商品总数", "value": len(products), "fmt": "款"},
                {"label": "在售", "value": len(on), "fmt": "款"},
                {"label": "低库存(≤5)", "value": len(low), "fmt": "款"},
            ],
            "trend": None,
            "dist": None,
            "list": {
                "name": "商品清单",
                "cols": [{"key": "name", "label": "商品"}, {"key": "price", "label": "价格"}, {"key": "stock", "label": "库存"}],
                "rows": [
                    [getattr(p, "name", "—"), "¥" + format(float(getattr(p, "price", 0) or 0), ",.2f"), str(getattr(p, "stock", 0))]
                    for p in products[:24]
                ],
            },
            "caveat": "产品库为全系统共享数据源：客服问答、营销文案、视频方案与开发均以本库为同源数据。",
        }

    def _stock():
        products = _load_products()
        total = sum(int(getattr(p, "stock", 0) or 0) for p in products)
        low = sorted([p for p in products if (getattr(p, "stock", 0) or 0) <= 5], key=lambda p: int(getattr(p, "stock", 0) or 0))
        return {
            "kpis": [
                {"label": "在库 SKU", "value": len(products), "fmt": "款"},
                {"label": "库存总量", "value": total, "fmt": "件"},
                {"label": "低库存预警", "value": len(low), "fmt": "款"},
            ],
            "trend": None,
            "dist": None,
            "list": {
                "name": "低库存预警清单（≤5 件）",
                "cols": [{"key": "name", "label": "商品"}, {"key": "stock", "label": "库存"}],
                "rows": [[getattr(p, "name", "—"), str(getattr(p, "stock", 0))] for p in low[:12]],
            },
            "caveat": "补货动作经网关 add_stock 留痕；库存为客服问答与营销同源实时读取。",
        }

    def _customers():
        customers = _load_customers()
        stage_cn = {"new": "新增", "lead": "咨询", "negotiate": "详谈", "won": "成交", "repeat": "复购", "lost": "流失"}
        cnt: dict[str, int] = {}
        for c in customers:
            k = stage_cn.get(str(getattr(c, "stage", "") or "").lower(), str(getattr(c, "stage", "") or "未知"))
            cnt[k] = cnt.get(k, 0) + 1
        return {
            "kpis": [
                {"label": k + "客户", "value": cnt.get(k, 0), "fmt": "位"} for k in ("新增", "咨询", "详谈", "成交", "复购")
            ],
            "trend": None,
            "dist": {"name": "客户阶段分布", "items": [{"key": k, "value": v} for k, v in cnt.items()]},
            "list": {
                "name": "客户清单",
                "cols": [{"key": "name", "label": "客户"}, {"key": "stage", "label": "阶段"}, {"key": "source", "label": "来源"}],
                "rows": [
                    [getattr(c, "name", "—") or getattr(c, "external_id", "—"),
                     stage_cn.get(str(getattr(c, "stage", "") or "").lower(), str(getattr(c, "stage", "") or "—")),
                     str(getattr(c, "source", "") or "—")]
                    for c in sorted(customers, key=lambda x: _utc(getattr(x, "created_at", None)) or now, reverse=True)[:16]
                ],
            },
            "caveat": "线索按评分规则分级，SOP 跟进节奏自动生成待办；阶段推进经网关留痕。",
        }

    def _service():
        tickets = db.execute(select(Ticket).where(Ticket.workspace_id == ws)).scalars().all()
        st_cn = {"new": "新工单", "processing": "处理中", "waiting_reply": "等待回复", "closed": "已关闭", "escalated": "已升级"}
        cnt: dict[str, int] = {}
        for t in tickets:
            k = st_cn.get(str(getattr(t, "status", "") or "").lower(), str(getattr(t, "status", "") or "未知"))
            cnt[k] = cnt.get(k, 0) + 1
        open_n = len([t for t in tickets if str(getattr(t, "status", "") or "").lower() != "closed"])
        return {
            "kpis": [
                {"label": "工单总数", "value": len(tickets), "fmt": "单"},
                {"label": "进行中", "value": open_n, "fmt": "单"},
                {"label": "已关闭", "value": cnt.get("已关闭", 0), "fmt": "单"},
                {"label": "已升级", "value": cnt.get("已升级", 0), "fmt": "单"},
            ],
            "trend": None,
            "dist": {"name": "工单状态分布", "items": [{"key": k, "value": v} for k, v in cnt.items()]},
            "list": {
                "name": "最近工单",
                "cols": [{"key": "title", "label": "工单"}, {"key": "status", "label": "状态"}, {"key": "priority", "label": "优先级"}],
                "rows": [
                    [str(getattr(t, "title", "") or getattr(t, "description", "") or "—")[:36],
                     st_cn.get(str(getattr(t, "status", "") or "").lower(), str(getattr(t, "status", "") or "—")),
                     str(getattr(t, "priority", "") or "—")]
                    for t in sorted(tickets, key=lambda x: _utc(getattr(x, "created_at", None)) or now, reverse=True)[:12]
                ],
            },
            "caveat": "工单全程留痕并入哈希链审计；SLA 超时自动盯防；渠道接入为真实凭证位。",
        }

    def _content(kind_any: bool = False):
        items = db.execute(select(ContentItem).where(ContentItem.workspace_id == ws)).scalars().all()
        st_cn = {"draft": "草稿", "proposed": "待拍板", "pending": "待拍板", "approved": "已拍板", "rejected": "已驳回", "published": "已发布"}
        cnt: dict[str, int] = {}
        kind_cnt: dict[str, int] = {}
        for it in items:
            st = str(getattr(it, "status", "") or "").lower()
            cnt[st_cn.get(st, st or "未知")] = cnt.get(st_cn.get(st, st or "未知"), 0) + 1
            kd = "视频" if str(getattr(it, "kind", "") or "").lower() == "video" else "文案"
            kind_cnt[kd] = kind_cnt.get(kd, 0) + 1
        pending = cnt.get("待拍板", 0)
        published = cnt.get("已发布", 0)
        return {
            "kpis": [
                {"label": "内容条目", "value": len(items), "fmt": "条"},
                {"label": "待CEO拍板", "value": pending, "fmt": "条"},
                {"label": "已发布", "value": published, "fmt": "条"},
                {"label": "文案/视频", "value": f"{kind_cnt.get('文案',0)}/{kind_cnt.get('视频',0)}", "fmt": ""},
            ],
            "trend": None,
            "dist": {"name": "内容状态分布", "items": [{"key": k, "value": v} for k, v in cnt.items()]},
            "list": {
                "name": "最近内容条目",
                "cols": [{"key": "title", "label": "标题"}, {"key": "kind", "label": "类型"}, {"key": "status", "label": "状态"}],
                "rows": [
                    [str(getattr(it, "title", "") or "—")[:36],
                     "视频" if str(getattr(it, "kind", "") or "").lower() == "video" else "文案",
                     st_cn.get(str(getattr(it, "status", "") or "").lower(), str(getattr(it, "status", "") or "—"))]
                    for it in sorted(items, key=lambda x: _utc(getattr(x, "created_at", None)) or now, reverse=True)[:12]
                ],
            },
            "caveat": "未 approved 不得发布；发布需真实渠道凭证；CEO 拍板备注与驳回原因全程留痕。",
        }

    def _tasks():
        todos = db.execute(select(Todo).where(Todo.workspace_id == ws, Todo.status == "pending")).scalars().all()
        approvals = db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == ws,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
        ).scalars().all()
        todo_rows = [
            [str(getattr(x, "title", "") or "—")[:40], str(getattr(x, "kind", "") or "—")]
            for x in sorted(todos, key=lambda a: _utc(getattr(a, "created_at", None)) or now)[:12]
        ]
        appr_rows = [
            [str(getattr(x, "action", "") or "—")[:40], str(getattr(x, "risk_level", "") or "—")]
            for x in sorted(approvals, key=lambda a: _utc(getattr(a, "created_at", None)) or now)[:12]
        ]
        return {
            "kpis": [
                {"label": "待办任务", "value": len(todos), "fmt": "项"},
                {"label": "待你拍板", "value": len(approvals), "fmt": "项"},
            ],
            "trend": None,
            "dist": None,
            "list": {
                "name": "待你拍板 + 待办清单",
                "cols": [{"key": "item", "label": "事项"}, {"key": "tag", "label": "类型"}],
                "rows": [
                    *[["拍板 · " + r[0], r[1]] for r in appr_rows],
                    *[["待办 · " + r[0], r[1]] for r in todo_rows],
                ][:16],
            },
            "caveat": "任务落账状态机统一视图；待审批经神经闸决策，重大动作需你授权。",
        }

    def _system():
        out = {"kpis": [{"label": "模型接入", "value": "待配置", "fmt": ""}, {"label": "工作区", "value": "已连接", "fmt": ""}, {"label": "数据库", "value": "未知", "fmt": ""}]}
        try:
            r = db.execute(select(Workspace).where(Workspace.id == ws)).scalars().first()
            if r and r.settings:
                cfg = r.settings.get("model_config") or r.settings.get("modelCfg") or {}
                if cfg.get("api_key") or cfg.get("enabled"):
                    out["kpis"][0]["value"] = "已配置"
        except Exception:
            pass
        try:
            health = db.execute(select(1)).first()
            out["kpis"][2]["value"] = "正常" if health else "异常"
        except Exception:
            out["kpis"][2]["value"] = "异常"
        return {
            **out,
            "trend": None,
            "dist": None,
            "list": {
                "name": "系统状态",
                "cols": [{"key": "k", "label": "项"}, {"key": "v", "label": "值"}],
                "rows": [["工作区数据库", "已同步"], ["规则执行引擎", "待命"], ["AI 模型服务", out["kpis"][0]["value"]], ["Karma 结算", "待接入"]],
            },
            "caveat": "只读探针，不暴露密钥与跨工作区数据；异常时自动进入巡检预案。",
        }

    panels = {
        "report": lambda: {**_orders_panel(), "meta_agent": "主脑·经营分析", "meta_sub": "全生意域总览"},
        "orders": lambda: {**_orders(), "meta_agent": "订单履约 Agent", "meta_sub": "订单与履约"},
        "finance": lambda: {**_finance(), "meta_agent": "财务资金 Agent", "meta_sub": "资金与结算"},
        "products": lambda: {**_products(), "meta_agent": "商品/库存 Agent", "meta_sub": "自营产品库"},
        "stock": lambda: {**_stock(), "meta_agent": "库存 Agent", "meta_sub": "库存与预警"},
        "customers": lambda: {**_customers(), "meta_agent": "智能获客 Agent", "meta_sub": "客户全生命周期"},
        "service": lambda: {**_service(), "meta_agent": "智能客服 Agent", "meta_sub": "工单与接待"},
        "marketing": lambda: {**_content(), "meta_agent": "社交运营 Agent", "meta_sub": "营销与发布"},
        "content": lambda: {**_content(), "meta_agent": "内容三 Agent", "meta_sub": "文案·视频·运营"},
        "tasks": lambda: {**_tasks(), "meta_agent": "CEO 主脑分派", "meta_sub": "任务与审批"},
        "system": lambda: {**_system(), "meta_agent": "系统维护 Agent", "meta_sub": "运行与健康"},
    }
    if domain not in panels:
        return {"domain": domain, "error": f"未知域 {domain}，可选：" + ",".join(sorted(panels)), "meta": {"title": "未知域"}, "kpis": [], "trend": None, "dist": None, "list": None, "caveat": ""}
    label = "昨日" if offset == 1 else (f"近 {days} 天" if days != 1 else "今日")
    p = panels[domain]()
    titles = {
        "report": "经营报表", "orders": "订单成交", "finance": "财务资金", "products": "产品库",
        "stock": "库存总览", "customers": "客户全景", "service": "智能客服", "marketing": "营销内容",
        "content": "内容创作", "tasks": "任务与审批", "system": "系统状态",
    }
    return {
        "domain": domain,
        "meta": {
            "title": titles.get(domain, domain),
            "label": label,
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "days": days,
            "offset": offset,
            "sub": p.pop("meta_sub", ""),
            "agent": p.pop("meta_agent", ""),
        },
        "kpis": p.pop("kpis", []),
        "trend": p.pop("trend", None),
        "dist": p.pop("dist", None),
        "list": p.pop("list", None),
        "caveat": p.pop("caveat", ""),
    }


@app.post("/workspaces/{workspace_id}/briefing/snapshot")
def briefing_snapshot_save(
    workspace_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """把当日 daily 简报手动存档（同日覆盖），本地只读历史，不发外部。"""
    from datetime import datetime, timezone

    from .briefing import summarize
    from .models import BriefingSnapshot

    orders = db.execute(select(Order).where(Order.workspace_id == actor.workspace_id)).scalars().all()
    customers = db.execute(select(Customer).where(Customer.workspace_id == actor.workspace_id)).scalars().all()
    products = db.execute(select(Product).where(Product.workspace_id == actor.workspace_id)).scalars().all()
    goals = db.execute(select(Goal).where(Goal.workspace_id == actor.workspace_id)).scalars().all()
    pending_todos = db.execute(
        select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending")
    ).scalars().all()
    pending_approvals = db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    ).scalars().all()
    tickets = db.execute(select(Ticket).where(Ticket.workspace_id == actor.workspace_id)).scalars().all()
    data = summarize(
        orders, customers, products,
        pending_todos=len(pending_todos),
        pending_approvals=len(pending_approvals),
        goals=goals, period="daily",
        tickets=tickets,
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = db.execute(
        select(BriefingSnapshot).where(
            BriefingSnapshot.workspace_id == actor.workspace_id,
            BriefingSnapshot.snapshot_date == today,
        )
    ).scalars().first()
    if existing:
        existing.payload = data
    else:
        db.add(BriefingSnapshot(workspace_id=actor.workspace_id, snapshot_date=today, period="daily", payload=data))
    db.commit()
    return {"saved": True, "date": today}


@app.get("/workspaces/{workspace_id}/briefing/snapshots")
def briefing_snapshot_list(
    workspace_id: str,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only 最近 30 条经营快照历史（按日期倒序）。"""
    from .models import BriefingSnapshot

    rows = db.execute(
        select(BriefingSnapshot)
        .where(BriefingSnapshot.workspace_id == actor.workspace_id)
        .order_by(BriefingSnapshot.snapshot_date.desc())
        .limit(30)
    ).scalars().all()
    return {
        "snapshots": [
            {
                "id": r.id,
                "date": r.snapshot_date,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "revenue_local": r.payload.get("revenue_local"),
                "orders_in_window": r.payload.get("orders_in_window"),
                "new_customers": r.payload.get("new_customers"),
                "pending_approvals": r.payload.get("pending_approvals"),
                "open_tickets": (r.payload.get("customer_service") or {}).get("open_tickets"),
                "urgent_open": (r.payload.get("customer_service") or {}).get("urgent_open"),
                "satisfaction_avg": (r.payload.get("customer_service") or {}).get("satisfaction_avg"),
            }
            for r in rows
        ]
    }

def _commerce_policy_out(policy: CommercePolicy) -> dict:
    return {
        "auto_accept_orders": policy.auto_accept_orders,
        "max_discount_percent": policy.max_discount_percent,
        "approval_threshold_amount": policy.approval_threshold_amount,
        "currency": policy.currency,
        "shipping_fee": policy.shipping_fee,
        "payment_method": policy.payment_method,
        "terms": policy.terms,
    }


def _get_or_create_commerce_policy(db: Session, workspace_id: str) -> CommercePolicy:
    policy = db.execute(
        select(CommercePolicy).where(CommercePolicy.workspace_id == workspace_id)
    ).scalars().first()
    if policy is None:
        policy = CommercePolicy(workspace_id=workspace_id)
        db.add(policy)
        db.commit()
        db.refresh(policy)
    return policy


@app.get("/workspaces/{workspace_id}/commerce")
def get_commerce_policy(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    policy = _get_or_create_commerce_policy(db, actor.workspace_id)
    return _commerce_policy_out(policy)


@app.put("/workspaces/{workspace_id}/commerce")
def put_commerce_policy(payload: schemas.CommercePolicyUpdate, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    policy = _get_or_create_commerce_policy(db, actor.workspace_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return _commerce_policy_out(policy)


@app.get("/workspaces/{workspace_id}/storefront")
def storefront(actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    """Machine-readable storefront for AI buyers and partners."""
    policy = _get_or_create_commerce_policy(db, actor.workspace_id)
    merchant = db.execute(
        select(MerchantProfile).where(MerchantProfile.workspace_id == actor.workspace_id)
    ).scalars().first()
    products = db.execute(
        select(Product).where(Product.workspace_id == actor.workspace_id, Product.status == "published")
    ).scalars().all()
    return {
        "merchant": {
            "shop_name": merchant.shop_name if merchant else None,
            "category": merchant.category if merchant else None,
            "region": merchant.region if merchant else None,
        },
        "policy": _commerce_policy_out(policy),
        "payment": {"method": policy.payment_method},
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "currency": policy.currency,
                "stock": p.stock,
                "photo_url": p.photo_url,
                "specs": p.specs,
                "detail": p.detail,
                "qualifications": p.qualifications,
            }
            for p in products
        ],
        "capabilities": ["quote", "order", "fulfill", "refund", "support"],
        "contact": {"channel": "chat_inbound", "note": "通过聊天工具或 inbound webhook 下单/询价"},
    }


@app.post("/chat/inbound")
async def chat_inbound(payload: schemas.ChatInbound, request: Request, db: Session = Depends(get_db)) -> dict:
    from . import crypto
    from .inbound import process_inbound
    from .ratelimit import limiter_inbound

    client_ip = request.client.host if request.client else "unknown"
    if not limiter_inbound.allow(f"inbound:{client_ip}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    if not payload.token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")

    from . import token_index

    ws = token_index.resolve(db, payload.token)
    if ws is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or disabled chat token")
    return await process_inbound(
        db,
        ws,
        channel=payload.channel,
        external_id=payload.external_id,
        message=payload.message,
        customer_name=payload.customer_name,
    )


@app.post("/workspaces/{workspace_id}/chat-token/rotate")
def rotate_chat_token(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    """自助接入：生成/重置聊天接入 Token（owner-only）。

    新 Token 立即生效并加入 O(1) 索引；明文只在本次响应返回一次，
    后续 GET /settings 只回掩码。旧 Token 同时失效（哈希被替换）。
    """
    ws = db.get(Workspace, actor.workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    from . import crypto, token_index

    new_token = "krama_" + secrets.token_urlsafe(24)
    settings = dict(ws.settings or {})
    chat_cfg = dict(settings.get("chat") or {})
    chat_cfg["token_hash"] = crypto.hash_value(new_token)
    chat_cfg["token_enc"] = crypto.encrypt(new_token)
    chat_cfg.pop("token", None)
    chat_cfg["enabled"] = True
    settings["chat"] = chat_cfg
    ws.settings = settings
    token_index.upsert(db, ws.id, chat_cfg["token_hash"])
    db.commit()
    return {"token": new_token, "enabled": True}


@app.get("/chat/poll")
async def chat_poll(
    token: str, external_id: str, after: str = "",
    request: Request = None, db: Session = Depends(get_db),
) -> dict:
    """挂件出站轮询：读某客户会话在 after 时间之后的 AI 回复（chat-token 鉴权）。"""
    from . import token_index
    from .ratelimit import limiter_inbound

    client_ip = request.client.host if request else "unknown"
    if not limiter_inbound.allow(f"poll:{client_ip}:{external_id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    if not token or not external_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing token/external_id")
    ws = token_index.resolve(db, token)
    if ws is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or disabled chat token")
    convs = db.execute(
        select(Conversation).where(
            Conversation.workspace_id == ws.id,
            Conversation.external_id == external_id,
        )
    ).scalars().all()
    out: list[dict] = []
    for conv in convs:
        q = select(Message).where(
            Message.conversation_id == conv.id, Message.direction == "out"
        ).order_by(Message.created_at)
        if after:
            q = q.where(Message.created_at > after)
        for m in db.execute(q).scalars().all():
            out.append({
                "id": m.id,
                "content": m.content,
                "created_at": m.created_at.isoformat(timespec="seconds") if m.created_at else None,
            })
    return {"messages": out}



async def _webhook(provider: str, request: Request, db: Session, token: str = "") -> dict:
    from .ratelimit import limiter_inbound
    from .webhooks import handle

    client_ip = request.client.host if request.client else "unknown"
    if not limiter_inbound.allow(f"webhook:{client_ip}:{provider}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 - platform may send non-JSON bodies
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    header_map = {k.lower(): v for k, v in request.headers.items()}
    start = time.perf_counter()
    try:
        result = await handle(db, provider, payload, token=token, raw_body=raw_body, headers=header_map)
    except Exception:
        record_webhook(provider, "", status="error", duration_ms=(time.perf_counter() - start) * 1000)
        raise
    record_webhook(provider, "", status="ok", duration_ms=(time.perf_counter() - start) * 1000)
    return result


@app.post("/webhooks/{provider}/{token}")
async def chat_webhook_token(provider: str, token: str, request: Request, db: Session = Depends(get_db)) -> dict:
    return await _webhook(provider, request, db, token=token)


@app.post("/webhooks/{provider}")
async def chat_webhook(provider: str, request: Request, db: Session = Depends(get_db)) -> dict:
    return await _webhook(provider, request, db, token=request.query_params.get("token", ""))


@app.post("/workspaces/{workspace_id}/webhooks/ecommerce")
async def ecommerce_webhook(workspace_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """独立站订单推送接收端（Shopify / WooCommerce / 通用 JSON）。

    独立站把订单/支付/发货事件推到这里，系统归一化后幂等落库到本地订单，
    与产品库/客户/库存联动。验签规则：
      - 配置了 webhook_secret 时要求 X-Krama-Signature（sha256=<hex>，HMAC 原文 body）；
      - Shopify 专用头 X-Shopify-Hmac-SHA256 也按同一 secret 校验。
    未配置 secret 视为开放接收（提示补验签，不阻断业务）。
    """
    from .connectors import normalize_webhook_order, verify_webhook_signature
    from .ratelimit import limiter_inbound
    from . import crypto

    client_ip = request.client.host if request.client else "unknown"
    if not limiter_inbound.allow(f"ecomwh:{client_ip}:{workspace_id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        payload = {}
    header_map = {k.lower(): v for k, v in request.headers.items()}

    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    ecom_cfg = (ws.settings or {}).get("ecommerce") or {}
    secret = ""
    if ecom_cfg.get("webhook_secret_enc"):
        try:
            secret = crypto.decrypt(ecom_cfg["webhook_secret_enc"])
        except Exception:  # noqa: BLE001
            secret = ""
    else:
        secret = str(ecom_cfg.get("webhook_secret") or "")
    shopify_sig = str(header_map.get("x-shopify-hmac-sha256") or "")
    sig_header = shopify_sig or str(header_map.get("x-krama-signature") or "")
    if secret and not verify_webhook_signature(secret, raw_body, sig_header or None):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")

    platform = str(ecom_cfg.get("platform") or "generic_webhook").lower()
    if platform not in ("shopify", "woocommerce"):
        platform = "generic_webhook"
        if isinstance(payload, dict) and payload.get("orders") is not None:
            platform = "shopify"
        elif isinstance(payload, dict) and payload.get("id") is not None and payload.get("line_items") is not None:
            platform = "woocommerce"

    normalized = normalize_webhook_order(platform, payload)
    created_ids: list[str] = []
    skipped: list[dict] = []
    rows = normalized["orders"] if isinstance(normalized, dict) and "orders" in normalized else [normalized]
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider_ref = str(row.get("provider_reference") or "").strip()
        if not provider_ref:
            skipped.append({"reason": "no provider_reference", "row": row})
            continue
        existing = db.execute(
            select(Order).where(
                Order.workspace_id == workspace_id,
                Order.provider_reference == provider_ref,
            )
        ).scalars().first()
        if existing is not None:
            skipped.append({"reason": "duplicate", "provider_reference": provider_ref, "order_id": existing.id})
            continue
        order = Order(
            workspace_id=workspace_id,
            channel=row.get("channel") or "independent",
            order_reference=provider_ref,
            provider_reference=provider_ref,
            status=row.get("status") or OrderStatus.CREATED.value,
            payment_status=row.get("payment_status") or "paid",
            total_amount=row.get("total_amount") or 0.0,
            currency=row.get("currency") or "USD",
            customer=row.get("customer") or {},
            items=row.get("items") or [],
        )
        db.add(order)
        db.flush()
        created_ids.append(order.id)
        # 客户建档（按 provider email 幂等）
        cust = (row.get("customer") or {})
        if cust.get("email"):
            external_id = "webhook:" + str(cust["email"])
            existing_cust = db.execute(
                select(Customer).where(
                    Customer.workspace_id == workspace_id,
                    Customer.channel == order.channel,
                    Customer.external_id == external_id,
                )
            ).scalars().first()
            if existing_cust is None:
                db.add(Customer(
                    workspace_id=workspace_id,
                    channel=order.channel,
                    external_id=external_id,
                    name=cust.get("name") or cust.get("email") or "",
                    profile={"email": cust.get("email") or "", "phone": cust.get("phone") or "",
                             "note": f"独立站{platform}推送建档"},
                    stage="lead",
                ))
        audit_record(db, workspace_id=workspace_id, actor_id="webhook", action="order_imported",
                     risk_level="R1", decision=Decision.ALLOWED,
                     resource=f"order:{order.id}", reason=f"独立站推送订单落库：{provider_ref}",
                     evidence={"platform": platform, "provider_reference": provider_ref}, data_tier="INTERNAL")
    db.commit()
    return {
        "ok": True,
        "platform": platform,
        "created": created_ids,
        "skipped": skipped,
        "signature_verified": bool(secret),
        "note": "已按 provider_reference 幂等落库；独立站配置 webhook_secret 可启用验签",
    }


@app.post("/gateway/execute")
async def gateway_execute(payload: schemas.ExecuteRequest, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    from .ratelimit import limiter_execute

    if not limiter_execute.allow(f"{actor.user.id}:{actor.workspace_id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "执行频率超限，请稍后再试")
    start = time.perf_counter()
    try:
        result: GatewayResult = await execute(db, actor, payload.tool, payload.params, payload.idempotency_key)
    except GatewayError as exc:
        record_tool(payload.tool, actor.workspace_id, status="error", duration_ms=(time.perf_counter() - start) * 1000)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    record_tool(payload.tool, actor.workspace_id, status="ok", duration_ms=(time.perf_counter() - start) * 1000)
    return {
        "decision": result.decision.value,
        "approval_id": result.approval_id,
        "execution_id": result.execution_id,
        "result": result.result,
        "reason": result.reason,
    }


@app.post("/workspaces/{workspace_id}/mcp")
async def mcp_rpc(
    request: Request,
    actor: Actor = Depends(require_actor),
    db: Session = Depends(get_db),
) -> dict | list:
    """MCP-compatible JSON-RPC 2.0 surface (initialize/ping/tools/list/tools/call).

    Every tools/call still goes through the unified gateway; this route only
    speaks the JSON-RPC envelope and never grants permissions directly.
    """
    import json as _json

    from .mcp_server import handle_request

    try:
        body = await request.json()
    except _json.JSONDecodeError:
        return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None}

    if isinstance(body, list):
        return [await handle_request(db, actor, item) for item in body]
    return await handle_request(db, actor, body)


@app.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, payload: schemas.ApprovalDecision, actor: Actor = Depends(require_actor), db: Session = Depends(get_db)) -> dict:
    try:
        req = approve(db, actor, approval_id) if payload.approve else reject(db, actor, approval_id)
    except GatewayError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"id": req.id, "status": req.status, "action": req.action}


# ---------------------------------------------------------------------------
# Backup & recovery (plan M10). Instance-level SQLite snapshots, owner-gated.
# ---------------------------------------------------------------------------
@app.get("/backups")
def backups_list(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    rows = list_backups(db)
    return {
        "backups": [
            {
                "id": b.id,
                "filename": b.filename,
                "size_bytes": b.size_bytes,
                "checksum": b.checksum,
                "reason": b.reason,
                "created_by": b.created_by,
                "status": b.status,
                "restored_at": b.restored_at.isoformat() if b.restored_at else None,
                "created_at": b.created_at.isoformat(),
            }
            for b in rows
        ]
    }


@app.post("/backups", status_code=201)
def backups_create(
    payload: schemas.BackupCreate | None = None,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    try:
        backup = create_backup(db, actor, reason=(payload.reason if payload else "") or "manual")
    except BackupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "id": backup.id,
        "filename": backup.filename,
        "size_bytes": backup.size_bytes,
        "checksum": backup.checksum,
        "reason": backup.reason,
        "created_at": backup.created_at.isoformat(),
    }


@app.post("/backups/{backup_id}/restore")
def backups_restore(
    backup_id: str,
    payload: schemas.BackupRestore,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return restore_backup(db, actor, backup_id, confirm=payload.confirm)
    except BackupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@app.post("/backups/prune")
def backups_prune(actor: Actor = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    return {"pruned": prune_backups(db)}


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str) -> FileResponse:
    """Serve the new console SPA for any unmatched GET path so a stray URL char still opens the app."""
    r = FileResponse(_STATIC_DIR / "home.html")
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r
