"""Built-in tool catalog and the execution context passed to handlers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AgentMemory,
    CommercePolicy,
    Conversation,
    Customer,
    Goal,
    LayoutArea,
    Message,
    Order,
    OrderStatus,
    Product,
    RiskLevel,
    Script,
    Shipment,
    ShipmentStatus,
    StockMovement,
    Task,
    Todo,
    Workspace,
)
from .security import Actor


@dataclass
class ToolContext:
    db: Session
    actor: Actor
    params: dict
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)


Handler = Callable[[ToolContext], Awaitable[dict] | dict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk_level: RiskLevel
    side_effect: bool
    idempotent: bool
    handler: Handler

    @property
    def requires_approval(self) -> bool:
        return int(self.risk_level.value[1]) >= 3


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())


# --- deterministic input parsing -------------------------------------------
# Bad user/agent input must return a readable {"error": ...} instead of
# raising a native KeyError/ValueError/TypeError that the gateway only wraps
# into an opaque message.

PRODUCT_STATUSES = {"draft", "published", "archived"}
CUSTOMER_STAGES = {"new", "lead", "won", "lost"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_money(value: Any) -> tuple[float | None, str | None]:
    """Accept int/float/numeric string; reject negatives and non-numbers."""
    if value is None or value == "":
        return 0.0, None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None, f"price must be a number, got {value!r}"
    if amount < 0:
        return None, f"price must be >= 0, got {amount}"
    return amount, None


def _parse_positive_int(value: Any, field: str) -> tuple[int | None, str | None]:
    """Accept int/numeric string; reject missing, non-numeric and non-positive."""
    if value is None or value == "":
        return None, f"{field} is required"
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None, f"{field} must be a whole number, got {value!r}"
    if n <= 0:
        return None, f"{field} must be a positive number, got {n}"
    return n, None


# --- handlers ---------------------------------------------------------------


async def _list_products(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Product).where(Product.workspace_id == ctx.actor.workspace_id)
    ).scalars().all()
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
                "specs": p.specs,
                "detail": p.detail,
                "qualifications": p.qualifications,
                "layout_area_id": p.layout_area_id,
            }
            for p in rows
        ]
    }


async def _create_product_draft(ctx: ToolContext) -> dict:
    layout_area_id = ctx.params.get("layout_area_id") or None
    name = _clean_text(ctx.params.get("name"))
    if not name:
        return {"error": "name is required"}
    price, price_err = _parse_money(ctx.params.get("price", 0.0))
    if price_err:
        return {"error": price_err}
    product = Product(
        workspace_id=ctx.actor.workspace_id,
        name=name,
        category=_clean_text(ctx.params.get("category")) or "general",
        price=price,
        status="draft",
        photo_url=ctx.params.get("photo_url", "") or "",
        specs=ctx.params.get("specs") or {},
        detail=ctx.params.get("detail", "") or "",
        qualifications=ctx.params.get("qualifications") or [],
        layout_area_id=layout_area_id,
    )
    ctx.db.add(product)
    ctx.db.flush()
    return {
        "product_id": product.id,
        "name": product.name,
        "price": product.price,
        "status": "draft",
        "category": product.category,
        "photo_url": product.photo_url,
        "specs": product.specs,
        "detail": product.detail,
        "qualifications": product.qualifications,
    }


async def _update_product(ctx: ToolContext) -> dict:
    product = _find_product(ctx, ctx.params)
    if product is None:
        return {"error": "product not found in workspace"}
    updatable = ("name", "category", "price", "status", "photo_url", "specs", "detail", "qualifications", "layout_area_id")
    for field in updatable:
        if field not in ctx.params:
            continue
        value = ctx.params[field]
        if field == "price":
            amount, err = _parse_money(value)
            if err:
                return {"error": err}
            product.price = amount
        elif field == "status":
            if value not in PRODUCT_STATUSES:
                return {"error": f"status must be one of {sorted(PRODUCT_STATUSES)}, got {value!r}"}
            product.status = value
        elif field == "name":
            cleaned = _clean_text(value)
            if not cleaned:
                return {"error": "name must not be empty"}
            product.name = cleaned
        elif field == "specs":
            product.specs = value if isinstance(value, dict) else {}
        elif field == "qualifications":
            product.qualifications = value if isinstance(value, list) else []
        elif field == "layout_area_id":
            product.layout_area_id = value or None
        else:
            setattr(product, field, value)
    ctx.db.flush()
    return {
        "product_id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "photo_url": product.photo_url,
        "specs": product.specs,
        "detail": product.detail,
        "qualifications": product.qualifications,
        "layout_area_id": product.layout_area_id,
    }


async def _add_layout_area(ctx: ToolContext) -> dict:
    stage = ctx.params.get("stage") or "live"
    if stage not in ("test", "live"):
        stage = "live"
    name = ctx.params["name"]
    parent_id = ctx.params.get("parent_id") or None
    existing = ctx.db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == ctx.actor.workspace_id,
            LayoutArea.name == name,
            LayoutArea.parent_id == parent_id,
        )
    ).scalars().first()
    if existing is not None:
        return {"area_id": existing.id, "name": existing.name, "parent_id": existing.parent_id, "stage": existing.stage, "reused": True}
    area = LayoutArea(
        workspace_id=ctx.actor.workspace_id,
        name=name,
        kind=ctx.params.get("kind", "module"),
        target_type=ctx.params.get("target_type", ""),
        target_id=ctx.params.get("target_id", ""),
        parent_id=parent_id,
        config=ctx.params.get("config", {}),
        sort_order=int(ctx.params.get("sort_order", 0)),
        stage=stage,
    )
    ctx.db.add(area)
    ctx.db.flush()
    return {"area_id": area.id, "name": area.name, "parent_id": area.parent_id, "stage": area.stage}


async def _list_layout_areas(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(LayoutArea).where(LayoutArea.workspace_id == ctx.actor.workspace_id).order_by(LayoutArea.sort_order)
    ).scalars().all()
    return {
        "areas": [
            {"id": a.id, "name": a.name, "kind": a.kind, "target_type": a.target_type, "parent_id": a.parent_id, "enabled": a.enabled, "stage": a.stage}
            for a in rows
        ]
    }


async def _link_products_to_area(ctx: ToolContext) -> dict:
    product_ids = ctx.params.get("product_ids") or []
    area_id = ctx.params.get("layout_area_id")
    if not area_id:
        return {"error": "layout_area_id is required"}
    area = ctx.db.get(LayoutArea, area_id)
    if area is None or area.workspace_id != ctx.actor.workspace_id:
        return {"error": "layout area not found in workspace"}
    linked = 0
    for product_id in product_ids:
        product = ctx.db.get(Product, product_id)
        if product is None or product.workspace_id != ctx.actor.workspace_id:
            continue
        product.layout_area_id = area_id
        linked += 1
    return {"linked": linked, "layout_area_id": area_id}


async def _generate_marketing_draft(ctx: ToolContext) -> dict:
    from .llm import generate, workspace_model_config

    text = generate(
        prompt=ctx.params.get("prompt", ""),
        context=ctx.params.get("context", ""),
        model_config=workspace_model_config(ctx.db, ctx.actor.workspace_id),
    )
    return {"draft": text, "status": "draft"}


async def _publish_product(ctx: ToolContext) -> dict:
    product_id = ctx.params.get("product_id")
    if not product_id:
        return {"error": "product_id is required"}
    product = ctx.db.get(Product, product_id)
    if product is None or product.workspace_id != ctx.actor.workspace_id:
        return {"error": "product not found in workspace"}
    product.status = "published"
    return {"product_id": product.id, "status": "published"}


async def _create_order(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    result = await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type=ctx.params.get("channel", "ecommerce_independent"),
        action="create_order",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )
    order_reference = result.get("order_reference")
    if order_reference and not result.get("error"):
        items = ctx.params.get("items") or []
        if not items:
            top_name = ctx.params.get("product_name") or ctx.params.get("name") or ctx.params.get("product")
            if top_name or ctx.params.get("product_id"):
                items = [{
                    "product_id": ctx.params.get("product_id"),
                    "name": top_name,
                    "quantity": ctx.params.get("quantity") or ctx.params.get("qty") or 1,
                    "unit_price": ctx.params.get("unit_price") or ctx.params.get("price"),
                }]
        normalized: list[dict] = []
        warnings: list[str] = []
        total = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            qty_raw = item.get("quantity") or item.get("qty") or 1
            quantity, qty_err = _parse_positive_int(qty_raw, "quantity")
            if qty_err:
                warnings.append(f"skipped a line: {qty_err}")
                continue
            item_name = item.get("name") or item.get("product_name") or item.get("product")
            product = None
            if item.get("product_id"):
                product = ctx.db.get(Product, item.get("product_id"))
                if product is not None and product.workspace_id != ctx.actor.workspace_id:
                    product = None
            if product is None and item_name:
                product = ctx.db.execute(
                    select(Product).where(
                        Product.workspace_id == ctx.actor.workspace_id,
                        Product.name == item_name,
                    )
                ).scalars().first()
            if product is not None:
                unit_price, price_err = _parse_money(item.get("unit_price") or item.get("price") or product.price)
                if price_err:
                    warnings.append(f"skipped {product.name}: {price_err}")
                    continue
                name = item_name or product.name
                normalized.append({"product_id": product.id, "name": name, "quantity": quantity, "unit_price": unit_price})
            else:
                unit_price, price_err = _parse_money(item.get("unit_price") or item.get("price") or 0.0)
                if price_err:
                    warnings.append(f"skipped a line: {price_err}")
                    continue
                normalized.append({"product_id": item.get("product_id") or "", "name": item_name or "", "quantity": quantity, "unit_price": unit_price})
                if not item_name:
                    warnings.append("a line has no product name or id; recorded at 0 price")
            total += quantity * unit_price
        if not normalized:
            # The channel already created the order (order_reference present);
            # we only enrich the local record when valid items are supplied.
            # Bad explicit lines are surfaced as warnings instead of blocking.
            if warnings:
                result["warnings"] = warnings
            return result
        order = Order(
            workspace_id=ctx.actor.workspace_id,
            channel=result.get("channel") or result.get("connector_type") or "ecommerce_independent",
            order_reference=order_reference,
            status=OrderStatus.CREATED.value,
            total_amount=total,
            currency=ctx.params.get("currency", "CNY"),
            customer=ctx.params.get("customer") or {},
            items=normalized,
        )
        ctx.db.add(order)
        ctx.db.flush()
        result["order_id"] = order.id
        if warnings:
            result["warnings"] = warnings
        customer_id = ctx.params.get("customer_id")
        customer_name = ctx.params.get("customer_name") or ctx.params.get("buyer_name")
        customer_obj = ctx.params.get("customer")
        if isinstance(customer_obj, dict):
            customer_name = customer_name or customer_obj.get("name")
        if not customer_id and customer_name:
            channel = result.get("channel") or result.get("connector_type") or "ecommerce_independent"
            external_id = "order-name:" + str(customer_name)
            customer = ctx.db.execute(
                select(Customer).where(
                    Customer.workspace_id == ctx.actor.workspace_id,
                    Customer.channel == channel,
                    Customer.external_id == external_id,
                )
            ).scalars().first()
            if customer is None:
                customer = Customer(
                    workspace_id=ctx.actor.workspace_id,
                    channel=channel,
                    external_id=external_id,
                    name=str(customer_name),
                    profile={},
                )
                ctx.db.add(customer)
                ctx.db.flush()
            customer_id = customer.id
        if customer_id:
            customer = ctx.db.get(Customer, customer_id)
            if customer is not None and customer.workspace_id == ctx.actor.workspace_id:
                customer.stage = "won"
                if customer_name and not customer.name:
                    customer.name = str(customer_name)
        ctx.db.flush()
    return result


async def _make_payment(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    result = await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type="karma_payment",
        action="pay",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )
    if result.get("receipt_status") == "SUCCEEDED" and not result.get("error"):
        order_id = ctx.params.get("order_id")
        if order_id:
            order = ctx.db.get(Order, order_id)
            if order is not None and order.workspace_id == ctx.actor.workspace_id:
                order.status = OrderStatus.PAID.value
                order.payment_status = "paid"
                order.provider_reference = result.get("provider_reference") or order.provider_reference
                result["order_id"] = order.id
    return result


async def _make_refund(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    result = await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type="karma_payment",
        action="refund",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )
    if result.get("receipt_status") == "SUCCEEDED" and not result.get("error"):
        reference = result.get("provider_reference") or ctx.params.get("provider_reference")
        if reference:
            order = ctx.db.execute(
                select(Order).where(
                    Order.workspace_id == ctx.actor.workspace_id,
                    Order.provider_reference == reference,
                )
            ).scalars().first()
            if order is not None:
                order.status = OrderStatus.REFUNDED.value
                order.payment_status = "refunded"
                result["order_id"] = order.id
    return result


async def _reconcile_payment(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    return await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type="karma_payment",
        action="reconcile",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )


async def _dispute_payment(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    return await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type="karma_payment",
        action="dispute",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )


async def _cancel_order(ctx: ToolContext) -> dict:
    from .connectors import execute_connector_action

    result = await execute_connector_action(
        db=ctx.db,
        workspace_id=ctx.actor.workspace_id,
        connector_type=ctx.params.get("channel", "ecommerce_independent"),
        action="cancel_order",
        payload=ctx.params,
        idempotency_key=ctx.idempotency_key,
    )
    if result.get("receipt_status") == "SUCCEEDED" and not result.get("error"):
        reference = result.get("order_reference") or ctx.params.get("order_reference")
        if reference:
            order = ctx.db.execute(
                select(Order).where(
                    Order.workspace_id == ctx.actor.workspace_id,
                    Order.order_reference == reference,
                )
            ).scalars().first()
            if order is not None:
                order.status = OrderStatus.CANCELLED.value
                result["order_id"] = order.id
    return result


async def _bulk_update_prices(ctx: ToolContext) -> dict:
    delta = float(ctx.params.get("delta", 0.0))
    rows = ctx.db.execute(
        select(Product).where(Product.workspace_id == ctx.actor.workspace_id)
    ).scalars().all()
    count = 0
    for product in rows:
        product.price = max(0.0, product.price + delta)
        count += 1
    return {"updated": count}


async def _workspace_summary(ctx: ToolContext) -> dict:
    ws = ctx.db.get(Workspace, ctx.actor.workspace_id)
    task_count = len(
        ctx.db.execute(select(Task).where(Task.workspace_id == ctx.actor.workspace_id)).scalars().all()
    )
    return {"workspace": ws.name if ws else "", "region": ws.region if ws else "", "tasks": task_count}


async def _list_orders(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Order).where(Order.workspace_id == ctx.actor.workspace_id).order_by(Order.created_at.desc())
    ).scalars().all()
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
                "items": o.items,
            }
            for o in rows
        ]
    }


async def _list_inventory(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Product).where(Product.workspace_id == ctx.actor.workspace_id).order_by(Product.created_at)
    ).scalars().all()
    return {
        "inventory": [
            {"product_id": p.id, "name": p.name, "stock": p.stock, "price": p.price, "status": p.status}
            for p in rows
        ]
    }


async def _list_shipments(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Shipment).where(Shipment.workspace_id == ctx.actor.workspace_id).order_by(Shipment.created_at.desc())
    ).scalars().all()
    return {
        "shipments": [
            {"id": s.id, "order_id": s.order_id, "carrier": s.carrier, "tracking_number": s.tracking_number, "status": s.status}
            for s in rows
        ]
    }


async def _add_stock(ctx: ToolContext) -> dict:
    product = ctx.db.get(Product, ctx.params.get("product_id"))
    if product is None or product.workspace_id != ctx.actor.workspace_id:
        return {"error": "product not found in workspace"}
    quantity, err = _parse_positive_int(ctx.params.get("quantity"), "quantity")
    if err:
        return {"error": err}
    product.stock += quantity
    ctx.db.add(StockMovement(workspace_id=ctx.actor.workspace_id, product_id=product.id, delta=quantity, reason="receive"))
    ctx.db.flush()
    return {"product_id": product.id, "name": product.name, "stock": product.stock, "added": quantity}


async def _fulfill_order(ctx: ToolContext) -> dict:
    order_id = ctx.params.get("order_id")
    if not order_id:
        return {"error": "order_id is required"}
    order = ctx.db.get(Order, order_id)
    if order is None or order.workspace_id != ctx.actor.workspace_id:
        return {"error": "order not found in workspace"}
    if order.status != OrderStatus.PAID.value:
        return {"error": f"order must be PAID to ship, current status {order.status}"}
    for item in order.items or []:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        item_name = item.get("name") or item.get("product_name")
        quantity, qty_err = _parse_positive_int(item.get("quantity") or item.get("qty") or 0, "quantity")
        if qty_err or quantity is None:
            continue
        product = None
        if product_id:
            product = ctx.db.get(Product, product_id)
            if product is not None and product.workspace_id != ctx.actor.workspace_id:
                product = None
        if product is None and item_name:
            product = ctx.db.execute(
                select(Product).where(
                    Product.workspace_id == ctx.actor.workspace_id,
                    Product.name == item_name,
                )
            ).scalars().first()
        if product is None:
            continue
        if product.stock < quantity:
            return {"error": f"insufficient stock for {product.name}: need {quantity}, have {product.stock}"}
        product.stock -= quantity
        ctx.db.add(StockMovement(workspace_id=ctx.actor.workspace_id, product_id=product.id, order_id=order.id, delta=-quantity, reason="ship"))
    shipment = Shipment(
        workspace_id=ctx.actor.workspace_id,
        order_id=order.id,
        carrier=ctx.params.get("carrier", ""),
        tracking_number=ctx.params.get("tracking_number", ""),
    )
    ctx.db.add(shipment)
    order.status = OrderStatus.SHIPPED.value
    ctx.db.flush()
    return {"order_id": order.id, "shipment_id": shipment.id, "status": order.status}


async def _mark_delivered(ctx: ToolContext) -> dict:
    shipment_id = ctx.params.get("shipment_id")
    order_id = ctx.params.get("order_id")
    if not shipment_id and not order_id:
        return {"error": "shipment_id or order_id is required"}
    shipment = None
    if shipment_id:
        shipment = ctx.db.get(Shipment, shipment_id)
    elif order_id:
        shipment = ctx.db.execute(
            select(Shipment)
            .where(Shipment.workspace_id == ctx.actor.workspace_id, Shipment.order_id == order_id)
            .order_by(Shipment.created_at.desc())
        ).scalars().first()
    if shipment is None or shipment.workspace_id != ctx.actor.workspace_id:
        return {"error": "shipment not found in workspace"}
    shipment.status = ShipmentStatus.DELIVERED.value
    shipment.delivered_at = _now()
    order = ctx.db.get(Order, shipment.order_id)
    if order is not None and order.workspace_id == ctx.actor.workspace_id:
        order.status = OrderStatus.DELIVERED.value
    return {"shipment_id": shipment.id, "order_id": shipment.order_id, "status": shipment.status}


def _find_customer(ctx: ToolContext, params: dict) -> Customer | None:
    customer_id = params.get("customer_id")
    if customer_id:
        customer = ctx.db.get(Customer, customer_id)
        if customer is not None and customer.workspace_id == ctx.actor.workspace_id:
            return customer
    channel = params.get("channel") or "chat"
    external_id = str(params.get("external_id") or "")
    if external_id:
        return ctx.db.execute(
            select(Customer).where(
                Customer.workspace_id == ctx.actor.workspace_id,
                Customer.channel == channel,
                Customer.external_id == external_id,
            )
        ).scalars().first()
    return None


def _find_product(ctx: ToolContext, params: dict) -> Product | None:
    product_id = params.get("product_id")
    if product_id:
        product = ctx.db.get(Product, product_id)
        if product is not None and product.workspace_id == ctx.actor.workspace_id:
            return product
    name = params.get("product_name") or params.get("name")
    if name:
        return ctx.db.execute(
            select(Product).where(Product.workspace_id == ctx.actor.workspace_id, Product.name == name)
        ).scalars().first()
    return None


def _model_text(ctx: ToolContext, prompt: str, fallback: str) -> str:
    from .llm import generate, workspace_model_config

    try:
        text = generate(prompt=prompt, context="", model_config=workspace_model_config(ctx.db, ctx.actor.workspace_id)).strip()
    except Exception:  # noqa: BLE001 - model generation is best-effort
        text = ""
    return text or fallback


async def _upsert_customer(ctx: ToolContext) -> dict:
    channel = _clean_text(ctx.params.get("channel")) or "chat"
    external_id = str(ctx.params.get("external_id") or "").strip()
    if not external_id:
        return {"error": "external_id is required"}
    profile_in = ctx.params.get("profile")
    profile_in = profile_in if isinstance(profile_in, dict) else {}
    name = _clean_text(ctx.params.get("name"))
    stage = ctx.params.get("stage")
    customer = ctx.db.execute(
        select(Customer).where(
            Customer.workspace_id == ctx.actor.workspace_id,
            Customer.channel == channel,
            Customer.external_id == external_id,
        )
    ).scalars().first()
    created = False
    if customer is None:
        customer = Customer(
            workspace_id=ctx.actor.workspace_id,
            channel=channel,
            external_id=external_id,
            name=name,
            profile=profile_in,
            stage=stage if stage in CUSTOMER_STAGES else "new",
        )
        ctx.db.add(customer)
        ctx.db.flush()
        created = True
    else:
        if name:
            customer.name = name
        customer.profile = {**(customer.profile or {}), **profile_in}
        if stage in CUSTOMER_STAGES:
            customer.stage = stage
    return {
        "customer_id": customer.id,
        "channel": channel,
        "external_id": external_id,
        "name": customer.name,
        "stage": customer.stage,
        "profile": customer.profile,
        "created": created,
    }


async def _list_customers(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Customer).where(Customer.workspace_id == ctx.actor.workspace_id).order_by(Customer.updated_at.desc())
    ).scalars().all()
    return {
        "customers": [
            {"id": c.id, "channel": c.channel, "external_id": c.external_id, "name": c.name, "profile": c.profile}
            for c in rows
        ]
    }


async def _get_customer(ctx: ToolContext) -> dict:
    customer = _find_customer(ctx, ctx.params)
    if customer is None:
        return {"error": "customer not found in workspace"}
    conversations = ctx.db.execute(
        select(Conversation).where(Conversation.customer_id == customer.id).order_by(Conversation.updated_at.desc()).limit(5)
    ).scalars().all()
    memory: list[dict] = []
    for conv in conversations:
        messages = ctx.db.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at.desc()).limit(10)
        ).scalars().all()
        memory.append(
            {
                "conversation_id": conv.id,
                "state": conv.state,
                "summary": conv.summary,
                "messages": [{"direction": m.direction, "content": m.content} for m in reversed(messages)],
            }
        )
    return {
        "customer_id": customer.id,
        "name": customer.name,
        "channel": customer.channel,
        "external_id": customer.external_id,
        "profile": customer.profile,
        "conversations": memory,
    }


async def _update_customer(ctx: ToolContext) -> dict:
    customer = _find_customer(ctx, ctx.params)
    if customer is None:
        return {"error": "customer not found in workspace"}
    if "stage" in ctx.params:
        stage = ctx.params["stage"]
        if stage not in CUSTOMER_STAGES:
            return {"error": f"stage must be one of {sorted(CUSTOMER_STAGES)}, got {stage!r}"}
        customer.stage = stage
    if ctx.params.get("name"):
        customer.name = _clean_text(ctx.params["name"])
    profile_in = ctx.params.get("profile")
    if isinstance(profile_in, dict):
        customer.profile = {**(customer.profile or {}), **profile_in}
    ctx.db.flush()
    return {"customer_id": customer.id, "name": customer.name, "stage": customer.stage, "profile": customer.profile}


def _name_similarity(left: str, right: str) -> float:
    """Bigram overlap for tolerant Chinese area-name matching."""
    def bigrams(text: str) -> set[str]:
        compact = "".join(str(text).split())
        return {compact[i : i + 2] for i in range(len(compact) - 1)}

    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def _promote_area(ctx: ToolContext) -> dict:
    area = ctx.db.get(LayoutArea, ctx.params.get("area_id")) if ctx.params.get("area_id") else None
    if area is None:
        name = ctx.params.get("area_name") or ctx.params.get("name")
        if name:
            area = ctx.db.execute(
                select(LayoutArea).where(
                    LayoutArea.workspace_id == ctx.actor.workspace_id,
                    LayoutArea.name == name,
                )
            ).scalars().first()
        if area is None and name:
            candidates = ctx.db.execute(
                select(LayoutArea).where(
                    LayoutArea.workspace_id == ctx.actor.workspace_id,
                    LayoutArea.stage == "test",
                )
            ).scalars().all()
            matches = [a for a in candidates if name in a.name or a.name in name]
            if not matches and candidates:
                scored = sorted(candidates, key=lambda a: _name_similarity(name, a.name), reverse=True)
                if _name_similarity(name, scored[0].name) >= 0.4:
                    area = scored[0]
            elif len(matches) == 1:
                area = matches[0]
            elif len(matches) > 1:
                area = min(matches, key=lambda a: abs(len(a.name) - len(name)))
    if area is None or area.workspace_id != ctx.actor.workspace_id:
        return {"error": "area not found in workspace"}
    area.stage = "live"
    if ctx.params.get("parent_id"):
        area.parent_id = ctx.params["parent_id"]
    if area.target_type == "function" and area.target_id:
        from .models import WorkspaceFunction

        fn = ctx.db.get(WorkspaceFunction, area.target_id)
        if fn is not None and fn.workspace_id == ctx.actor.workspace_id:
            fn.stage = "live"
    ctx.db.flush()
    return {"area_id": area.id, "name": area.name, "stage": area.stage, "parent_id": area.parent_id}


async def _create_function(ctx: ToolContext) -> dict:
    """Create a real, executable workspace function plus its functional area.

    Agent-kind functions carry goal/instructions/acceptance in ``config`` so
    they can be tested and later run automatically once promoted to live.
    """
    import hashlib

    from .functions import validate_name
    from .models import WorkspaceFunction

    display_name = (ctx.params.get("display_name") or ctx.params.get("name") or "").strip()
    name = ctx.params.get("name") or ""
    if not validate_name(name):
        name = "fn_" + hashlib.md5(display_name.encode("utf-8")).hexdigest()[:12]
    from .tools import registry as _registry

    if _registry.get(name) is not None:
        return {"error": f"function name conflicts with built-in tool: {name}"}
    exists = ctx.db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == ctx.actor.workspace_id,
            WorkspaceFunction.name == name,
        )
    ).scalars().first()
    if exists is not None:
        return {"error": f"function already exists: {name}"}

    kind = ctx.params.get("kind") or "agent"
    config = dict(ctx.params.get("config") or {})
    config.setdefault("display_name", display_name or name)
    category = ctx.params.get("category") or "general"
    parent_area_id = ctx.params.get("parent_area_id") or None

    fn = WorkspaceFunction(
        workspace_id=ctx.actor.workspace_id,
        name=name,
        description=ctx.params.get("description") or display_name,
        kind=kind,
        category=category,
        input_schema=ctx.params.get("input_schema") or {},
        risk_level=ctx.params.get("risk_level") or "R0",
        side_effect=bool(ctx.params.get("side_effect")),
        idempotent=bool(ctx.params.get("idempotent", True)),
        enabled=True,
        stage="test",
        parent_area_id=parent_area_id,
        config=config,
    )
    ctx.db.add(fn)
    ctx.db.flush()

    area = LayoutArea(
        workspace_id=ctx.actor.workspace_id,
        name=display_name or name,
        kind="function",
        target_type="function",
        target_id=fn.id,
        parent_id=parent_area_id,
        stage="test",
        config={"function_id": fn.id, "kind": kind, "category": category},
    )
    ctx.db.add(area)
    ctx.db.flush()
    return {
        "function_id": fn.id,
        "name": fn.name,
        "display_name": display_name,
        "area_id": area.id,
        "stage": "test",
        "kind": fn.kind,
        "category": fn.category,
        "description": fn.description,
        "acceptance": config.get("acceptance", ""),
    }


async def _run_function(ctx: ToolContext) -> dict:
    from .functions import get_workspace_function
    from .gateway import execute as gateway_execute
    from .models import WorkspaceFunction

    fn = None
    if ctx.params.get("function_id"):
        candidate = ctx.db.get(WorkspaceFunction, ctx.params["function_id"])
        if candidate is not None and candidate.workspace_id == ctx.actor.workspace_id:
            fn = candidate
    if fn is None and ctx.params.get("name"):
        fn = get_workspace_function(ctx.db, ctx.actor.workspace_id, ctx.params["name"])
    if fn is None:
        return {"error": "function not found in workspace"}

    params = ctx.params.get("params")
    if not isinstance(params, dict):
        params = {}
    res = await gateway_execute(ctx.db, ctx.actor, fn.name, params, idempotency_key=ctx.idempotency_key)
    out = {"function_id": fn.id, "name": fn.name, "stage": fn.stage, "decision": res.decision.value}
    if res.approval_id:
        out["approval_id"] = res.approval_id
    if res.result is not None:
        out["result"] = res.result
    return out


async def _promote_function(ctx: ToolContext) -> dict:
    from .models import WorkspaceFunction

    fn = None
    if ctx.params.get("function_id"):
        candidate = ctx.db.get(WorkspaceFunction, ctx.params["function_id"])
        if candidate is not None and candidate.workspace_id == ctx.actor.workspace_id:
            fn = candidate
    if fn is None and (ctx.params.get("name") or ctx.params.get("function_name")):
        name = ctx.params.get("name") or ctx.params.get("function_name")
        fn = ctx.db.execute(
            select(WorkspaceFunction).where(
                WorkspaceFunction.workspace_id == ctx.actor.workspace_id,
                WorkspaceFunction.name == name,
            )
        ).scalars().first()
    if fn is None:
        return {"error": "function not found in workspace"}
    fn.stage = "live"
    areas = ctx.db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == ctx.actor.workspace_id,
            LayoutArea.target_type == "function",
            LayoutArea.target_id == fn.id,
        )
    ).scalars().all()
    for area in areas:
        area.stage = "live"
    ctx.db.flush()
    return {"function_id": fn.id, "name": fn.name, "stage": "live", "areas_promoted": len(areas)}


async def _run_code(ctx: ToolContext) -> dict:
    from .general import run_code

    return run_code(ctx.params.get("code", ""), ctx.params.get("inputs") or {}, float(ctx.params.get("timeout") or 5))


async def _read_data(ctx: ToolContext) -> dict:
    from .general import read_data

    return read_data(ctx.actor.workspace_id, ctx.params.get("file_name", ""))


async def _write_report(ctx: ToolContext) -> dict:
    from .general import write_report

    return write_report(ctx.actor.workspace_id, ctx.params.get("file_name", "report.json"), ctx.params.get("content", ""))


async def _summarize_data(ctx: ToolContext) -> dict:
    from .general import summarize_data

    return summarize_data(
        ctx.actor.workspace_id,
        ctx.params.get("file_name", ""),
        ctx.params.get("group_by", ""),
        ctx.params.get("value_col", ""),
        ctx.params.get("agg", "sum"),
        ctx.params.get("output_file") or None,
    )


async def _create_todo(ctx: ToolContext) -> dict:
    """Triage an agent-observed event into an owner-facing action item."""
    kind = ctx.params.get("kind") or "info"
    if kind not in ("must_confirm", "needs_reply", "info"):
        kind = "info"
    title = str(ctx.params.get("title") or "").strip() or "待办事项"
    detail = str(ctx.params.get("detail") or "")
    confirm_code = ""
    if kind == "must_confirm":
        from .notify import generate_code, notify_owner_for_workspace

        confirm_code = str(ctx.params.get("confirm_code") or "").strip() or generate_code()
        notify_owner_for_workspace(
            ctx.db,
            ctx.actor.workspace_id,
            f"[Krama] 待确认：{title}",
            f"{detail}\n确认码：{confirm_code}",
        )
    todo = Todo(
        workspace_id=ctx.actor.workspace_id,
        kind=kind,
        title=title,
        detail=detail,
        status="pending",
        confirm_code=confirm_code,
        ref_type=str(ctx.params.get("ref_type") or ""),
        ref_id=str(ctx.params.get("ref_id") or ""),
    )
    ctx.db.add(todo)
    ctx.db.flush()
    return {"todo_id": todo.id, "kind": todo.kind, "title": todo.title, "confirm_code": confirm_code}


async def _list_todos(ctx: ToolContext) -> dict:
    kind = ctx.params.get("kind") or None
    status = ctx.params.get("status") or "pending"
    query = select(Todo).where(Todo.workspace_id == ctx.actor.workspace_id)
    if kind:
        query = query.where(Todo.kind == kind)
    if status:
        query = query.where(Todo.status == status)
    rows = ctx.db.execute(query.order_by(Todo.created_at.desc())).scalars().all()
    return {
        "todos": [
            {
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
            for t in rows
        ]
    }


async def _business_overview(ctx: ToolContext) -> dict:
    products = ctx.db.execute(
        select(Product).where(Product.workspace_id == ctx.actor.workspace_id)
    ).scalars().all()
    customers = ctx.db.execute(
        select(Customer).where(Customer.workspace_id == ctx.actor.workspace_id)
    ).scalars().all()
    scripts = ctx.db.execute(
        select(Script).where(Script.workspace_id == ctx.actor.workspace_id).order_by(Script.wins.desc())
    ).scalars().all()
    live_areas = ctx.db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == ctx.actor.workspace_id,
            LayoutArea.stage == "live",
        )
    ).scalars().all()
    pipeline = {"new": 0, "lead": 0, "won": 0, "lost": 0}
    for customer in customers:
        key = customer.stage if customer.stage in pipeline else "new"
        pipeline[key] += 1
    low_stock = [p for p in products if (p.stock or 0) <= 5]
    pending_ship = ctx.db.execute(
        select(Order).where(
            Order.workspace_id == ctx.actor.workspace_id,
            Order.status == OrderStatus.PAID.value,
        )
    ).scalars().all()
    from .models import ApprovalRequest, ApprovalStatus

    pending_approvals = ctx.db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == ctx.actor.workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    ).scalars().all()
    return {
        "products": len(products),
        "published": len([p for p in products if p.status == "published"]),
        "total_stock": sum(p.stock or 0 for p in products),
        "customers": len(customers),
        "pipeline": pipeline,
        "live_areas": [a.name for a in live_areas],
        "top_scripts": [{"title": s.title, "wins": s.wins, "sends": s.sends} for s in scripts[:5]],
        "low_stock": len(low_stock),
        "pending_ship": len(pending_ship),
        "pending_approvals": len(pending_approvals),
    }


async def _record_win(ctx: ToolContext) -> dict:
    customer_id = ctx.params.get("customer_id")
    customer_stage = None
    if customer_id:
        customer = ctx.db.get(Customer, customer_id)
        if customer is not None and customer.workspace_id == ctx.actor.workspace_id:
            customer.stage = "won"
            customer_stage = customer.stage

    script_id = ctx.params.get("script_id")
    script = None
    if script_id:
        candidate = ctx.db.get(Script, script_id)
        if candidate is not None and candidate.workspace_id == ctx.actor.workspace_id:
            script = candidate
    else:
        script = ctx.db.execute(
            select(Script).where(Script.workspace_id == ctx.actor.workspace_id)
            .order_by(Script.updated_at.desc())
        ).scalars().first()

    if script is not None:
        script.wins = int(script.wins or 0) + 1
    else:
        content = (ctx.params.get("content") or "").strip()
        if content:
            script = Script(
                workspace_id=ctx.actor.workspace_id,
                title=(ctx.params.get("title") or content[:40]),
                content=content,
                tags=[],
                wins=1,
                sends=0,
            )
            ctx.db.add(script)
            ctx.db.flush()
    ctx.db.flush()
    return {
        "customer_id": customer_id,
        "customer_stage": customer_stage,
        "script_id": script.id if script is not None else None,
        "script_wins": script.wins if script is not None else 0,
    }


async def _record_script(ctx: ToolContext) -> dict:
    content = (ctx.params.get("content") or "").strip()
    if not content:
        return {"error": "content is required"}
    script = Script(
        workspace_id=ctx.actor.workspace_id,
        title=(ctx.params.get("title") or content[:40]),
        content=content,
        tags=ctx.params.get("tags") or [],
        wins=int(ctx.params.get("wins") or 0),
        sends=int(ctx.params.get("sends") or 1),
    )
    ctx.db.add(script)
    ctx.db.flush()
    return {"script_id": script.id, "title": script.title, "wins": script.wins, "sends": script.sends}


async def _optimize_scripts(ctx: ToolContext) -> dict:
    scripts = ctx.db.execute(
        select(Script).where(Script.workspace_id == ctx.actor.workspace_id).order_by(Script.wins.desc()).limit(10)
    ).scalars().all()
    if not scripts:
        return {"reply": "还没有话术样本。请先记录成交话术，我会持续学习最优话术。"}
    catalog = "\n".join(
        f"{index}. [{s.wins}胜/{s.sends}发] {s.title}：{s.content[:200]}"
        for index, s in enumerate(scripts, 1)
    )
    prompt = (
        "你是销售话术优化师。以下话术按成交次数排序：\n" + catalog +
        "\n请综合最高成交的话术，输出一句更可能促成下单的话术。只输出话术本身，不要解释。"
    )
    best = _model_text(ctx, prompt, scripts[0].content)
    return {"reply": best, "samples": len(scripts), "top": [{"title": s.title, "wins": s.wins} for s in scripts[:3]]}


async def _quote_product(ctx: ToolContext) -> dict:
    product = _find_product(ctx, ctx.params)
    if product is None:
        return {"error": "product not found in workspace"}
    quantity = max(1, int(ctx.params.get("quantity") or 1))
    customer = _find_customer(ctx, ctx.params)
    buyer = customer.name if customer else "客户"
    total = round(product.price * quantity, 2)
    prompt = (
        f"你是商家客服。请用一句自然中文向{buyer}报价：商品「{product.name}」、单价 ¥{product.price:g}、"
        f"数量 {quantity}、合计 ¥{total:g}、库存 {product.stock}。只输出报价这一句。"
    )
    fallback = (
        f"{buyer}您好，「{product.name}」单价 ¥{product.price:g}，数量 {quantity} 件，合计 ¥{total:g}；"
        f"当前库存 {product.stock} 件，需要下单请回复。"
    )
    reply = _model_text(ctx, prompt, fallback)
    return {
        "reply": reply,
        "quote": {
            "product_id": product.id,
            "product_name": product.name,
            "unit_price": product.price,
            "quantity": quantity,
            "total": total,
            "currency": "CNY",
            "stock": product.stock,
        },
    }


async def _introduce_product(ctx: ToolContext) -> dict:
    product = _find_product(ctx, ctx.params)
    if product is None:
        return {"error": "product not found in workspace"}
    prompt = (
        f"你是商家客服。请用两三句中文向客户介绍商品「{product.name}」，"
        f"包含价格 ¥{product.price:g} 和库存 {product.stock}，语气自然。只输出介绍文案。"
    )
    fallback = f"向您推荐「{product.name}」：单价 ¥{product.price:g}，当前库存 {product.stock} 件，欢迎选购。"
    return {"reply": _model_text(ctx, prompt, fallback), "product_id": product.id, "product_name": product.name}


async def _reply_customer(ctx: ToolContext) -> dict:
    customer = _find_customer(ctx, ctx.params)
    if customer is None:
        return {"error": "customer not found in workspace"}
    text = ctx.params.get("text") or ""
    if not text:
        return {"error": "text is required"}
    conversation = ctx.db.execute(
        select(Conversation).where(Conversation.customer_id == customer.id).order_by(Conversation.updated_at.desc())
    ).scalars().first()
    conversation_id = conversation.id if conversation is not None else ""
    from .messaging import send_outbound

    result = send_outbound(ctx.db, ctx.actor.workspace_id, customer.channel, customer.external_id, text, conversation_id)
    return {"customer_id": customer.id, "reply": text, **result}


async def _send_chat_message(ctx: ToolContext) -> dict:
    channel = ctx.params.get("channel") or "chat"
    external_id = str(ctx.params.get("external_id") or "")
    text = ctx.params.get("text") or ""
    if not external_id or not text:
        return {"error": "external_id and text are required"}
    from .messaging import send_outbound

    return send_outbound(
        ctx.db,
        ctx.actor.workspace_id,
        channel,
        external_id,
        text,
        ctx.params.get("conversation_id") or "",
    )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _list_goals(ctx: ToolContext) -> dict:
    rows = ctx.db.execute(
        select(Goal).where(Goal.workspace_id == ctx.actor.workspace_id).order_by(Goal.created_at.desc())
    ).scalars().all()
    return {
        "goals": [
            {
                "id": g.id,
                "title": g.title,
                "detail": g.detail,
                "success_criteria": g.success_criteria,
                "progress_note": g.progress_note,
                "status": g.status,
                "due_at": g.due_at.isoformat() if g.due_at else None,
            }
            for g in rows
        ]
    }


async def _create_goal(ctx: ToolContext) -> dict:
    title = str(ctx.params.get("title") or ctx.params.get("goal") or "").strip()
    if not title:
        return {"error": "goal title is required"}
    goal = Goal(
        workspace_id=ctx.actor.workspace_id,
        title=title,
        detail=str(ctx.params.get("detail") or ""),
        success_criteria=str(ctx.params.get("success_criteria") or ""),
        progress_note=str(ctx.params.get("progress_note") or ""),
        status="active",
    )
    if ctx.params.get("due_at"):
        from .models import utcnow

        try:
            goal.due_at = utcnow()
        except Exception:  # noqa: BLE001 - due date is best-effort metadata
            pass
    ctx.db.add(goal)
    ctx.db.flush()
    return {"goal_id": goal.id, "title": goal.title, "status": goal.status}


async def _update_goal(ctx: ToolContext) -> dict:
    goal_id = ctx.params.get("goal_id") or ctx.params.get("id")
    row = ctx.db.execute(
        select(Goal).where(Goal.workspace_id == ctx.actor.workspace_id, Goal.id == goal_id)
    ).scalars().first()
    if row is None:
        return {"error": "goal not found"}
    for field in ("status", "detail", "success_criteria", "progress_note"):
        if ctx.params.get(field) is not None:
            setattr(row, field, str(ctx.params[field]))
    ctx.db.flush()
    return {"goal_id": row.id, "title": row.title, "status": row.status, "progress_note": row.progress_note}


async def _get_memory(ctx: ToolContext) -> dict:
    key = ctx.params.get("key")
    query = select(AgentMemory).where(AgentMemory.workspace_id == ctx.actor.workspace_id)
    if key:
        query = query.where(AgentMemory.key == key)
    rows = ctx.db.execute(query.order_by(AgentMemory.updated_at.desc())).scalars().all()
    return {"memories": [{"key": m.key, "value": m.value, "kind": m.kind} for m in rows]}


async def _set_memory(ctx: ToolContext) -> dict:
    key = str(ctx.params.get("key") or "").strip()
    value = str(ctx.params.get("value") or "")
    if not key:
        return {"error": "memory key is required"}
    kind = str(ctx.params.get("kind") or "preference")
    if kind not in ("preference", "fact", "note"):
        kind = "preference"
    row = ctx.db.execute(
        select(AgentMemory).where(AgentMemory.workspace_id == ctx.actor.workspace_id, AgentMemory.key == key)
    ).scalars().first()
    if row is None:
        row = AgentMemory(workspace_id=ctx.actor.workspace_id, key=key, value=value, kind=kind)
        ctx.db.add(row)
    else:
        row.value = value
        row.kind = kind
    ctx.db.flush()
    from .memory_gov import enforce_capacity

    enforce_capacity(ctx.db, ctx.actor.workspace_id)
    return {"key": row.key, "value": row.value, "kind": row.kind}


async def _list_team_roles(ctx: ToolContext) -> dict:
    """Return the Polsia-style AI team this agent can self-organize into."""
    from .roles import catalog

    return {"roles": catalog()}


async def _list_skills(ctx: ToolContext) -> dict:
    from .skills import catalog, installed_skill_ids

    installed = installed_skill_ids(ctx.db, ctx.actor.workspace_id)
    query = str(ctx.params.get("query") or "").strip()
    items = []
    for skill in catalog():
        if query and query.lower() not in skill["id"].lower() and query not in skill["title"] and query not in skill["category"]:
            continue
        item = {"id": skill["id"], "title": skill["title"]}
        item["installed"] = skill["id"] in installed
        items.append(item)
    return {"skills": items, "hint": "用 install_skill(skill_id) 安装；查指定能力用 list_skills(query='关键词')"}


async def _install_skill(ctx: ToolContext) -> dict:
    from .skills import install_skill

    skill_id = str(ctx.params.get("skill_id") or ctx.params.get("name") or "").strip()
    if not skill_id:
        return {"error": "skill_id is required"}
    try:
        return install_skill(ctx.db, ctx.actor.workspace_id, skill_id)
    except ValueError as exc:
        return {"error": str(exc)}


async def _get_channel_receipt(ctx: ToolContext) -> dict:
    """Read a configured channel connector's real receipt data over SSRF-guarded HTTP."""
    from .connectors import _headers_from_config
    from .functions import _interpolate_url, recheck_url, request_http, validate_url
    from .models import ConnectorInstance

    channel = str(ctx.params.get("channel") or ctx.params.get("connector_type") or "").strip()
    connector_id = ctx.params.get("connector_id")
    query = ctx.params.get("params") or {}
    if not isinstance(query, dict):
        query = {}

    instance = None
    if connector_id:
        candidate = ctx.db.get(ConnectorInstance, connector_id)
        if candidate is not None and candidate.workspace_id == ctx.actor.workspace_id:
            instance = candidate
    if instance is None and channel:
        instance = ctx.db.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.workspace_id == ctx.actor.workspace_id,
                ConnectorInstance.connector_type == channel,
            )
        ).scalars().first()
    if instance is None:
        return {"error": "connector not found; use channel=connector_type (e.g. ecommerce_independent)"}

    config = instance.config or {}
    signer = config.get("signer")
    if signer:
        from .connectors import SignedApiConnector, decrypt_config_secrets

        connector = SignedApiConnector(instance.connector_type, signer, config=decrypt_config_secrets(config))
        return await connector.read(query or {})
    endpoint = str(config.get("endpoint_url") or "").strip()
    if not endpoint:
        return {
            "connector_type": instance.connector_type,
            "connected": False,
            "status": "stub",
            "note": "未配置回执网址，当前为预留桩；配置回执网址与 Key 后即可读取真实回执。",
        }
    method = str(config.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        method = "GET"
    allow_private = bool(config.get("allow_private"))
    try:
        validate_url(endpoint, allow_private=allow_private)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    url, remaining = _interpolate_url(endpoint, query)
    headers = _headers_from_config(config)
    try:
        if method == "POST":
            resp = await request_http(
                "POST", recheck_url(url, allow_private), headers=headers, json_body=remaining
            )
        else:
            resp = await request_http(
                "GET", recheck_url(url, allow_private), headers=headers, params=remaining
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"渠道请求失败: {exc}"}
    body = resp.get("json")
    if not isinstance(body, (dict, list)):
        body = resp.get("text") or ""
    if isinstance(body, str) and len(body) > 4000:
        body = body[:4000]
    return {
        "connector_type": instance.connector_type,
        "connected": True,
        "http_status": resp.get("status"),
        "body": body,
    }


async def _list_channel_connectors(ctx: ToolContext) -> dict:
    from .models import ConnectorInstance

    rows = ctx.db.execute(
        select(ConnectorInstance).where(ConnectorInstance.workspace_id == ctx.actor.workspace_id)
    ).scalars().all()
    return {
        "connectors": [
            {
                "id": c.id,
                "connector_type": c.connector_type,
                "name": c.name,
                "configured": bool((c.config or {}).get("endpoint_url") or (c.config or {}).get("signer")),
                "endpoint_url": (c.config or {}).get("endpoint_url") or "",
                "allow_private": bool((c.config or {}).get("allow_private")),
                "signer": (c.config or {}).get("signer") or "",
                "has_secret": bool((c.config or {}).get("app_secret_enc") or (c.config or {}).get("checkword_enc") or (c.config or {}).get("access_token_enc")),
            }
            for c in rows
        ]
    }


def _coerce_product_items(payload) -> list[dict]:
    """Normalise user-supplied product data into the internal item shape."""
    def num(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def text(value):
        return str(value or "").strip()

    items: list[dict] = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        name = text(row.get("name") or row.get("产品") or row.get("商品"))
        if not name:
            continue
        specs = row.get("specs") or row.get("规格") or {}
        qualifications = row.get("qualifications") or row.get("资质") or []
        if isinstance(qualifications, str):
            qualifications = [part.strip() for part in qualifications.replace("，", ",").split(",") if part.strip()]
        items.append(
            {
                "name": name,
                "price": num(row.get("price") or row.get("价格") or row.get("单价"), 0.0),
                "category": text(row.get("category") or row.get("类目") or "general"),
                "photo_url": text(row.get("photo_url") or row.get("照片") or row.get("图片")),
                "specs": specs if isinstance(specs, dict) else {},
                "detail": text(row.get("detail") or row.get("详情") or row.get("描述")),
                "qualifications": qualifications if isinstance(qualifications, list) else [],
                "stock": int(num(row.get("stock") or row.get("库存"), 0)),
            }
        )
    return items


async def _import_products(ctx: ToolContext) -> dict:
    """Bulk-create products from inline items, pasted text or an uploaded file."""
    import csv
    import io
    import json as json_mod
    import re

    workspace_id = ctx.actor.workspace_id
    area_name = str(ctx.params.get("area_name") or "产品展示区")
    area_id = ctx.params.get("layout_area_id") or ""
    items = _coerce_product_items(ctx.params.get("items"))

    file_name = str(ctx.params.get("file_name") or "")
    if file_name and not items:
        from .general import read_data

        data = read_data(workspace_id, file_name)
        if data.get("kind") == "csv":
            items = _coerce_product_items(data.get("rows") or [])
        elif data.get("kind") == "json":
            payload = data.get("data")
            if isinstance(payload, dict):
                payload = payload.get("products") or payload.get("items") or []
            items = _coerce_product_items(payload)
        elif data.get("kind") == "text":
            for line in (data.get("content") or "").splitlines():
                match = re.match(r"(.+?)[\s,，]+(\d+(?:\.\d{1,2})?)\s*(?:元|块|¥|￥)?$", line.strip())
                if match:
                    items.append({"name": match.group(1).strip(), "price": float(match.group(2))})

    text = str(ctx.params.get("text") or "")
    if text and not items:
        for line in text.splitlines():
            match = re.match(r"(.+?)[\s,，]+(\d+(?:\.\d{1,2})?)\s*(?:元|块|¥|￥)?$", line.strip())
            if match:
                items.append({"name": match.group(1).strip(), "price": float(match.group(2))})

    if not items:
        return {"error": "no parseable products found"}

    if not area_id:
        area = ctx.db.execute(
            select(LayoutArea).where(
                LayoutArea.workspace_id == workspace_id,
                LayoutArea.name == area_name,
                LayoutArea.parent_id.is_(None),
            )
        ).scalars().first()
        if area is not None:
            area_id = area.id
        else:
            area = LayoutArea(
                workspace_id=workspace_id,
                name=area_name,
                kind="module",
                target_type="product_manage",
                stage="test",
                sort_order=0,
                enabled=True,
            )
            ctx.db.add(area)
            ctx.db.flush()
            area_id = area.id

    created: list[dict] = []
    for item in items:
        product = Product(
            workspace_id=workspace_id,
            name=item.get("name") or "未命名",
            category=item.get("category") or area_name,
            price=float(item.get("price") or 0.0),
            status="draft",
            stock=int(item.get("stock") or 0),
            photo_url=item.get("photo_url") or "",
            specs=item.get("specs") or {},
            detail=item.get("detail") or "",
            qualifications=item.get("qualifications") or [],
            layout_area_id=area_id,
        )
        ctx.db.add(product)
        ctx.db.flush()
        created.append({"product_id": product.id, "name": product.name, "price": product.price})
    return {"area_id": area_id, "area_name": area_name, "created": created, "count": len(created)}


async def _get_commerce_policy(ctx: ToolContext) -> dict:
    policy = ctx.db.execute(
        select(CommercePolicy).where(CommercePolicy.workspace_id == ctx.actor.workspace_id)
    ).scalars().first()
    if policy is None:
        policy = CommercePolicy(workspace_id=ctx.actor.workspace_id)
        ctx.db.add(policy)
        ctx.db.flush()
    return {
        "auto_accept_orders": policy.auto_accept_orders,
        "max_discount_percent": policy.max_discount_percent,
        "approval_threshold_amount": policy.approval_threshold_amount,
        "currency": policy.currency,
        "shipping_fee": policy.shipping_fee,
        "payment_method": policy.payment_method,
        "terms": policy.terms,
    }


async def _set_commerce_policy(ctx: ToolContext) -> dict:
    policy = ctx.db.execute(
        select(CommercePolicy).where(CommercePolicy.workspace_id == ctx.actor.workspace_id)
    ).scalars().first()
    if policy is None:
        policy = CommercePolicy(workspace_id=ctx.actor.workspace_id)
        ctx.db.add(policy)
    updatable = (
        "auto_accept_orders",
        "max_discount_percent",
        "approval_threshold_amount",
        "currency",
        "shipping_fee",
        "payment_method",
        "terms",
    )
    for field in updatable:
        if ctx.params.get(field) is not None:
            value = ctx.params[field]
            if field in ("max_discount_percent", "approval_threshold_amount", "shipping_fee"):
                value = float(value)
            elif field == "auto_accept_orders":
                value = bool(value)
            else:
                value = str(value)
            setattr(policy, field, value)
    ctx.db.flush()
    return await _get_commerce_policy(ctx)


registry = ToolRegistry()
for _spec in (
    ToolSpec("list_products", "List workspace products (read-only)", RiskLevel.R0, False, True, _list_products),
    ToolSpec("workspace_summary", "Summarize workspace (read-only)", RiskLevel.R0, False, True, _workspace_summary),
    ToolSpec("list_orders", "List workspace orders (read-only)", RiskLevel.R0, False, True, _list_orders),
    ToolSpec("list_inventory", "List product stock levels (read-only)", RiskLevel.R0, False, True, _list_inventory),
    ToolSpec("list_shipments", "List shipments (read-only)", RiskLevel.R0, False, True, _list_shipments),
    ToolSpec("list_customers", "List workspace customers (read-only)", RiskLevel.R0, False, True, _list_customers),
    ToolSpec("get_customer", "Read one customer profile and conversation memory (read-only)", RiskLevel.R0, False, True, _get_customer),
    ToolSpec("business_overview", "Summarize products, inventory, customer pipeline and top scripts (read-only)", RiskLevel.R0, False, True, _business_overview),
    ToolSpec("list_team_roles", "List the agent's business roles (店长/上架员/客服/运营增长/履约员/财务) it self-organizes into (read-only)", RiskLevel.R0, False, True, _list_team_roles),
    ToolSpec("list_skills", "List installable market-validated business skills and whether each is installed (read-only)", RiskLevel.R0, False, True, _list_skills),
    ToolSpec("install_skill", "Install a market-validated business skill by skill_id (creates live executable functions)", RiskLevel.R1, True, True, _install_skill),
    ToolSpec("get_channel_receipt", "Read a configured channel connector's real receipt data (orders/shipments/payment) via its SSRF-guarded endpoint URL; returns a stub note when not configured (read-only)", RiskLevel.R0, False, True, _get_channel_receipt),
    ToolSpec("list_channel_connectors", "List workspace channel connectors and whether each has a configured receipt URL (read-only)", RiskLevel.R0, False, True, _list_channel_connectors),
    ToolSpec("create_product_draft", "Create a product draft", RiskLevel.R1, True, True, _create_product_draft),
    ToolSpec("update_product", "Update a product's name, category, price, photo, specs, detail, qualifications", RiskLevel.R1, True, True, _update_product),
    ToolSpec("update_customer", "Update a customer's stage (new/lead/won/lost), name or profile", RiskLevel.R1, True, True, _update_customer),
    ToolSpec("promote_area", "Promote a tested functional area from test to live", RiskLevel.R1, True, True, _promote_area),
    ToolSpec("record_script", "Record a sales script with conversion metrics", RiskLevel.R1, True, True, _record_script),
    ToolSpec("record_win", "Mark a customer won and increment a sales script's win count", RiskLevel.R1, True, False, _record_win),
    ToolSpec("generate_marketing_draft", "Generate marketing copy draft via model", RiskLevel.R1, False, True, _generate_marketing_draft),
    ToolSpec("optimize_scripts", "Learn and propose the highest-converting sales script", RiskLevel.R1, False, True, _optimize_scripts),
    ToolSpec("quote_product", "Draft a price quote for a customer against live stock", RiskLevel.R1, False, True, _quote_product),
    ToolSpec("introduce_product", "Draft a product introduction reply for a customer", RiskLevel.R1, False, True, _introduce_product),
    ToolSpec("upsert_customer", "Create or update a customer identity and profile", RiskLevel.R1, True, True, _upsert_customer),
    ToolSpec("publish_product", "Publish a product", RiskLevel.R2, True, True, _publish_product),
    ToolSpec("create_order", "Create an order via an e-commerce channel", RiskLevel.R2, True, True, _create_order),
    ToolSpec("reply_customer", "Send a reply to a known customer via their channel", RiskLevel.R2, True, True, _reply_customer),
    ToolSpec("send_chat_message", "Send a raw outbound message through a chat channel", RiskLevel.R2, True, True, _send_chat_message),
    ToolSpec("add_stock", "Receive inventory for a product", RiskLevel.R2, True, False, _add_stock),
    ToolSpec("fulfill_order", "Ship a paid order and decrement stock", RiskLevel.R2, True, True, _fulfill_order),
    ToolSpec("mark_delivered", "Mark a shipment as delivered", RiskLevel.R2, True, True, _mark_delivered),
    ToolSpec("bulk_update_prices", "Bulk update product prices", RiskLevel.R3, True, True, _bulk_update_prices),
    ToolSpec("make_payment", "Execute a payment via Karma", RiskLevel.R3, True, True, _make_payment),
    ToolSpec("make_refund", "Refund a Karma payment", RiskLevel.R3, True, True, _make_refund),
    ToolSpec("reconcile_payment", "Reconcile a Karma payment against receipts and settlement", RiskLevel.R1, False, True, _reconcile_payment),
    ToolSpec("dispute_payment", "Open a dispute for a Karma payment", RiskLevel.R3, True, True, _dispute_payment),
    ToolSpec("cancel_order", "Cancel an e-commerce channel order", RiskLevel.R2, True, True, _cancel_order),
    ToolSpec("add_layout_area", "Add a functional area (module/widget)", RiskLevel.R1, True, True, _add_layout_area),
    ToolSpec("list_layout_areas", "List workspace functional areas (read-only)", RiskLevel.R0, False, True, _list_layout_areas),
    ToolSpec("link_products_to_area", "Categorize products into a functional area", RiskLevel.R1, True, True, _link_products_to_area),
    ToolSpec("create_function", "Create a real executable business function in the test area (agent/http/model/karma)", RiskLevel.R1, True, True, _create_function),
    ToolSpec("run_function", "Run a workspace function with given params through the gateway", RiskLevel.R1, True, True, _run_function),
    ToolSpec("promote_function", "Promote a tested workspace function from test to live", RiskLevel.R1, True, True, _promote_function),
    ToolSpec("run_code", "Run sandboxed pure-Python; code reads `data`/`inputs` dict and prints json.dumps(result); no filesystem/network, safe stdlib imports only", RiskLevel.R1, False, True, _run_code),
    ToolSpec("read_data", "Read an uploaded CSV/JSON/TXT file from the workspace data directory (read-only)", RiskLevel.R0, False, True, _read_data),
    ToolSpec("write_report", "Write a report file (JSON/CSV/Markdown/TXT) into the workspace data directory", RiskLevel.R1, True, False, _write_report),
    ToolSpec("summarize_data", "Group a CSV column by another column and aggregate (sum/avg/count/max/min), optionally writing the result to a file", RiskLevel.R1, True, True, _summarize_data),
    ToolSpec("create_todo", "Create an owner-facing action item (kind: must_confirm/needs_reply/info); must_confirm auto-generates an email confirmation code", RiskLevel.R1, True, True, _create_todo),
    ToolSpec("list_todos", "List owner-facing action items, optionally filtered by kind and status (read-only)", RiskLevel.R0, False, True, _list_todos),
    ToolSpec("list_goals", "List the workspace long-running goals the agent is tracking (read-only)", RiskLevel.R0, False, True, _list_goals),
    ToolSpec("create_goal", "Create a durable long-running goal the agent tracks across sessions until it is done", RiskLevel.R1, True, True, _create_goal),
    ToolSpec("update_goal", "Update a goal's status, progress_note, detail or success_criteria", RiskLevel.R1, True, True, _update_goal),
    ToolSpec("get_memory", "Read durable workspace agent memory (preferences/facts/notes); optional key filter (read-only)", RiskLevel.R0, False, True, _get_memory),
    ToolSpec("set_memory", "Write durable workspace agent memory so behavior stays consistent across conversations", RiskLevel.R1, True, True, _set_memory),
    ToolSpec("import_products", "Bulk-create products from inline items, pasted text or an uploaded CSV/JSON/TXT file into a product area", RiskLevel.R1, True, True, _import_products),
    ToolSpec("get_commerce_policy", "Read the owner-set trade guardrails (auto-accept, max discount, approval threshold, currency, shipping, terms) (read-only)", RiskLevel.R0, False, True, _get_commerce_policy),
    ToolSpec("set_commerce_policy", "Update the owner-set trade guardrails the agent must obey when quoting/negotiating/accepting orders", RiskLevel.R1, True, True, _set_commerce_policy),
):
    registry.register(_spec)
