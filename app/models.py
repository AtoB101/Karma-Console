"""Persistence models for the Krama MVP.

Layering follows the platform blueprint: identity, workspace/tenant,
business domain, connectors, user-arrangeable layout, and the protected
execution core (intent, task, execution, approval, audit).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RiskLevel(str, enum.Enum):
    R0 = "R0"  # read-only / information
    R1 = "R1"  # reversible, low risk (drafts)
    R2 = "R2"  # limited external impact
    R3 = "R3"  # significant / hard to reverse (payments, bulk)
    R4 = "R4"  # critical / irreversible


class TaskStatus(str, enum.Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class Role(str, enum.Enum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


class ConnectorType(str, enum.Enum):
    KARMA_PAYMENT = "karma_payment"
    ECOMMERCE_INDEPENDENT = "ecommerce_independent"
    ECOMMERCE_SOCIAL = "ecommerce_social"
    TAKEOUT_DELIVERY = "takeout_delivery"
    ECOMMERCE_MARKETPLACE = "ecommerce_marketplace"
    FOREIGN_TRADE = "foreign_trade"
    LOGISTICS = "logistics"
    CROSS_BORDER = "cross_border"


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class ShipmentStatus(str, enum.Enum):
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    RETURNED = "RETURNED"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class Decision(str, enum.Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    PENDING = "PENDING"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    region: Mapped[str] = mapped_column(String(8), default="CN", index=True)
    business_category: Mapped[str] = mapped_column(String(64), default="general")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    products: Mapped[list["Product"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceSettingSnapshot(Base):
    """Immutable version history of Workspace.settings for safe rollback.

    Every PUT /settings stores the prior state as a numbered snapshot so a
    bad model/chat/karma/SMTP configuration can be rolled back in one step.
    Secrets remain encrypted at rest because snapshots copy the settings dict
    as-is (api_key_enc/token_enc/password_enc, never plaintext).
    """

    __tablename__ = "workspace_setting_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped["Workspace"] = relationship(back_populates="memberships")


class MerchantProfile(Base):
    __tablename__ = "merchant_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True, index=True)
    shop_name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(64), default="general")  # food/clothing/housing/transport
    region: Mapped[str] = mapped_column(String(8), default="CN")
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64), default="general")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published
    stock: Mapped[int] = mapped_column(Integer, default=0)
    photo_url: Mapped[str] = mapped_column(String(2048), default="")
    specs: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str] = mapped_column(Text, default="")
    qualifications: Mapped[list] = mapped_column(JSON, default=list)
    layout_area_id: Mapped[str | None] = mapped_column(ForeignKey("layout_areas.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped["Workspace"] = relationship(back_populates="products")


class Order(Base):
    """A customer order placed through an e-commerce channel."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(48), default="ecommerce_independent")
    order_reference: Mapped[str] = mapped_column(String(160), default="", index=True)
    provider_reference: Mapped[str] = mapped_column(String(160), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.CREATED.value, index=True)
    payment_status: Mapped[str] = mapped_column(String(16), default="unpaid")
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    customer: Mapped[dict] = mapped_column(JSON, default=dict)
    items: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class StockMovement(Base):
    """Auditable inventory change ledger (receive / ship / adjust)."""

    __tablename__ = "stock_movements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    delta: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(32), default="adjust")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Shipment(Base):
    """Fulfilment record for a paid order."""

    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(64), default="")
    tracking_number: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(16), default=ShipmentStatus.SHIPPED.value)
    shipped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class Customer(Base):
    """A shopper identity keyed by workspace + chat channel + external id.

    The agent accumulates preferences, purchase history references and tags so
    later replies can quote, introduce products and follow up with context.
    """

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("workspace_id", "channel", "external_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(48), default="chat")
    external_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(16), default="new")  # new | lead | won | lost
    # P0 获客底座：本地规则评分/分级/来源/负责人/跟进时间
    score: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str] = mapped_column(String(8), default="")  # H | A | B | C
    source: Mapped[str] = mapped_column(String(64), default="")
    owner_id: Mapped[str] = mapped_column(String(32), default="")
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_follow_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    satisfaction: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Conversation(Base):
    """An open thread between a customer and the workspace agent."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(48), default="chat")
    external_id: Mapped[str] = mapped_column(String(160), default="")
    state: Mapped[str] = mapped_column(String(24), default="open")
    summary: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    """One inbound/outbound message inside a conversation (durable memory)."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(8), default="in")
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Script(Base):
    """A reusable sales script plus conversion metrics for continuous learning."""

    __tablename__ = "scripts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    sends: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkspaceFunction(Base):
    """A user-defined function mounted into a workspace functional area.

    Users configure their own capabilities (receipt URL reads, inventory
    feeds, Karma payment actions, HTTP writebacks) as data instead of
    hardcoding new tools. Execution always flows through the unified
    gateway; defining a function never grants backend permissions.
    """

    __tablename__ = "workspace_functions"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(24), default="http_read")  # http_read | http_write | karma | model
    category: Mapped[str] = mapped_column(String(32), default="general")  # receipt | inventory | pricing | orders | general
    endpoint_url: Mapped[str] = mapped_column(String(2048), default="")
    method: Mapped[str] = mapped_column(String(8), default="GET")
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    auth: Mapped[dict] = mapped_column(JSON, default=dict)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str] = mapped_column(String(4), default=RiskLevel.R0.value)
    side_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotent: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stage: Mapped[str] = mapped_column(String(16), default="test")  # test | live
    parent_area_id: Mapped[str | None] = mapped_column(ForeignKey("layout_areas.id"), nullable=True, index=True)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ModuleVisibility(str, enum.Enum):
    PRIVATE = "private"
    PUBLISHED = "published"


class ModuleStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class Module(Base):
    """A packaged, publishable function that can be installed across workspaces.

    Drafts stay private to the author workspace. Publishing strips embedded
    secrets so the marketplace never leaks credentials; installers supply
    their own auth via overrides.
    """

    __tablename__ = "modules"
    __table_args__ = (UniqueConstraint("author_workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    author_workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(24), default="http_read")
    category: Mapped[str] = mapped_column(String(32), default="general")
    endpoint_url: Mapped[str] = mapped_column(String(2048), default="")
    method: Mapped[str] = mapped_column(String(8), default="GET")
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    auth: Mapped[dict] = mapped_column(JSON, default=dict)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str] = mapped_column(String(4), default=RiskLevel.R0.value)
    side_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotent: Mapped[bool] = mapped_column(Boolean, default=True)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    min_role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER.value)  # minimum role allowed to install/run
    visibility: Mapped[str] = mapped_column(String(16), default=ModuleVisibility.PRIVATE.value)
    status: Mapped[str] = mapped_column(String(16), default=ModuleStatus.DRAFT.value)
    version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ConnectorInstance(Base):
    __tablename__ = "connector_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connector_type: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(160))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LayoutArea(Base):
    """A user-arrangeable functional area (widget) inside a workspace.

    Connectors and tools are surfaced as widgets that the user places and
    reorders here; placing a connector does not grant execution rights.
    """

    __tablename__ = "layout_areas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(16), default="connector")  # connector | tool | module | function | data_source
    target_type: Mapped[str] = mapped_column(String(48), default="")
    target_id: Mapped[str] = mapped_column(String(32), default="")
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("layout_areas.id"), nullable=True, index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stage: Mapped[str] = mapped_column(String(16), default="live")  # test | live
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Intent(Base):
    """Structured task intent produced before any side effect."""

    __tablename__ = "intents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    goal: Mapped[str] = mapped_column(Text)
    success_criteria: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[list] = mapped_column(JSON, default=list)
    prohibited: Mapped[list] = mapped_column(JSON, default=list)
    budget_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(4), default=RiskLevel.R0.value)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    intent_id: Mapped[str] = mapped_column(ForeignKey("intents.id"))
    status: Mapped[str] = mapped_column(String(24), default=TaskStatus.CREATED.value, index=True)
    plan: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(160))
    risk_level: Mapped[str] = mapped_column(String(4))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING.value)
    idempotency_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    confirm_code: Mapped[str] = mapped_column(String(12), default="")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True, default="")
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=ExecutionStatus.PENDING.value)
    risk_level: Mapped[str] = mapped_column(String(4), default=RiskLevel.R0.value)
    idempotency_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(160))
    resource: Mapped[str] = mapped_column(String(160), default="")
    risk_level: Mapped[str] = mapped_column(String(4), default=RiskLevel.R0.value)
    decision: Mapped[str] = mapped_column(String(16), default=Decision.ALLOWED.value)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    data_tier: Mapped[str] = mapped_column(String(16), default="INTERNAL")
    data_labels: Mapped[str] = mapped_column(String(255), default="")
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatTokenIndex(Base):
    """O(1) lookup index for inbound chat tokens; avoids scanning every workspace settings JSON."""

    __tablename__ = "chat_token_index"
    __table_args__ = (UniqueConstraint("token_hash"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VirtualNumber(Base):
    """AI 专属虚拟号映射占位。

    对外只展示 ``virtual_number``；真实号码以 E2EE 密文 ``real_encrypted``
    保存（服务端不可解）。真实来电/短信转接由运营商隐私号服务完成（外部
    依赖，见 ``docs/EXTERNAL_DEPENDENCIES.md``），本地只维护映射与状态。
    """

    __tablename__ = "virtual_numbers"
    __table_args__ = (UniqueConstraint("workspace_id", "virtual_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    virtual_number: Mapped[str] = mapped_column(String(32), index=True)
    real_encrypted: Mapped[str] = mapped_column(Text, default="")
    bound_customer_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="available")  # available|bound|released
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class E2EEBlob(Base):
    """Owner-only E2EE ciphertext envelope stored server-side; the server holds
    no key and never decrypts. ``blind_index`` supports exact-equality lookup
    only (no range/fuzzy). Encryption/decryption happen on the client.
    """

    __tablename__ = "e2ee_blobs"
    __table_args__ = (UniqueConstraint("workspace_id", "owner_ref", "field"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_ref: Mapped[str] = mapped_column(String(120), index=True)
    field: Mapped[str] = mapped_column(String(48), index=True)
    ciphertext: Mapped[str] = mapped_column(Text, default="")
    nonce: Mapped[str] = mapped_column(String(64), default="")
    alg: Mapped[str] = mapped_column(String(32), default="aes-gcm")
    blind_index: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DevicePairing(Base):
    """Registered device for key sync. Server stores only the Passkey/credential
    handle and metadata; it never stores the master key. A one-time pair code
    authorizes a new device from an already-authorized one.
    """

    __tablename__ = "device_pairings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    device_name: Mapped[str] = mapped_column(String(120), default="")
    credential_handle: Mapped[str] = mapped_column(String(255), default="")
    pair_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|authorized|revoked
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    authorized_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class RegionPolicyRule(Base):
    """Region-aware policy rules. Values are placeholders pending legal review."""

    __tablename__ = "region_policy_rules"
    __table_args__ = (UniqueConstraint("region", "rule_type"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    region: Mapped[str] = mapped_column(String(8), index=True)
    rule_type: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")


class Todo(Base):
    """Owner-facing action item triaged from agent activity.

    kind: must_confirm (owner confirmation required), needs_reply (owner
    should reply), info (low priority / archived notice). confirm_code is
    set for must_confirm items so the owner can approve by entering the
    numeric code sent by email.
    """

    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="info")  # must_confirm | needs_reply | info
    title: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done | cancelled
    confirm_code: Mapped[str] = mapped_column(String(12), default="")
    ref_type: Mapped[str] = mapped_column(String(32), default="")
    ref_id: Mapped[str] = mapped_column(String(32), default="")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Goal(Base):
    """A long-running objective the agent tracks in the background.

    Muse-style: the agent keeps a durable goal, works toward it across
    sessions and comes back to the owner only when progress is made,
    something needs a decision, or the target date is near/past.
    """

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[str] = mapped_column(Text, default="")
    progress_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | done | cancelled
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AIAgent(Base):
    """Persistent, owner-configurable AI manager (plan chapter 5).

    Each manager has an explicit role, responsibility list, allowed-tool
    allowlist, memory policy and execution limits. Tool permissions are still
    enforced server-side by the gateway; this allowlist is an additional,
    narrower bound that the orchestrator surfaces to the model.
    """

    __tablename__ = "ai_agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(64), default="store_manager")
    description: Mapped[str] = mapped_column(Text, default="")
    responsibilities: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    memory_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_limits: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | test | live | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentMemory(Base):
    """Durable per-workspace memory the agent can read and write.

    Muse-style: preferences and facts survive across conversations so the
    agent does not re-ask the owner and keeps behavior consistent.
    """

    __tablename__ = "agent_memories"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(24), default="preference")  # preference | fact | note
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CommercePolicy(Base):
    """Owner-set guardrails for how the agent trades on the merchant's behalf.

    The agent may quote, negotiate and accept orders autonomously inside these
    limits. Anything outside them must be escalated to the owner.
    """

    __tablename__ = "commerce_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True, index=True)
    auto_accept_orders: Mapped[bool] = mapped_column(Boolean, default=False)
    max_discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    approval_threshold_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)
    payment_method: Mapped[str] = mapped_column(String(24), default="karma")
    terms: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

class WorkspacePanel(Base):
    """A user-placed panel in the neural console, persisted server-side.

    Panels are created from constrained configs validated against
    app.panels.COMPONENT_REGISTRY; invalid configs are never enabled.
    Layout positions live in Workspace.settings["neural_layout"] so the whole
    console layout is versioned with the workspace instead of browser storage.
    """

    __tablename__ = "workspace_panels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    panel_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    data_source: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | live | disabled
    version: Mapped[int] = mapped_column(Integer, default=1)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("workspace_panels.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ScheduledJob(Base):
    """Owner-registered recurring/conditional workflow (plan M05).

    The trigger is a declarative dict; the goal/steps are executed through the
    unified gateway exactly like interactive tasks, so permission, policy,
    approval, idempotency and audit all still apply.
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    trigger: Mapped[dict] = mapped_column(JSON, default=dict)  # {"type":"interval","minutes":N}
    goal: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkspacePanelSnapshot(Base):
    """Version history of a workspace panel for safe rollback (plan 4.5)."""

    __tablename__ = "workspace_panel_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    panel_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    panel_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    data_source: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BackupRecord(Base):
    """Instance-level database backup (plan M10: recovery center).

    Backups snapshot the whole deployment database (all workspaces), not a
    single workspace. They are managed through the workspace-owner gate;
    restore additionally requires an explicit confirmation flag and records
    an R4 audit event. `status` tracks the backup lifecycle: ok | restored |
    pruned | failed.
    """

    __tablename__ = "backup_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)  # acting owner's workspace (audit linkage)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BriefingSnapshot(Base):
    """Manual archive of a daily business briefing for local history review.

    One row per workspace + calendar day; re-archiving the same day overwrites
    the stored payload. Read-only history; no external sync.
    """

    __tablename__ = "briefing_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD (UTC)
    period: Mapped[str] = mapped_column(String(8), default="daily")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    """P1 售后/投诉工单生命周期：new→processing→waiting_reply→closed，任意态可 escalated。"""

    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    order_id: Mapped[str] = mapped_column(String(32), default="")
    channel: Mapped[str] = mapped_column(String(32), default="chat")
    type: Mapped[str] = mapped_column(String(24), default="other")  # complaint|refund|aftersale|other
    priority: Mapped[str] = mapped_column(String(8), default="P2")    # P0|P1|P2
    status: Mapped[str] = mapped_column(String(16), default="new")   # new|processing|waiting_reply|closed|escalated
    assignee_id: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Faq(Base):
    """P1 FAQ 知识库：标准问答，供接待命中返回标准答案。"""

    __tablename__ = "faqs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    question: Mapped[str] = mapped_column(String(200), default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TicketEvent(Base):
    """工单审计时间线：每次创建/认领/状态流转/回复/关闭追加一条，全程留痕可查。"""

    __tablename__ = "ticket_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    ticket_id: Mapped[str] = mapped_column(String(32), index=True)
    event: Mapped[str] = mapped_column(String(24), default="created")  # created|claimed|status_change|reply|escalated|closed
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16), default="")
    actor_label: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowGraph(Base):
    """可执行 DAG 工作流图（类Coze）：节点+边落库，运行前经 app.dag 校验与拓扑规划。

    nodes: [{id, action, label, x, y, config}]；edges: [{from, to}]。
    危险动作（资金/不可逆/改政策）只进待拍板，绝不自动执行。
    """

    __tablename__ = "workflow_graphs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="未命名工作流")
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|published
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChangeRequest(Base):
    """受控开发 Agent 变更单：草案+回归门禁+老板审批，绝不自动写盘/装依赖/重启。

    状态机 draft -> proposed -> approved/rejected -> applied（人工落盘后手动确认）。
    """

    __tablename__ = "change_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    requirement: Mapped[str] = mapped_column(Text, default="")
    domains: Mapped[list] = mapped_column(JSON, default=list)
    change_points: Mapped[list] = mapped_column(JSON, default=list)
    impact: Mapped[list] = mapped_column(JSON, default=list)
    test_plan: Mapped[list] = mapped_column(JSON, default=list)
    rollback: Mapped[str] = mapped_column(Text, default="")
    draft: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(4), default="R1")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    regression_status: Mapped[str] = mapped_column(String(8), default="pending")
    forbidden_hits: Mapped[list] = mapped_column(JSON, default=list)
    requested_by: Mapped[str] = mapped_column(String(80), default="")
    reviewed_by: Mapped[str] = mapped_column(String(80), default="")
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ContentItem(Base):
    """内容流水线条目：文案/视频，Agent 起草 -> CEO 拍板 -> 社交运营发布。

    状态机见 app/content_pipeline.py；未 approved 不得发布，发布需真实渠道凭证。
    """

    __tablename__ = "content_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="copy", index=True)  # copy | video
    title: Mapped[str] = mapped_column(String(200), default="")
    topic: Mapped[str] = mapped_column(Text, default="")  # 选题/目标人群/输入要求
    body: Mapped[str] = mapped_column(Text, default="")  # 文案正文 / 视频脚本
    assets: Mapped[dict] = mapped_column(JSON, default=dict)  # 卖点/分镜/参数单/热门点
    model_id: Mapped[str] = mapped_column(String(32), default="")  # 选用模型（注册表 id）
    status: Mapped[str] = mapped_column(String(18), default="draft", index=True)
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    reviewed_by: Mapped[str] = mapped_column(String(80), default="")
    target_channels: Mapped[list] = mapped_column(JSON, default=list)
    source_ref: Mapped[str] = mapped_column(String(120), default="")  # 关联商品/选题来源
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # 发布后数据/热门分析
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SocialComment(Base):
    """评论箱：真实抓取留渠道凭证位；可先手动录入建档。回复仅草稿，发送需确认。"""

    __tablename__ = "social_comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    content_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    platform: Mapped[str] = mapped_column(String(24), default="manual")
    external_id: Mapped[str] = mapped_column(String(80), default="")
    author: Mapped[str] = mapped_column(String(80), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    sentiment: Mapped[str] = mapped_column(String(12), default="neutral")
    status: Mapped[str] = mapped_column(String(16), default="new")
    # new | draft_reply | approved_reply | sent | ignored
    reply_draft: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrendingVideo(Base):
    """热门视频建档：指标由录入/回执提供，热门点由纯函数确定性提炼，不联网抓取。"""

    __tablename__ = "trending_videos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    platform: Mapped[str] = mapped_column(String(24), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(60), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    hot_points: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChannelAccount(Base):
    """统一渠道账号接入：只登记官方合规渠道，凭证加密；个人号桥接不接入。

    真实收发消息需对应平台凭证与 webhook；本机不伪造 connected。
    """

    __tablename__ = "channel_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(24), default="webchat", index=True)
    account_label: Mapped[str] = mapped_column(String(80), default="")
    identifier: Mapped[str] = mapped_column(String(160), default="")
    credential_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # credential_required | configured | error
    status: Mapped[str] = mapped_column(String(20), default="credential_required")
    risk_note: Mapped[str] = mapped_column(String(200), default="")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

