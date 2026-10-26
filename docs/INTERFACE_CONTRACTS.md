# 公共接口契约草案（冻结基线）

> 本文由主控维护。任何公共契约变更需评估影响并通知依赖方。字段名与现有代码对齐，冻结后不得擅自改名。

## 1. 任务契约（Agent 交接统一格式）

```yaml
task_id: PANEL-001
title: 实现动态面板配置校验
owner: agent_frontend
reviewer: agent_testing
goal: 渲染前校验面板配置，无效时给出可读错误
inputs: [panel_config_schema, approved_component_registry]
outputs: [panel_validation_logic, panel_validation_tests]
dependencies: [API-001, CONTRACT-001]
allowed_paths: [app/static/console.html, tests/]
forbidden_actions: [修改数据库结构, 绕过权限检查, 执行未批准动态脚本]
acceptance_criteria:
  - 有效配置正常渲染
  - 缺失必要字段被拒绝
  - 未注册组件被拒绝
  - 错误信息可理解
  - 自动化测试通过
completion_requirements: [代码差异, 测试结果, 未完成事项, 潜在风险]
```

## 2. API 契约

- 鉴权：`Authorization: Bearer <api_key>`；租户上下文：`X-Workspace-Id: <workspace_id>`（写操作）。
- 错误统一为 `{"detail": "<可读中文>"}`，HTTP 400/401/403/404/409/422。
- 列表统一 `{"<复数键>": [...]}`：`products / orders / inventory / shipments / customers / connectors / functions / areas / approvals / todos / events`。
- 主脑事件：`GET /workspaces/{id}/brain/events?limit=` → `{"workspace_id", "events":[{id, action, decision, risk_level, resource, reason, data_tier, actor_id, created_at}]}`。
- 主脑预检：`POST /workspaces/{id}/brain/preflight` → `{"workspace_id", "hints":[{code, severity, message}]}`，severity ∈ {warn, danger}。
- 主脑实时流：`GET /workspaces/{id}/brain/stream?interval=&once=` → SSE，帧事件 `ready` / `snapshot` / `event`（`data` 为事件 JSON）；`once=1` 时快照后结束（可测/调试）。事件仍从 append-only 审计日志派生。

## 3. 工具契约（ToolSpec）

- 注册字段：`name, description, risk_level(R0-R4), requires_approval, allow_customer, handler`。
- 执行上下文 `ToolContext`：`db, actor, params, idempotency_key`。
- 所有工具经网关：权限 → 政策 → 审批 → 幂等 → 执行 → 核验 → 审计；模型只提案，不得直接调用。
- 网关结果 `GatewayResult`：`decision ∈ {ALLOWED, DENIED, PENDING}`，`result`，`approval_id`，`execution_id`，`reason`。

## 4. 连接器契约

- `execute(action, payload) -> dict`：必须返回 `receipt_status` 与 `provider_reference`；成功状态集 `{SUCCEEDED, SUCCESS, OK, PAID, COMPLETED}`。
- `verify(action, reference) -> dict`：必须返回 `status`；未确认进入 `UNKNOWN`，不得标记完成。
- 未配置时确定性 stub 仍返回可核验回执，保证离线闭环。

## 5. 主脑契约（brain）

- `preflight_hints(action, params, workspace_id) -> list[{code, severity, message}]`：只给提示，不阻断；危险动作仍由网关硬阻断。
- 已冻结提示码：`funds_terminal / irreversible_delete / sensitive_to_external / credential_exposure / mass_scope`。
- 事件流从 append-only 审计日志派生，不做第二份真相源。

## 6. 面板契约（阶段 1 已实现）

- `panel_config = {panel_id, type, title, data_source, layout, permissions, version, status}`（服务端 `WorkspacePanel` + `GET/POST/PATCH/DELETE /workspaces/{id}/panels`，布局存 `GET/PUT /workspaces/{id}/neural/layout`）。
- 组件必须来自审核过的 `component_registry`；未注册 `type` 拒绝渲染。
- 无效配置（缺字段/非法 type/未授权 data_source）明确报错，不得启用。
- 面板更新失败时旧版本继续可用（版本回滚）。