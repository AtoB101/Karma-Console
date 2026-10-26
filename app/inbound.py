"""Shared inbound chat processing.

Extracted from the /chat/inbound endpoint so both the generic inbound API and
the platform-native webhook receivers (Telegram/WeChat/Discord) run the exact
same chain: customer lookup -> conversation/message record -> injection guard ->
agent decision -> outbound delivery.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Conversation, Customer, Membership, Message, Role, User, Workspace
from .security import Actor


async def process_inbound(
    db: Session,
    ws: Workspace,
    *,
    channel: str,
    external_id: str,
    message: str,
    customer_name: str = "",
) -> dict:
    from .chat import build_customer_memory, handle_message
    from .guard import detect_injection
    from .messaging import send_outbound

    owner = db.execute(
        select(Membership).where(Membership.workspace_id == ws.id, Membership.role == Role.OWNER.value)
    ).scalars().first()
    if owner is None:
        return {"workspace_id": ws.id, "reply": "", "intent": "error", "error": "no owner"}

    user = db.get(User, owner.user_id)
    actor = Actor(user=user, workspace_id=ws.id, role=Role.OWNER, source="customer")

    customer_id = ""
    conversation_id = ""
    memory = ""
    if external_id:
        customer = db.execute(
            select(Customer).where(
                Customer.workspace_id == ws.id,
                Customer.channel == channel,
                Customer.external_id == external_id,
            )
        ).scalars().first()
        if customer is None:
            customer = Customer(
                workspace_id=ws.id,
                channel=channel,
                external_id=external_id,
                name=customer_name,
            )
            db.add(customer)
            db.flush()
        elif customer_name:
            customer.name = customer_name
        customer_id = customer.id

        conversation = db.execute(
            select(Conversation)
            .where(Conversation.customer_id == customer.id)
            .order_by(Conversation.updated_at.desc())
        ).scalars().first()
        if conversation is None:
            conversation = Conversation(
                workspace_id=ws.id,
                customer_id=customer.id,
                channel=channel,
                external_id=external_id,
            )
            db.add(conversation)
            db.flush()
        conversation_id = conversation.id
        db.add(
            Message(
                workspace_id=ws.id,
                conversation_id=conversation_id,
                direction="in",
                content=message,
                meta={"channel": channel, "external_id": external_id},
            )
        )
        memory = build_customer_memory(db, ws.id, channel, external_id)

    injection = detect_injection(message)
    if injection:
        neutral = "抱歉，我无法执行该请求，请直接联系商家处理。"
        outbound = (
            send_outbound(db, ws.id, channel, external_id, neutral, conversation_id)
            if external_id
            else {"sent": False, "delivery": {"provider": "stub"}}
        )
        db.commit()
        return {
            "workspace_id": ws.id,
            "reply": neutral,
            "intent": "blocked",
            "customer_id": customer_id,
            "conversation_id": conversation_id,
            "outbound": outbound,
            "guard": injection,
        }

    result = await handle_message(db, actor, message, memory)
    outbound = {"sent": False, "delivery": {"provider": "stub"}}
    if external_id:
        outbound = send_outbound(db, ws.id, channel, external_id, result.get("reply", ""), conversation_id)
    db.commit()
    return {
        "workspace_id": ws.id,
        "reply": result.get("reply"),
        "intent": result.get("intent"),
        "customer_id": customer_id,
        "conversation_id": conversation_id,
        "outbound": outbound,
    }
