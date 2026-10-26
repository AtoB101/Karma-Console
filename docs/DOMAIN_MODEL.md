# 领域模型（DOMAIN_MODEL）

> 当前已实现的持久化模型（`app/models.py`），非目标蓝图。

## 1. 实体关系

```
User 1─* ApiKey
User 1─* Membership *─1 Workspace
Workspace 1─1 MerchantProfile
Workspace 1─* Product
Workspace 1─* WorkspaceFunction
Workspace 1─* ConnectorInstance
Workspace 1─* LayoutArea (自引用 parent_id 形成功能区树)
Workspace 1─* Module (author_workspace_id)
Workspace 1─* Intent
Intent 1─1 Task
Workspace 1─* Task 1─* Execution
Workspace 1─* ApprovalRequest *─? Task
Workspace 1─* AuditLog
Product *─? LayoutArea (layout_area_id，归类)
Workspace 1─1 settings(JSON: model / chat)
```

## 2. 核心实体

- **User**：邮箱、显示名。
- **ApiKey**：key_hash、prefix、active（密钥只存哈希）。
- **Workspace**：name、region、business_category、settings(JSON)。
- **Membership**：workspace+user 唯一，role=owner|member|viewer。
- **MerchantProfile**：shop_name、category（衣食住行）、region、currency。
- **Product**：name、category、price、status(draft|published)、layout_area_id。
- **WorkspaceFunction**：name、kind(http_read|http_write|model|karma)、category、endpoint_url、method、headers、auth、input_schema、risk_level、side_effect、idempotent、enabled、verification、config。
- **ConnectorInstance**：connector_type(karma_payment|ecommerce_independent|ecommerce_social)、config、status。
- **LayoutArea**：name、kind(connector|tool|module|function|data_source)、target_type、target_id、parent_id、config、sort_order、enabled。
- **Module**：作者工作区、打包的功能字段、min_role、visibility(private|published)、status(draft|published)、version。
- **Intent**：goal、success_criteria、constraints、prohibited、budget_limit、risk_level、requires_approval。
- **Task**：intent_id、status、plan(JSON)、result(JSON)、error。
- **ApprovalRequest**：action、risk_level、payload、status(PENDING|APPROVED|DENIED|EXPIRED)、idempotency_key。
- **Execution**：task_id、tool_name、input/output、status、risk_level、idempotency_key。
- **AuditLog**：action、resource、risk_level、decision、reason、evidence。
- **RegionPolicyRule**：region、rule_type(require_approval|block)、config、enabled（占位，待法务）。

## 3. 枚举

- RiskLevel：R0(只读) R1(可逆) R2(有限外部影响) R3(重大/难逆) R4(关键/不可逆)。
- TaskStatus：CREATED/PLANNING/PENDING_APPROVAL/APPROVED/EXECUTING/VERIFYING/COMPLETED/FAILED/UNKNOWN/BLOCKED/CANCELLED。
- Role：owner/member/viewer。
- Decision：ALLOWED/DENIED/PENDING。
- ApprovalStatus：PENDING/APPROVED/DENIED/EXPIRED。
- ExecutionStatus：PENDING/RUNNING/SUCCEEDED/FAILED/UNKNOWN。
- ConnectorType：karma_payment/ecommerce_independent/ecommerce_social。
- ModuleVisibility：private/published。ModuleStatus：draft/published。

## 4. 不变式

- ApiKey 只存哈希，不存明文。
- 支付类（R3+）必有 ApprovalRequest 且 APPROVED 后才执行。
- Execution 与 AuditLog 均携带 idempotency_key。
- 租户边界：所有业务对象带 workspace_id，查询必须按其过滤。
- 功能区树：LayoutArea.parent_id 自引用，不允许跨工作区父子。