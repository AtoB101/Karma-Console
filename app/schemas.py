"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class WorkspaceCreate(BaseModel):
    name: str
    region: str = "CN"
    business_category: str = "general"


class UserCreate(BaseModel):
    email: str
    display_name: str = ""


class ApiKeyOut(BaseModel):
    api_key: str
    prefix: str


class MerchantCreate(BaseModel):
    shop_name: str
    category: str = "general"
    region: str = "CN"
    currency: str = "CNY"


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1)
    category: str = "general"
    price: float = Field(default=0.0, ge=0)
    photo_url: str = ""
    specs: dict = Field(default_factory=dict)
    detail: str = ""
    qualifications: list = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    category: str | None = None
    price: float | None = Field(default=None, ge=0)
    status: Literal["draft", "published", "archived"] | None = None
    photo_url: str | None = None
    specs: dict | None = None
    detail: str | None = None
    qualifications: list | None = None
    layout_area_id: str | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name must not be blank")
        return v.strip() if v is not None else v


class ConnectorInstall(BaseModel):
    connector_type: str
    name: str
    config: dict = Field(default_factory=dict)


class ConnectorConfig(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    method: str = "GET"
    auth: dict | None = None
    allow_private: bool = False
    signer: str | None = None
    app_key: str | None = None
    app_id: str | None = None
    partner_id: str | None = None
    app_secret: str | None = None
    checkword: str | None = None
    access_token: str | None = None
    api_method: str | None = None
    sign_method: str | None = None
    service_code: str | None = None
    actions: dict | None = None
    verification: dict | None = None


class LayoutAreaUpsert(BaseModel):
    name: str
    kind: str = "connector"
    target_type: str = ""
    target_id: str = ""
    parent_id: str | None = None
    config: dict = Field(default_factory=dict)
    sort_order: int = 0
    enabled: bool = True
    stage: str = "live"


class FileUpload(BaseModel):
    name: str
    content_base64: str


class FunctionCreate(BaseModel):
    name: str
    description: str = ""
    kind: str = "http_read"
    category: str = "general"
    endpoint_url: str = ""
    method: str = "GET"
    headers: dict = Field(default_factory=dict)
    auth: dict = Field(default_factory=dict)
    input_schema: dict = Field(default_factory=dict)
    risk_level: str | None = None
    side_effect: bool = False
    idempotent: bool = True
    enabled: bool = True
    verification: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class ChatMessage(BaseModel):
    message: str
    model_id: str | None = None


class SettingsUpdate(BaseModel):
    model: dict | None = None
    chat: dict | None = None
    karma: dict | None = None
    smtp: dict | None = None
    owner_email: str | None = None


class ChatInbound(BaseModel):
    token: str
    message: str
    channel: str = "chat"
    external_id: str = ""
    customer_name: str = ""


class ModuleCreate(BaseModel):
    name: str
    description: str = ""
    kind: str = "http_read"
    category: str = "general"
    endpoint_url: str = ""
    method: str = "GET"
    headers: dict = Field(default_factory=dict)
    auth: dict = Field(default_factory=dict)
    input_schema: dict = Field(default_factory=dict)
    risk_level: str | None = None
    side_effect: bool = False
    idempotent: bool = True
    verification: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    min_role: str = "member"
    source_function_id: str | None = None


class ModuleUpdate(BaseModel):
    description: str | None = None
    kind: str | None = None
    category: str | None = None
    endpoint_url: str | None = None
    method: str | None = None
    headers: dict | None = None
    auth: dict | None = None
    input_schema: dict | None = None
    risk_level: str | None = None
    side_effect: bool | None = None
    idempotent: bool | None = None
    verification: dict | None = None
    config: dict | None = None
    min_role: str | None = None
    version: str | None = None
    visibility: str | None = None
    status: str | None = None


class ModuleInstall(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    auth: dict | None = None


class ModuleReview(BaseModel):
    decision: str  # approve | reject
    note: str = ""


class LayoutAreaUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    config: dict | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    stage: str | None = None


class LayoutAreaReorder(BaseModel):
    area_ids: list[str] = Field(default_factory=list)


class FunctionUpdate(BaseModel):
    description: str | None = None
    kind: str | None = None
    category: str | None = None
    endpoint_url: str | None = None
    method: str | None = None
    headers: dict | None = None
    auth: dict | None = None
    input_schema: dict | None = None
    risk_level: str | None = None
    side_effect: bool | None = None
    idempotent: bool | None = None
    enabled: bool | None = None
    verification: dict | None = None
    config: dict | None = None


class TaskCreate(BaseModel):
    goal: str
    success_criteria: str = ""
    constraints: list = Field(default_factory=list)
    prohibited: list = Field(default_factory=list)
    budget_limit: float | None = None
    risk_level: str | None = None
    steps: list[dict] | None = None


class ExecuteRequest(BaseModel):
    tool: str
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class ApprovalDecision(BaseModel):
    approve: bool


class TodoCreate(BaseModel):
    kind: str = "info"
    title: str
    detail: str = ""
    confirm_code: str = ""
    ref_type: str = ""
    ref_id: str = ""


class TodoConfirm(BaseModel):
    code: str


class TodoUpdateStatus(BaseModel):
    status: str


class GoalCreate(BaseModel):
    title: str
    detail: str = ""
    success_criteria: str = ""
    due_at: str | None = None


class GoalUpdate(BaseModel):
    status: str | None = None
    detail: str | None = None
    success_criteria: str | None = None
    progress_note: str | None = None
    due_at: str | None = None


class AgentCreate(BaseModel):
    name: str
    role: str = "store_manager"
    description: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    memory_policy: dict = Field(default_factory=lambda: {"scope": "workspace", "enabled": True})
    execution_limits: dict = Field(default_factory=lambda: {"max_runtime_seconds": 300, "max_tool_calls": 20})
    status: str = "draft"


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    description: str | None = None
    responsibilities: list[str] | None = None
    allowed_tools: list[str] | None = None
    memory_policy: dict | None = None
    execution_limits: dict | None = None
    status: str | None = None


class AgentRunStep(BaseModel):
    tool: str
    params: dict = Field(default_factory=dict)


class AgentRun(BaseModel):
    steps: list[AgentRunStep] = Field(default_factory=list)


class JobCreate(BaseModel):
    name: str
    trigger: dict = Field(default_factory=lambda: {"type": "interval", "minutes": 60})
    goal: str = ""
    steps: list[dict] = Field(default_factory=list)
    enabled: bool = True


class JobUpdate(BaseModel):
    name: str | None = None
    trigger: dict | None = None
    goal: str | None = None
    steps: list[dict] | None = None
    enabled: bool | None = None


class MemoryUpsert(BaseModel):
    key: str
    value: str
    kind: str = "preference"


class CommercePolicyUpdate(BaseModel):
    auto_accept_orders: bool | None = None
    max_discount_percent: float | None = None
    approval_threshold_amount: float | None = None
    currency: str | None = None
    shipping_fee: float | None = None
    payment_method: str | None = None
    terms: str | None = None

class PanelCreate(BaseModel):
    type: str
    title: str = ""
    data_source: str = ""
    status: str = "draft"
    version: int = 1
    permissions: dict = Field(default_factory=dict)
    parent_id: str | None = None


class PanelUpdate(BaseModel):
    type: str | None = None
    title: str | None = None
    data_source: str | None = None
    status: str | None = None
    version: int | None = None
    permissions: dict | None = None
    parent_id: str | None = None


class NeuralLayoutUpdate(BaseModel):
    positions: dict = Field(default_factory=dict)


class BackupCreate(BaseModel):
    reason: str = ""


class BackupRestore(BaseModel):
    confirm: bool = False

