# 生产部署指南（DEPLOY）

> 目标：把已验证的 Krama 单体从本地 SQLite 开发态，部署到可复现、可回滚的生产环境。
> 基线：`app` 为 FastAPI 模块化单体；数据库通过 `DATABASE_URL` 环境变量切换（SQLite / PostgreSQL）；密钥经 `KRAMA_SECRET_KEY` 提供 Fernet 加密。

## 1. 快速开始（Docker Compose，PostgreSQL）

```powershell
# 在仓库根目录（app 所在目录）
Copy-Item .env.example .env   # 可选：按需填写模型/邮件/Karma 参数

# 生成生产密钥并写入 .env（必须，且要长期保管、定期轮换）
python -c "import secrets; print('KRAMA_SECRET_KEY=' + secrets.token_urlsafe(48))" | Out-File -FilePath .env -Encoding utf8

docker compose up -d --build
docker compose ps          # app 与 db 都应为 healthy/running
```

- 服务端口：`http://127.0.0.1:8000`（`docker-compose.yml` 映射 `8000:8000`）。
- 健康检查：`GET /health` 返回 `{"status":"ok"}`；`GET /health/metrics` 返回进程内指标。
- 数据卷：`pgdata`（PostgreSQL）、`appdata`（`/app/data`，上传文件/报告）、`backups`（`/app/backups`，SQLite 备份目录）。

## 2. 环境变量要点

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | 生产用 `postgresql+psycopg://user:pass@host:5432/db`；本地默认 `sqlite:///./krama.db` |
| `KRAMA_SECRET_KEY` | 静止加密主密钥（Fernet）。生产必须设置，丢失将无法解密已存密钥/令牌 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 默认 OpenAI 兼容模型路由（可在页面“设置”按工作区覆盖） |
| `DEFAULT_MODEL_PROVIDER` | `mock`（确定性离线）或真实 provider；接入模型后设为对应值并在设置页保存 key |
| `KRAMA_*` | Karma 支付凭据；留空使用确定性 stub（生产联调前必须官方联调放行） |
| `SMTP_*` / `OWNER_EMAIL` | 待办/审批超时邮件升级与确认码发送 |
| `BACKUP_ENABLED` / `BACKUP_DIR` / `BACKUP_RETENTION` | SQLite 在线快照与保留策略（Postgres 请改用 pg_dump 或托管备份） |
| `ENABLE_BACKGROUND_SCHEDULER` | 后台对账/断点续跑/邮件升级循环开关 |

## 3. 接入 DeepSeek（页面操作，不写代码）

1. 打开 `http://127.0.0.1:8000` → 「设置」。
2. 模型区填：provider `openai_compat`、`base_url https://api.deepseek.com`、`model deepseek-chat`、`api_key` 填 key。
3. 保存后 Agent 即用该模型做推理/工具编排；`/chat` 的执行链路 trace 可在界面查看。

## 4. Webhook 接入与签名校验

- 聊天工具 webhook 统一入口：`POST /webhooks/{provider}[/{token}]`，provider 支持 `telegram` / `wechat` / `discord` / `generic`。
- 在「设置 → 聊天」里配置 `webhook_secret` 后，服务端强制校验：
  - Telegram：请求头 `X-Telegram-Bot-Api-Secret-Token` 必须等于 `webhook_secret`。
  - 其他 provider：请求头 `X-Krama-Signature: sha256=<HMAC-SHA256(raw_body, webhook_secret)>`。
- 未配置 `webhook_secret` 时保持原有 token 校验（向后兼容）；配置后签名错误返回 `{"ok": false, "error": "invalid webhook signature"}`。

## 5. 上线前硬门槛（外部依赖）

- Karma 生产环境鉴权/结算/仲裁/真实资金语义：代码已按官方 `API_REFERENCE` 对齐，真实扣款必须官方联调后单独放行。
- 微信/Telegram/Discord 平台侧开放审核与生产联调。
- 地区地缘法律矩阵：当前为 CN/EU 占位规则，需法务团队补齐后替换 `app/seed.py::DEFAULT_RULES`。
- `run_code` 为受限纯 Python 子进程沙箱（`-I` + 白名单导入 + 超时 + 输出上限 + 瞬态 spawn 重试）；如需运行不可信第三方代码，生产应再套容器/VM 级隔离。
- TLS：生产应在 `app` 前加反向代理（Nginx/Caddy）终止 HTTPS。

## 6. 回滚与恢复

- 设置版本化：`GET /workspaces/{id}/settings/history` + `POST /workspaces/{id}/settings/rollback/{version}`。
- 备份恢复：`POST /backups`（快照）、`POST /backups/{id}/restore`（`confirm: true`，owner 门禁，恢复前自动做安全快照 + `PRAGMA integrity_check`）。
