# 架构说明（ARCHITECTURE）

> 描述当前已实现的架构，而非目标蓝图。目标蓝图见 `karma.txt`。

## 1. 总体形态

模块化单体（FastAPI 单进程 + SQLite 单库）。所有写路径强制经过统一执行网关；模型、连接器、用户功能都不直接触碰业务对象。

```
浏览器(console.html)
   │ HTTP (Bearer API Key + X-Workspace-Id)
   ▼
FastAPI 路由 (app/main.py)
   ├─ /users /workspaces /products /layout /functions /modules /settings /chat
   ├─ /gateway/execute        ──────────────┐
   └─ /chat/inbound (聊天工具 webhook) ──────┤
                                            ▼
                                    统一执行网关 app/gateway.py
                                    (权限→政策→审批→幂等→执行→核验→审计)
                                            │
              ┌─────────────────────────────┼──────────────────────────────┐
              ▼                             ▼                              ▼
       app/tools.py                 app/functions.py               app/connectors.py
      内置工具                      用户自定义功能                  app/karma.py (Karma)
                                    (SSRF 防护/回执核验)            (真实客户端或 stub)
                                            │
                                            ▼
                                    SQLAlchemy / SQLite (app/db.py)
```

## 2. 分层

- 入口层：`app/main.py` 路由 + `app/static/console.html`；`/chat/inbound` 是聊天工具统一入口。
- 编排层：`app/chat.py`（确定性解析器 + 可选模型 JSON 计划）、`app/workflow.py`（任务状态机）。
- 执行核心：`app/gateway.py` 唯一执行入口，内聚权限/政策/审批/幂等/核验/审计。
- 能力层：`app/tools.py`（内置）、`app/functions.py`（用户定义）、`app/connectors.py` + `app/karma.py`（外部连接）、`app/llm.py`（模型路由）。
- 领域与持久化：`app/models.py`（商品/订单/库存流水/运单/布局/函数/模块等）、`app/db.py`（含增量迁移）、`app/seed.py`（占位地区规则）。
- 治理：`app/policy.py`（地区规则）、`app/risk.py`（分级）、`app/security.py`（认证/权限）、`app/audit.py`（审计）。

## 3. 请求流（以聊天为例）

1. `POST /workspaces/{ws}/chat` → `app/chat.py::handle_message`。
2. 若工作区配置了模型：`_call_model` 让模型输出 JSON 计划；`_parse_plan` 解析；`_run_model_steps` 逐条经网关执行。
3. 未配置模型或解析失败：回退 `detect_intent` 确定性解析器。
4. 每条动作都经 `gateway.execute`：`can_execute_reason`（角色）→ `policy_engine.evaluate`（地区）→ `requires_approval`（风险）→ 审批去重 → `ToolSpec.handler` → 回执核验 → `record` 审计。
5. 结果写入 `Intent/Task/Execution/AuditLog`，并返回给前端渲染。

## 4. 不可违背的不变量

- 模型只产出计划或文案，不直接执行；执行唯一入口是网关。
- 用户添加功能/布局/模块不授予后端权限；权限只看 Actor 角色 + 风险级。
- 支付动作恒需审批（`policy.HARD_REASONS` + R3）。
- 外部动作先核验回执，未确认不得标记完成。
- 审计只追加，不修改、不删除。

## 5. 已知架构债

- 单进程 + SQLite：不支持多副本写、无连接池横向扩展。
- 同步 httpx 调用混入 async 路由（`app/karma.py`、`app/llm.py`）：低负载可用，高并发会阻塞事件循环。
- 聊天 token 匹配为全表扫描（`app/main.py::chat_inbound`），规模化需索引/专用表。
- 密钥加密存储已实现（`app/crypto.py` Fernet）；工作流持久化（断点续跑/超时/补偿）已实现（`app/workflow.py`）。
- 无模块沙箱、无消息总线、无后台调度（对账/超时目前按需触发）。
