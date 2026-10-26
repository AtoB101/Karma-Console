"""AI 专属虚拟号映射（占位）。

本地只维护「对外虚拟号 ↔ E2EE 密文真实号 ↔ 客户」的映射与状态；真实
来电/短信转接由运营商隐私号服务完成（外部依赖，见
``docs/EXTERNAL_DEPENDENCIES.md``）。``real_encrypted`` 由客户端 E2EE 层
写入，服务端不解密，列表接口也不回传该密文。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Customer, VirtualNumber

class VirtualNumberError(Exception):
    """Raised on allocation/bind failures; mapped to HTTP 409 by routes."""


def _next_virtual_number(db: Session, workspace_id: str) -> str:
    rows = db.execute(
        select(VirtualNumber.virtual_number)
        .where(VirtualNumber.workspace_id == workspace_id)
    ).scalars().all()
    max_seq = 8000
    for v in rows:
        digits = v.lstrip("#")
        if digits.isdigit():
            max_seq = max(max_seq, int(digits))
    return f"#{max_seq + 1}"


def allocate(db: Session, actor, *, real_encrypted: str = "", customer_id: str = "") -> VirtualNumber:
    """Allocate the next available virtual number for the workspace."""
    number = _next_virtual_number(db, actor.workspace_id)
    status = "bound" if customer_id else "available"
    record = VirtualNumber(
        workspace_id=actor.workspace_id,
        virtual_number=number,
        real_encrypted=real_encrypted or "",
        bound_customer_id=customer_id or "",
        status=status,
    )
    db.add(record)
    db.flush()
    return record


def bind(db: Session, actor, *, virtual_number: str, customer_id: str, real_encrypted: str = "") -> VirtualNumber:
    record = db.execute(
        select(VirtualNumber).where(
            VirtualNumber.workspace_id == actor.workspace_id,
            VirtualNumber.virtual_number == virtual_number,
        )
    ).scalars().first()
    if record is None:
        raise VirtualNumberError("virtual number not found")
    customer = db.get(Customer, customer_id)
    if customer is None or customer.workspace_id != actor.workspace_id:
        raise VirtualNumberError("customer not found in this workspace")
    record.bound_customer_id = customer_id
    record.status = "bound"
    if real_encrypted:
        record.real_encrypted = real_encrypted
    db.flush()
    return record


def release(db: Session, actor, *, virtual_number: str) -> VirtualNumber:
    record = db.execute(
        select(VirtualNumber).where(
            VirtualNumber.workspace_id == actor.workspace_id,
            VirtualNumber.virtual_number == virtual_number,
        )
    ).scalars().first()
    if record is None:
        raise VirtualNumberError("virtual number not found")
    record.bound_customer_id = ""
    record.status = "available"
    db.flush()
    return record


def list_numbers(db: Session, actor) -> list[dict]:
    rows = db.execute(
        select(VirtualNumber)
        .where(VirtualNumber.workspace_id == actor.workspace_id)
        .order_by(VirtualNumber.created_at)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "virtual_number": r.virtual_number,
            "status": r.status,
            "bound_customer_id": r.bound_customer_id,
            "has_real": bool(r.real_encrypted),
        }
        for r in rows
    ]
