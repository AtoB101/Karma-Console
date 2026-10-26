# AI Super Console 整改对照与实施计划 V1.0

> 对照《AI Super Console 超级控制台 完整工程实施总方案 V1.0》（M01–M12 + 第一至第五章），
> 逐项核对当前代码，给出“保留 / 调整 / 补齐”结论与优先级。
> 开发团队不变、规则不变；本文只界定“内容整改”范围。依据为实际代码与测试，非自述。

## 0. 总结论

- 当前系统在“安全执行链路、真实业务闭环、主脑编排、多 Agent 协作”上已高度对齐方案精神，**不是推倒重来**。
- 需要整改的集中在三类：
  1. **缺实体**：AI 经理人目前只是静态角色表 + 临时 Worker，没有可持久化/可管理的经理人对象。
  2. **缺组件**：组件注册表只有 5 类，方案要求 14 类；前端神经面板是写死的 6 个。
  3. **缺正式契约**：任务计划 JSON 无正式 Schema；面板无版本历史/回滚；工作流无条件触发/持久队列；无语音。
- 技术基线（React+TS / PostgreSQL / Redis / S3 / Docker）在方案 2.4 中明确定义为“建议默认，不是强制”，且“禁止无依据重写现有系统”。**当前 FastAPI + SQLite + 单文件前端保留，生产化阶段再迁移**，不列为整改阻塞项。

## 1. 逐章对照

### 第一章 项目目标与产品定义

| 能力/标准 | 现状 | 整改 |
| --- | --- | --- |
| 能力一 自然语言控制 | 已实现（`app/chat.py` + `app/multiagent.py`） | 无 |
| 能力二 动态工作空间 | 部分（`app/panels.py` + `WorkspacePanel`） | 扩充组件与生命周期（见第四章） |
| 能力三 专业 AI 经理人 | 部分（`app/roles.py` 静态角色 + 临时 Worker） | 补齐持久化经理人实体 |
| 能力四 真实业务执行 | 已实现（`app/tools.py` + `app/connectors.py` 全经网关） | 无（真实渠道仍需平台联调） |
| 能力五 持续工作 | 部分（`app/workflow.py` + `app/scheduler.py`） | 补条件触发 + 持久任务队列 |
| 能力六 安全审计恢复 | 已实现（网关/风险/政策/审计/补偿） | 补备份恢复（外部化） |
| 成功标准 1-10 | 8/10 达标；#4 经理人管理与 #6 条件触发未达标 | 见下方 P0/P1 |

### 第二章 M01–M12 模块对照

| 模块 | 现状文件 | 整改 |
| --- | --- | --- |
| M01 用户交互层 | `app/static/console.html`（单文件） | 保留；补组件渲染 |
| M02 AI 主脑 | `app/chat.py`、`app/multiagent.py` | 正式拆分 8 组件（见第三章） |
| M03 动态面板引擎 | `app/panels.py`、`WorkspacePanel` | 扩充 14 组件 + 版本回滚 |
| M04 AI 经理人运行时 | `app/roles.py`、`app/multiagent.py` | **新增经理人持久化实体** |
| M05 工作流任务引擎 | `app/workflow.py`、`app/scheduler.py` | 条件触发 + 持久队列 |
| M06 工具连接器 | `app/tools.py`、`app/connectors.py` | 无（已达标） |
| M07 数据与记忆层 | `AgentMemory/Goal/Customer/Conversation` | 记忆策略正式化 |
| M08 身份与权限中心 | `app/security.py`、`app/gateway.py` | 无（已达标） |
| M09 安全与风险引擎 | `app/risk.py`、`app/policy.py`、`app/guard.py` | 无（持续对抗注入） |
| M10 审计与恢复中心 | `app/audit.py` + 工作流补偿 | 备份恢复（外部化） |
| M11 监控与通知中心 | `app/observability.py`、`app/notify.py`、`app/brain.py` SSE | 分布式追踪（外部化） |
| M12 多模态交互层 | 仅文本/文件/图片 | **补语音输入** |

### 第三章 AI 主脑 8 组件

- 已有：意图识别、上下文组装、规划器、工具发现、执行协调、结果验证、结果汇总（分散在 `app/chat.py`）。
- 整改：把“权限预检查器”显式化，并输出正式任务计划 JSON Schema（含 version/超时/重试/授权/输出验证）。

### 第四章 动态面板引擎

- 现有 5 类组件：`data_list / overview / todos / approvals / chat`。
- 方案要求 14 类：指标卡、数据表格、趋势图、饼图、筛选器、搜索框、表单、任务列表、日历、报告阅读器、经理人状态卡、审批卡、日志查看器、通知列表。
- 整改：`app/panels.py` 注册表扩充 + 前端 `console.html` 按 schema 渲染；`WorkspacePanel` 增加版本历史与回滚接口（4.5：草稿→测试→启用→归档，失败不覆盖最后有效版本）。

### 第五章 AI 经理人系统

- 现状：`app/roles.py` 是静态团队角色；`app/multiagent.py` 的 Worker 每次对话临时创建，无持久化配置。
- 整改：新增 `AIAgent` 模型（id / workspace_id / name / role / description / responsibilities / allowed_tools / memory_policy{scope,enabled} / execution_limits{max_runtime_seconds,max_tool_calls} / status），配套 CRUD + 运行时绑定 + 多经理人交接记录（复用 `Task`）。

## 2. 整改优先级

- **P0 经理人持久化** ✅：新增 `AIAgent` 模型 + `GET/POST/PATCH/DELETE /workspaces/{id}/agents` + 运行时绑定到多 Agent 编排。这是成功标准 #4 与第五章的核心，改动最大。
- **P1 组件注册表 14 类 + 面板版本回滚** ✅：`app/panels.py` 注册表扩充、`WorkspacePanel` 历史/回滚接口、前端 schema 渲染。
- **P1 任务计划正式 Schema** ✅：`app/schemas.py` 增加 TaskPlan JSON 契约 + 主脑 8 组件显式化。
- **P2 工作流条件触发 + 持久队列** ✅：`app/scheduler.py` 支持按时间/事件触发，持久化任务队列。
- **P3 语音输入** ✅：前端 `SpeechRecognition` 接入 + 后端 `M12`。
- **P3 记忆策略正式化** ✅：已随 `AIAgent.memory_policy` 落地。
- **技术栈迁移（PG/Redis/S3/Docker）**：生产化阶段再做，不阻塞本轮。

## 3. 验收

每项整改完成后：全量 `python -m pytest -q -o addopts=""`、前端 `node --check`、真实 DeepSeek 回归 `e2e_scenarios.py` 10/10、真实服务器冒烟，四项全绿才在 `docs/TASK_BOARD.md` 标记完成。
