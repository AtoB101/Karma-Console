# KARMA SUPER CONSOLE

> 本地部署的 AI 企业管理超级控制台 —— 产品库是唯一数据中枢，AI 客服 / 文案 / 视频 / 开发 Agent 围绕它运转，CEO 主脑统一调度，每一件事都有触达、有回执。数据 100% 存在你自己电脑上。

![platform](app/static/scene.png)

## 它能干什么

- **产品库中枢**：商品、库存、订单、客户全部以产品库为唯一事实来源，所有 Agent 实时读取
- **AI 智能客服**：自动接待各渠道客户，基于产品库回答、线索分级、SOP 跟进、转人工工单全程留痕
- **文案 / 视频 / 社交 Agent**：每个 Agent 有独立的专业操作页与模型选择器，作品先经 CEO 拍板再发布
- **开发 Agent**：受控开发新功能（草案 → 回归门禁 → 审批），用户说清需求即可扩展操作台
- **神经调度网**：CEO 主脑分派任务、神经触角直达每个功能面板，固定重复工作高速化、需要动脑的工作有方案与预案
- **全息操作台 UI**：玻璃拟态面板、科幻动效，打开即懂、可直接操作（每个面板都有功能详情引导卡）
- **Karma 结算对接（可选）**：对接非托管双边托管结算（USDC 锁定 → 交付验证 → 结算），争议自动仲裁

## 快速开始

### Windows（一键）

```powershell
# 方式一：双击「启动控制台.bat」（自动自检 + 启动）
# 方式二：命令行
powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Docker

```bash
docker compose up -d
```

启动后打开 **http://127.0.0.1:8000/** 即可。

## 首次初始化（1 分钟）

首次打开会看到「进入 / 初始化工作区」。需要先在本地创建你的工作区凭证：

```bash
# 1. 创建用户
curl -X POST http://127.0.0.1:8000/users -H "Content-Type: application/json" -d "{\"email\":\"me@shop.com\"}"

# 2. 领取 API Key（替换 <user_id> 为上一步返回的 id）
curl -X POST http://127.0.0.1:8000/users/<user_id>/api-keys

# 3. 创建工作区（替换 <key> 为 API Key）
curl -X POST http://127.0.0.1:8000/workspaces -H "Authorization: Bearer <key>" -H "Content-Type: application/json" -d "{\"name\":\"我的店铺\",\"region\":\"CN\",\"business_category\":\"clothing\"}"
```

然后回到页面，在「进入 / 初始化工作区」的弹出卡片里填入：

- **API Key**：第 2 步返回的 `krama_...`
- **工作区 ID**：第 3 步返回的 id
- **工作区名称**：如「我的店铺」

保存后点击「进入」，即可开始使用。

## 接入大模型（可选，但强烈建议）

> 不接入模型也能完整使用：系统内置确定性 mock 提供商，商品 / 订单 / 客服 / 审批等全部业务闭环可离线跑通。

接入后所有 Agent 才真正"动脑干活"：

1. 打开控制台 → **设置** → **模型设置**
2. 填入任意 OpenAI 兼容模型的 `base_url` / `model` / `api_key`（支持 DeepSeek、OpenAI、通义、本地 Ollama 等）
3. 每个 Agent 的独立操作页里还可以单独选择自己的模型

## Karma 结算对接（可选）

控制台与 [Karma](https://github.com/AtoB101/Karma)（非托管双边托管结算层）预留了完整对接位：

- 环境变量：`KRAMA_API_BASE_URL` / `KRAMA_API_KEY` / `KRAMA_AGENT_ID` / `KRAMA_IDENTITY_ID`
- 不填则使用确定性 stub，不伪造"连接成功"；填齐后以对端真实回执为准

## 备份与恢复

- 工作区 owner 可在 **系统设置 → 备份恢复** 一键备份（一致性快照 + SHA-256 校验和）
- 恢复必须先显式确认，且恢复前自动生成 pre-restore 安全快照，校验失败不会用坏文件覆盖现库
- 默认目录 `./backups`，保留策略可配置

## 安全设计（不可绕过）

- **统一执行网关**：所有工具调用唯一入口，模型 / 模块无法绕过
- **风险分级 R0–R4**：R3+ 操作必须显式审批
- **审计日志**：每次执行记录 decision + 证据，只追加
- **幂等**：资金 / 订单类操作带幂等键，重复请求不会重复扣款
- **密钥加密落库**：Fernet 加密存储，读取脱敏
- **跨工作区数据隔离**：owner / member / viewer 权限矩阵

## 目录结构

```
app/                 FastAPI 应用（models / gateway / tools / chat / karma / connectors / backup ...）
app/static/home.html 单文件全息控制台前端
scripts/             启动脚本、自检、回归
tests/               自动化测试
docs/                架构、威胁模型、部署、运维文档
.env.example         环境变量模板（含全部外部联调凭证位，留空=离线演练）
```

## 开发与测试

```bash
# 全量回归（商业级门槛：业务闭环 + 安全 + 并发 + 性能）
python scripts/regression.py

# pytest 单测
python -m pytest -q
```

## License

Apache License 2.0（待最终确认，见 LICENSE）。

## 免责声明

- 本项目为通用商业 Agent 平台，涉及真实资金、跨境结算、法务规则等请自行完成合规审查
- 本地 SQLite 适合单机单店；多用户高并发请按 `docs/DEPLOY.md` 切换到 PostgreSQL
- 所有外部渠道（电商 / 物流 / 支付）均为凭证位预留，真实状态以对端回执为准，系统绝不伪造"连接成功"
