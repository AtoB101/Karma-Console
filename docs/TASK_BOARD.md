# 主控任务看板（按总纲执行）

> 唯一总纲：`docs/MASTER_PLAN_V1.md`（AI Super Console：Codex 多 Agent 协作开发总规范 V1.0）。
> 主控 Agent 全程指挥；每项验收通过即上报。交付以真实测试为准，不以自述为准；测试 Agent 拥有一票否决权。

## 任务单

| ID | 目标 | 负责人 | 允许修改路径 | 验收标准 | 依赖 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| T1 UI-001 | 前端 schema 驱动面板渲染 | Agent 2 前端 | `app/static/console.html` | 从 `GET /panels/registry` 取组件；添加面板走 `POST /panels/validate`；无效配置报错不渲染；未注册组件拒绝；旧面板不因新配置失败而消失 | CONTRACT-001 | ✅ 完成 |
| T2 TOOL-001 | 外部超时先查真实状态 | Agent 4 工作流 | `app/functions.py`、`app/connectors.py` | 外部超时/网络错误返回 `UNKNOWN` + `needs_reconcile`，不自动判失败；幂等键去重；verify 作为查真实状态路径 | CONTRACT-001 | ✅ 完成 |
| T3 TEST-001 | 恢复与并发注入测试 | Agent 6 测试 | `tests/recovery/` | 超时对账、外部不可达、重复提交三案例有可复现测试 | T2 | ✅ 完成 |
| T4 VERIFY | 全量集成验收 | 主控 | 全仓库（只读+汇总） | 全量 pytest 全绿（184 passed, 1 skipped）；e2e 链跑通；输出验收报告 | T1-T3 | ✅ 完成 |
| T5 PANEL-002 | 面板服务端持久化 | Agent 2 前端 + 主控 | `app/models.py`、`app/schemas.py`、`app/main.py`、`app/static/console.html` | 面板 CRUD 落库（WorkspacePanel）；未注册 type/非法配置 400；更新失败旧版本可用；布局存 workspace.settings；前端 localStorage 仅离线缓存；测试可复现 | T1 | ✅ 完成 |
| T6 BRAIN-001 | 主脑事件 SSE 实时推送 | Agent 5 主脑 + 主控 | `app/brain.py`、`app/main.py`、`app/static/console.html` | `GET /workspaces/{id}/brain/stream` SSE（ready/snapshot/event），从审计日志派生；`once=1` 可测；前端 fetch 流接入 + 轮询兜底；断线重连；权限经 require_actor | T4 | ✅ 完成 |
| T7 CHAT-001 | 场景问答模型驱动 | Agent 3 对话 + 主控 | `app/chat.py`、`app/static/console.html` | 「我是xx老板，你能帮我做什么」先登记店铺/技能，再进入真实 Agent 循环推理执行（有模型时）；无模型保持确定性兜底；前端渲染执行链路 trace | T4 | ✅ 完成 |
| T8 FN-001 | 功能生命周期打通 | Agent 2 前端 + Agent 4 工作流 | `app/main.py`、`app/static/console.html` | 创建功能后 catalog 即时刷新并含 `acceptance`；「▶ 运行/测试」可点通；`run_function`/`promote_function` 走网关；上线后功能区与 catalog 同步 live | T4 | ✅ 完成 |
| T9 APPROVE-001 | 审批/待办闭环 + 超时邮件 | Agent 5 安全 + Agent 6 测试 | `app/scheduler.py`、`tests/test_remaining_gaps.py` | 待办/审批可点批准/拒绝并同步关联单；确认码校验；超 10 分钟未处理发邮件升级（approval+todo 双路径测试） | T4 | ✅ 完成 |
| T10 LIVE-001 | 真实模型业务回归 | 主控 + Agent 6 | `e2e_scenarios.py`、`tests/e2e/test_live_model_scenarios.py` | 真实 DeepSeek 跑 10/10 场景通过（画像/上架/库存/接单/报价/支付审批/发货/签收/库存扣减）；无 key 自动 skip | T7 | ✅ 完成（真实模型 10/10） |
| T11 MA-001 | 多 Agent 并行规划 + 冲突化解 + 长时记忆 | Agent 3 + 主控 | `app/multiagent.py`、`tests/test_multiagent.py` | Worker 规划并发 gather；重复变异目标被 CONFLICT 去重不二次执行；编排注入近期已完成任务历史 | T4 | ✅ 完成 |
| T12 MARKET-001 | 控制台模块市场 UI | Agent 2 + 主控 | `app/static/console.html` | 左侧模块市场浏览已发布模块并一键安装；安装后刷新 catalog/功能区；空态可读 | T4 | ✅ 完成 |
| T13 SETTINGS-001 | 设置版本化与回滚 | Agent 1 + 主控 | `app/models.py`、`app/main.py`、`tests/test_settings.py` | 每次 PUT 存快照；历史可查；owner 可回滚且回滚本身版本化；member 回滚 403；密钥保持加密 | T4 | ✅ 完成 |
| T14 GATE-001 | 上线门槛与成功标准 | 主控 | `docs/LAUNCH_GATE.md` | 15.3 上线门槛 + 19 成功标准（含分母/统计方法）落地 | T4 | ✅ 完成 |
| T15 AGENT-001 | 经理人持久化实体 | Agent 3 + 主控 | `app/models.py`、`app/schemas.py`、`app/main.py`、`app/chat.py`、`app/multiagent.py`、`tests/test_agents.py` | AIAgent CRUD + 租户隔离 + 工具白名单校验 + 运行时绑定（编排器识别自定义经理人并约束其工具） | T4 | ✅ 完成 |
| T16 PANEL-003 | 组件注册表 14 类 + 面板版本回滚 | Agent 2 + 主控 | `app/panels.py`、`app/models.py`、`app/main.py`、`app/static/console.html`、`tests/contract/*` | 14 基础组件注册 + allows_write 元数据 + 前端类型渲染器 + 面板快照历史/回滚（owner） | T4 | ✅ 完成 |
| T17 PLAN-001 | 任务计划正式 Schema | Agent 3 + 主控 | `app/plan.py`、`app/chat.py`、`tests/test_plan.py` | `validate_agent_plan` 结构化校验并回灌纠错；非法步骤/params 被拒 | T4 | ✅ 完成 |
| T18 JOB-001 | 条件触发工作流 | Agent 4 + 主控 | `app/models.py`、`app/schemas.py`、`app/main.py`、`app/scheduler.py`、`tests/test_jobs.py` | ScheduledJob CRUD + 到期经网关执行 + 下次运行时间推进 | T4 | ✅ 完成 |
| T19 VOICE-001 | 语音输入（M12） | Agent 2 + 主控 | `app/static/console.html` | 麦克风按钮 SpeechRecognition 填入输入框（不支持浏览器优雅降级） | T4 | ✅ 完成 |

## 执行顺序
1. T1、T2 无互相依赖，可并行（写范围互斥）。
2. T3 依赖 T2 的超时语义。
3. T4 汇总验收。
4. T5、T6 可并行（后端写范围互斥），完成后 T4 复验全量。

## 验收门禁（安全）
- 密钥不入日志/模型提示；越权拒绝；R2/R3 需授权；危险动作网关硬阻断；主脑提示不阻断安全错误。