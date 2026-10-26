# 本机运维手册（OPERATIONS_LOCAL）

> 面向 Windows 本机常驻运行：自检、启动/停止、目录与日志规范、备份恢复、外部联调凭证位。
> 生产 Docker/Postgres 部署见 `docs/DEPLOY.md`；本手册只管本机稳定常驻。

## 1. 目录规范

| 路径 | 用途 | 处置 |
| --- | --- | --- |
| `app/` | 后端源码（FastAPI 模块化单体） | 只读运行，勿手改数据 |
| `tests/` | pytest 全量回归 | 每批改动后跑全量，RC=0 |
| `scripts/selfcheck.py` | 本机一键自检 | 启动前必过 |
| `scripts/start_server.ps1` | 启动辅助（自检→建目录→前台运行+日志落盘） | 由 bat 调用 |
| `logs/` | 运行日志，按启动次数切分 `server_时间戳.log` | 自动创建，可定期清理旧文件 |
| `backups/` | SQLite 一致快照（手动/自动备份产物） | 由 `BACKUP_DIR` 指定，需可写 |
| `data/` | 本地上传/导出等可变数据目录 | 自动创建 |
| `krama.db` | 默认 SQLite 库文件 | 勿直接编辑，走备份/恢复 |
| `.secret.key` | Fernet 静止加密主密钥（自动生成） | 长期保管，丢失则已存密文不可解 |

> 根目录历史遗留的 `_server*.log`、`_patch*`、`p_*.txt`、`e2e_*.py`、`verify_*.py` 为早期临时产物，不属于运行必需，勿删业务文件，也不要把新日志写进根目录。

## 2. 一键自检

```powershell
# 人读输出；任一 FAIL 退出码为 1
python scripts/selfcheck.py

# 机器可读（供开机项/脚本判断）
python scripts/selfcheck.py --json
```

检查项：Python≥3.10、核心依赖可导入、数据库 `SELECT 1`、备份/日志/数据目录可写、加密密钥就位。外部渠道凭证缺失只记 WARN，不阻断启动，也绝不伪造连通。

## 3. 启动 / 停止

- 启动：双击 `启动控制台.bat`（等价于 `powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1`）。
- 流程：先自检 → 建目录 → 前台运行 uvicorn，日志同时落盘 `logs/`。
- 停止：在服务窗口按 `Ctrl+C`，或关闭窗口。
- 访问：`http://127.0.0.1:8000/`。

端口被占用时：先在任务管理器/`netstat -ano | findstr :8000` 找到旧 python/uvicorn 进程结束，再启动；不要重复起多个实例。

## 4. 日志规范

- 每次启动生成独立 `logs/server_YYYYMMDD_HHMMSS.log`，stdout/stderr 合并写入。
- 不做自动轮转（本机规模小）；定期归档或删除 30 天前的旧日志即可。
- 排查顺序：先看最新 `server_*.log` 尾部 → `/health/metrics` → 审计链。

## 5. 备份与恢复

- 手动：安全中心/设置页触发备份（R1，落审计）；恢复为 R4，必须显式确认并先自动做安全快照。
- 自动：设 `BACKUP_ENABLED=1`，按 `BACKUP_INTERVAL_HOURS` 检查距最新健康快照是否超期，`BACKUP_RETENTION` 控制保留份数。
- 备份目录必须可写；不可写时备份会明确报错而不是静默丢快照。
- 恢复前校验 SHA-256 校验和，恢复后跑 `PRAGMA integrity_check`；失败保留安全快照并中止。

## 6. 外部联调凭证位清单（不伪造、留真实凭证位）

登记层见 `app/integrations.py`，只读状态端点 `GET /workspaces/{id}/integrations/status`。
缺必填凭证 → `status=unavailable`；填齐 → `ready_to_probe`（仅代表可发起真实连通测试，不等于已连通，真实结果以对端回执为准）。

| 适配项 | 必填凭证位（环境变量） | 待实现真实方法 |
| --- | --- | --- |
| Karma 资金记账/结算 | `KRAMA_API_KEY`、`KRAMA_AGENT_ID`、`KRAMA_IDENTITY_ID` | 建合约/锁托管/交付验证/结算/退款/争议/对账 |
| 独立站商城 | `ECOM_INDEP_ENDPOINT`（可选 `ECOM_INDEP_API_KEY`） | 建单/取消/库存同步/发货回传 |
| 社交网店 | `ECOM_SOCIAL_ENDPOINT`（可选 `ECOM_SOCIAL_API_KEY`） | 建单/取消/发货/库存同步 |
| 淘宝 TOP | `TAOBAO_APP_KEY`、`TAOBAO_APP_SECRET`、`TAOBAO_ACCESS_TOKEN` | 拉单/发货回传/退款查询 |
| 抖音电商 | `DOUYIN_APP_ID`、`DOUYIN_APP_SECRET`、`DOUYIN_ACCESS_TOKEN` | 拉单/发货/售后查询 |
| 顺丰物流 | `SF_PARTNER_ID`、`SF_CHECKWORD`、`SF_SERVICE_CODE`、`SF_ENDPOINT` | 下单取号/轨迹追踪/签收回传 |
| 通用物流 | `LOGISTICS_ENDPOINT`（可选 `LOGISTICS_API_KEY`） | 轨迹查询/运单回写 |

> 真实 HTTP 调用复用 `app/connectors.py`（通用 HTTP/签名连接器）与 `app/karma.py`（KarmaClient）；凭证经 Fernet 加密落库、读取脱敏，不下发前端。

## 7. 日常巡检端点

| 端点 | 用途 |
| --- | --- |
| `GET /health` | 存活探针，恒返回 `ok` |
| `GET /health/ready` | 就绪探针，DB 不通返回 503 |
| `GET /health/metrics` | 进程内可观测指标快照 |
| `GET /workspaces/{id}/integrations/status` | 各联调适配项配置态/缺项清单（只读） |

## 8. 故障排查

| 现象 | 先查 | 处置 |
| --- | --- | --- |
| 启动即退出 | `selfcheck.py` 输出 | 按 FAIL 项补依赖/修目录权限，勿带病起服务 |
| 页面打不开 | `/health/ready` 是否 503、8000 是否被占 | 结束旧进程后重启；DB 损坏走备份恢复 |
| 渠道显示不可用 | `/integrations/status` 的 missing 列表 | 补必填凭证位后再做真实连通测试 |
| 邮件/审批码未到 | SMTP_* / OWNER_EMAIL | 核对配置，确认码只发主人邮箱，Agent 不代确认 |
| 密钥相关解密失败 | `.secret.key` / `KRAMA_SECRET_KEY` 是否被换 | 用备份密钥恢复，禁止凭猜测重置 |
