# KARMA 超级控制台 · 商业级验收总览

- 验收日期：2026-10-22
- 运行环境：本机 FastAPI + SQLite，前端单文件 `app/static/home.html`，入口 `http://127.0.0.1:8000`
- 验收方式：一键回归 `python scripts/regression.py`（AST 全量编译 + 核心纯函数断言 + 前端 JS 语法 + pytest 全量），本轮结果 **11 项断言全 PASS，pytest 退出码 0，无 FAIL/WARN**。

## 一、本轮（商业级验收轮）落地内容

### 1. 可执行 DAG 工作流内核（类 Coze）
- 拓扑排序/环检测/白名单（只读安全动作、危险动作强制拍板、控制节点）。
- 条件分支 `branch`（> >= < <= == != contains/empty/nonempty）、按走向裁剪未选分支、上游参数绑定 `inputs`、并行分层 `layerize`。
- 图落库 CRUD + 运行端点，前端可拖拽编排、分支/参数可视化配置、运行回执展示路由/跳过/并行层。
- 说明：并行采用"同层无依赖=可并行"的确定性分层；物理多线程因 SQLite 单连接非线程安全未做，属已知取舍。

### 2. 商业级一键回归 `scripts/regression.py`
- 阶段1 AST 编译全部 `app/**/*.py`；阶段2 核心纯函数断言（DAG、线索评分、客服路由/FAQ、TTL 缓存、预案库）；阶段3 前端内联脚本 `node --check`；阶段4 pytest 全量，以退出码判定。
- 退出码 0=无 FAIL，WARN 不阻断；可 `--json` 供 CI。

### 3. 数据安全审计 `docs/SECURITY_AUDIT.md`
- 逐项对照最小权限/隔离/E2EE/审计链/注入防护/分级保留/备份/限流基线，区分"已具备"与"差距"。
- 即时整改：补 `cryptography` 依赖声明；`.gitignore` 增补 `logs/ backups/ data/ *.log`。

### 4. 重复工作高速化 `app/cache.py`
- 线程安全短 TTL 只读缓存（get/set/invalidate/clear，key 必带 workspace_id，不缓存凭证/PII）。
- 消除 `cs/board` 对 Message 的重复全表扫描（一次查询同时构建客户消息与会话消息）。
- 高频过滤列（workspace_id/status/customer_id/order_id 等）已有索引。

### 5. 动脑决策快速方案与预案库 `app/playbook.py`
- 10 条预案：退款争议、投诉差评、低库存、热线索、模型不可用、渠道断连、安全告警、备份失败、SLA 超时、批量调价/改政策。
- 每条含：触发信号/关键词、优先级 P0-P2、首选快速方案、B 计划、成本代价、最坏结果、拍板人（auto/ceo/boss）、是否强制升级。
- 只读端点 `GET /workspaces/{wid}/playbooks?q=`，已接入神经调度网面板展示。

## 二、全系统能力地图（已落地）

| 域 | 已具备能力 |
|---|---|
| 身份与权限 | 高熵随机 API Key（库中只存 SHA-256 摘要，compare_digest）、owner/member/viewer、R0-R4 分级、支付/退款/争议/批量调价/改政策/正式发布强制老板拍板 |
| 多租户 | 全部查询带 workspace_id 物理隔离，跨租户读/写/审批/记忆均有隔离测试 |
| 隐私加密 | E2EE 服务端只存密文/nonce/盲索引、不持密钥；第三方凭证 Fernet 落库、内存解密；设备配对可撤销 |
| 安全防护 | 提示注入确定性硬拦截、入站空/超长拦截、幂等去重、审计哈希链完整性校验、数据分级保留自动清理、限流 |
| 业务闭环 | 自营商品/库存/订单/客户全闭环；订单发货/取消、客户阶段推进、补货、批量调价（审批通道） |
| 获客×客服 | 线索规则评分分级、SOP 跟进节奏、FAQ 命中、转人工认领、工单时间线+哈希链+SLA 盯防、渠道凭证位（未配置一律 unavailable，不伪造） |
| 神经调度 | 触角注册表、CEO 分派纯函数、自主/政策内/强制升级三态、任务落账状态机泳道、预案可视化 |
| 受控开发 | 系统工程 Agent 只做安全扫描/拆单/列测试与回滚/登记回归/审批门禁，绝不自动写盘/装依赖/重启/联网；批准/拒绝/落地全部进哈希链审计，落地必须老板拍板后人工受控执行；回归门槛汇总（pass/fail/pending，fail 阻断、gate_ok）与待批滞留检测（proposed 超 24h 标 stale，只读、不自动催办）均有只读端点与离线回归断言 |
| 内容生产三 Agent | 文案 Agent（产品文案骨架+合规词提醒）、视频 Agent（分镜脚本/参数单，成片生成恒为 credential_required 不伪造）、社交运营 Agent（发布队列闸门/评论箱/回复草稿老板确认/热门视频确定性提炼）；统一 draft→review→approved→ready_to_publish→published 状态机，CEO 批准前仅草稿、发布须老板+渠道凭证；多模型注册表（密钥 Fernet 加密、前端只 mask），每个 Agent 独立操作页按 TEXT/VIDEO 能力选模型并可就地登记；CEO 统一审批台聚合跨文案/视频待拍板项，可写批准/驳回备注并回显，首页待拍板计数并入待审批卡与主脑状态/红点；草稿生成后本页结构化回显候选标题/卖点/三版本文案/分镜表/参数单/合规与模型状态 |
| 统一渠道账号接入 | 只收录官方合规渠道的官方 API：网站在线客服、Telegram Bot、WhatsApp Business Cloud、Instagram Messaging、Facebook Messenger、LINE、Slack、Discord、邮箱、企业微信客服、抖音/快手开放平台私信、SMS；个人微信/个人 WhatsApp 非官方桥接一律不收录（防封号）；凭证 Fernet 加密入库、前端只显示是否已配置不回显明文；账号接入/启停/删除/确定性重连探测（不联网、不伪造 connected）；前端"渠道接入·统一收件箱"面板含目录风险说明、接入表单、状态徽标；接入后该渠道客户消息才进统一收件箱由 AI 客服应答、必要时转人工；真实收发需对应平台凭证与 Webhook（凭证位，不伪造） |
| 运维 | 备份恢复+恢复演练、就绪探针、本机一键自检、启动脚本/日志/目录规范、经营日报周报/快照、只读运行日志（脱敏） |
| 集成 | MCP 兼容 JSON-RPC（tools/list+tools/call 走网关）、HTTP 工具目录、联调登记表与凭证位 |

## 三、待部署 / 待真实凭证（不在本机冒充完成）

1. **P0 对外传输加密**：当前仅本机 http，对外需反向代理终止 TLS + HSTS。
2. **P0 依赖锁定与漏洞扫描**：补 lock 文件、定期 `pip-audit`。
3. **P0 密钥管理**：Fernet 主密钥轮换/KMS 注入，不落盘。
4. **外部渠道**：微信客服/抖音私信/呼叫中心/物流/真实资金通道，需真实凭证后填对端 HTTP，凭证到位前保持 unavailable。
5. **内容三 Agent 外部能力**：真实文生/图生视频 API、社交平台发布与评论抓取、所选大模型联网调用，均需凭证；到位前视频恒为 credential_required、发布闸门无凭证拒绝、评论可手动录入，绝不伪造成片/发布成功/平台数据。
6. 若未来加用户口令登录，需换 argon2id/bcrypt 慢哈希（当前高熵随机 Key 用 SHA-256 可接受）。

## 四、如何复跑验收

```powershell
cd "C:\Users\Administrator\Desktop\krama agent"
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" scripts/regression.py
```

## 五、诚实边界
- 本轮未做外部渗透测试、未做真实渠道/资金联调；环境无法起 uvicorn 做 HTTP 冒烟，验收以 AST+纯函数断言+前端语法+pytest 退出码为准。
- 所有"未连通"状态均如实标记，未伪造成功回执。
