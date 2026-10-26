# 仓库审查报告 + 架构差距清单（主控 Agent 输出）

> 基线：`C:\Users\Administrator\Desktop\krama agent`
> 审查时间：2026-10-19。测试基线：168 passed, 1 skipped。

## 1. 技术栈与形态

- Python 3.12，FastAPI，SQLAlchemy 2.0，Pydantic 2，httpx，SQLite。
- 模块化单体：后端集中在 `app/*.py`（36 个模块），前端单文件 `app/static/console.html`（约 1879 行原生 JS，无构建步骤）。
- 测试 `tests/` 30 个文件，扁平结构，pytest + FastAPI TestClient。

## 2. 现有能力盘点（对照第一阶段业务链）

| 链路环节 | 现状 | 位置 |
| --- | --- | --- |
| 用户输入需求 | 已实现：对话 + 自然语言入口 + 语音/文件上传预留 | `app/chat.py`、`app/static/console.html` |
| 主脑生成结构化计划 | 已实现：规划器 JSON 契约 + 少样本 + 自纠 + 确定性解析回退 | `app/chat.py` |
| 动态面板/功能创建 | 已实现：`WorkspaceFunction` + `LayoutArea`（test→live 两级） | `app/functions.py`、`app/main.py` |
| 连接授权数据源 | 已实现：8 类连接器 + SSRF 防护 + 凭证加密 + 回执核验 | `app/connectors.py`、`app/crypto.py` |
| 经理人执行任务 | 已实现：6 角色 AI 团队 + 统一网关（权限→政策→审批→幂等→执行→核验→审计） | `app/multiagent.py`、`app/roles.py`、`app/gateway.py` |
| 结果验证 | 已实现：回执 verify-before-complete + 主脑安全提示 | `app/connectors.py`、`app/brain.py` |
| 记录保存 | 已实现：append-only 审计 + Task/Execution + 主脑事件流 | `app/audit.py`、`app/workflow.py` |
| 用户查看/反馈 | 已实现：GPT 式聊天 + 待办/审批 + 神经控制台 | `app/static/console.html` |

结论：第一阶段业务链「大体已跑通」，这不是从零起步；规范要求的是「冻结契约、补齐缺口、真实集成验收」，而不是重写。

## 3. 架构差距清单（需在阶段 1 补齐的硬缺口）

- G1 面板是硬编码 JS 渲染，不是「受约束 JSON 配置 + 组件注册表」；无 panel_config_schema、无未注册组件拒绝、无面板版本回滚。
- G2 无 WebSocket/SSE 实时推送，主脑事件流与面板刷新依赖 3 秒轮询。
- G3 面板之间只有「主脑↔面板」星形神经线，无面板到面板的直接通信协议。
- G4 无机器可读的任务交接契约（规范第四节），任务依赖与 allowed_paths 未入仓库。
- G5 测试为扁平目录，缺 contract / e2e / security / recovery 分层，缺故障注入与并发/重启恢复用例。
- G6 密钥无过期与轮换；模块无运行沙箱；Prompt Injection 仅启发式守卫，无对抗测试。
- G7 工作流缺显式重试/退避策略与「外部超时先查真实状态再决定」的确定性重试判定（当前有回执核验，但未覆盖全部连接器动作）。
- G8 语音入口未实现（前端仅文字/文件/图片）。

## 4. 技术债（非阻塞，记录在案）

- `main.py` 达 1319 行、`tools.py` 1409 行、`chat.py` 1082 行，均偏大，需按模块边界拆分但不属于阶段 1 验收项。
- 前端单文件 1879 行，缺少组件化与 schema 驱动。
- 测试依赖 session 级 TestClient 与真实 SQLite，缺少显式 fixture 工厂分层。

## 5. 运行与约束

- 环境：Windows PowerShell，Python 3.12。
- 启动：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8010`。
- 测试：`python -m pytest -q -o addopts=""`。
- 无真实 Karma 生产凭证与真实电商回执地址；真实资金链路只能 wire/mock 验证，不可动真实资金。