# 模块责任表 + 允许修改路径（Agent 边界）

> 本仓库是模块化单体，目录结构沿用现状；下表把规范中的 7 个 Agent 映射到真实文件所有权。主控只冻结契约，不改专业实现。

## 1. 所有权映射

| 角色 | 允许修改路径 | 禁止 |
| --- | --- | --- |
| 主控 Agent 0 | `docs/*.md`、任务清单文件、集成/构建配置 | 改 `app/tools.py`、`app/chat.py`、`app/gateway.py`、`app/security.py`、`app/models.py` 的实现 |
| 后端 Agent 1 | `app/models.py`、`app/schemas.py`、`app/db.py`、`app/main.py`、`app/seed.py`、`app/workflow.py`、`app/scheduler.py`、迁移 | 改前端 `app/static/console.html`；扩大授权范围；绕过工作流 |
| 前端 Agent 2 | `app/static/console.html`、面板组件与布局相关部分 | 改数据库结构、业务权限、直连外部 API |
| 主脑 Agent 3 | `app/chat.py`、`app/llm.py`、`app/multiagent.py`、`app/roles.py`、`app/brain.py`（规划/交接/记忆部分） | 自行赋予权限、直连生产库、伪造执行结果 |
| 工作流 Agent 4 | `app/tools.py`、`app/connectors.py`、`app/functions.py`、`app/karma.py`、`app/scheduler.py`、`app/workflow.py`、`app/signing.py` | 决定调用权限、绕过安全模块、把超时一律当可重试 |
| 安全 Agent 5 | `app/security.py`、`app/risk.py`、`app/policy.py`、`app/guard.py`、`app/audit.py`、`app/crypto.py`、`app/data_classification.py`、`app/ratelimit.py` | 改业务实现（跨模块修复需开任务） |
| 测试 Agent 6 | `tests/**`、`pytest.ini`、`conftest.py`、测试报告 | 改业务实现让测试通过 |

## 2. 公共契约（主控维护，任何人可评审，不得擅自改）

- `docs/INTERFACE_CONTRACTS.md`：任务契约、API 契约、工具契约、连接器契约、主脑契约、面板契约。
- 修改公共契约前必须影响分析并通知依赖方；冻结后修改需记录原因。

## 3. 协作规则

- 同一文件同一时间只允许一个 Agent 修改；公共接口变更先走契约评审。
- 支持独立工作树时按工作树隔离；否则串行改共享文件、并行做互不冲突任务。
- 完成必须按规范第四节报告「改了什么/新增文件/测试结果/越界/已知问题/接手人」。
- 测试 Agent 拥有一票否决权，但不能改其他模块代码；主控最终审核后交付。