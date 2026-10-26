# KARMA 超级控制台 · 数据安全商业级审计报告

- 审计日期：2026-10-22
- 审计方式：静态代码对照公认安全基线（最小权限、零信任、静态/传输加密、密钥管理、注入防护、不可篡改审计、备份恢复、数据分级保留、限流、泄露防护）。未做外部渗透测试，未伪造任何外部渠道联调。
- 结论口径：**已具备**＝代码中存在且有回归覆盖；**差距**＝缺失或需部署/补测；不夸大、不把"设计位"说成"已落地"。

## 1. 安全基线对照（现状）

| 维度 | 现状 | 证据（模块） | 评级 |
|---|---|---|---|
| 身份认证 | 高熵随机 API Key（`secrets.token_urlsafe(24)`），库中只存 SHA-256 摘要，不落明文；比对用 `hmac.compare_digest` | `app/security.py` | ✅ 已具备 |
| 最小权限 | owner/member/viewer 三角色，R0 只读 / R1 可逆草稿 / R2 有限外发 / R3 难逆支付 / R4 不可逆 分级；支付/退款/争议/批量调价/改政策/正式发布强制老板拍板（FORCE_ESCALATE） | `app/policy.py`、`app/roles.py`、网关层 | ✅ 已具备 |
| 多租户隔离 | 所有查询带 `workspace_id` 物理过滤，跨工作区读/写/审批/记忆均有隔离测试 | `app/security.py`、`tests/test_tenant_isolation.py` 等 | ✅ 已具备 |
| E2EE 密文保管 | 服务端只存 ciphertext/nonce/blind_index，**不持解密密钥**；密钥由前端 WebCrypto 持有；盲索引仅支持精确查找，不暴露明文 | `app/e2ee_vault.py`、前端加密闭环 | ✅ 已具备 |
| 服务端对称加密 | Fernet 用于落库的第三方凭证（模型 key、SMTP、Karma 凭证），内存中解密、落库为密文 | `app/crypto.py`、`app/connectors.py` | ✅ 已具备 |
| 审计不可篡改 | 审计日志 prev_hash/content_hash 哈希链，提供完整性校验端点 | `app/audit.py`、`tests/test_integrity.py` | ✅ 已具备 |
| 提示注入防护 | 确定性规则硬拦截 jailbreak/越狱/DAN/JSON 工具调用注入，区分阻断与升级 | `app/guard.py`、`app/input_guard.py` | ✅ 已具备 |
| 数据分级保留 | PUBLIC/INTERNAL/SENSITIVE/CRITICAL 分级，PII 正则识别，到期自动清理 | `app/data_classification.py`、`app/retention.py` | ✅ 已具备 |
| 幂等与去重 | 同键重复提交只执行一次，执行回放幂等 | `tests/test_execution_idempotency.py` | ✅ 已具备 |
| 设备配对 | Passkey 式一次性授权配对，可撤销踢出 | `app/device_pairing.py` | ✅ 已具备 |
| 备份恢复 | 备份/恢复闭环 + 恢复演练 | `app/backup.py`、`tests/test_backup.py`、`tests/test_recovery_drill.py` | ✅ 已具备 |
| 速率限制 | 限流层存在 | `app/ratelimit.py` | ✅ 已具备 |
| 泄露防护 | `.secret.key`、`*.db`、`.env`、`logs/`、`backups/`、`data/` 均已在 `.gitignore` | `.gitignore` | ✅ 本轮补齐 |

## 2. 差距与整改项（分级）

### P0（上线前必须）
| # | 差距 | 整改建议 | 状态 |
|---|---|---|---|
| P0-1 | 传输层未强制 HTTPS/TLS；当前本机 `http://127.0.0.1:8000` 仅适用内网/本机，对外必须由反向代理（Nginx/Caddy）终止 TLS，禁用明文 | 部署项：Caddy/Nginx + 证书，HSTS；代码侧不假设明文安全 | ⏳ 待部署 |
| P0-2 | 依赖未做锁定与漏洞扫描：`requirements.txt` 仅给下限，未 lock 文件 | 补 `requirements-lock.txt`（`pip freeze` 锁定），定期 `pip-audit` 扫 CVE | ⏳ 待办 |
| P0-3 | 服务端 Fernet 主密钥 `.secret.key` 为单机文件，无轮换/拆分；泄露即全量可解 | 引入密钥轮换接口与备份；生产改用环境变量/KMS 注入，不落盘 | ⏳ 待办 |

### P1（商业级应具备）
| # | 差距 | 整改建议 | 状态 |
|---|---|---|---|
| P1-1 | 当前只有高熵随机 API Key，无"用户口令"体系；若未来加口令登录，SHA-256 不可用于口令 | 口令改用 argon2id/bcrypt 慢哈希+盐；当前随机 Key 用 SHA-256 可接受 | ⏳ 视需求 |
| P1-2 | 无登录/鉴权失败的集中告警与异常检测（异地、高频失败） | 在限流层之上加失败计数告警，接入既有告警引擎 | ⏳ 待办 |
| P1-3 | 备份未做加密校验清单（仅备份文件）；需校验可恢复、附哈希 | 备份包加密 + 清单哈希 + 定期恢复抽测（已有演练，补清单） | ⏳ 待办 |
| P1-4 | 日志脱敏需逐条复核，确保不打印密钥/明文 PII | 补日志脱敏回归用例，扫描 `logs/` 样例 | ⏳ 待办 |

### P2（增强项）
| # | 差距 | 建议 | 状态 |
|---|---|---|---|
| P2-1 | 无零知识证明（ZK）层；当前为"服务端不持密钥"的 E2EE，不等于密码学 ZKP 证明 | 仅在需要对外证明数据真实（如对账）时引入，预留路线 | 📋 路线登记 |
| P2-2 | 无细粒度字段级访问策略可视化下发 | 现有角色+分级已覆盖自用；多客户 SaaS 化时再扩 | 📋 路线 |
| P2-3 | 外部渠道（微信客服/抖音/呼叫/物流）凭证未联调，保持 unavailable，不伪造 | 凭证到位后按最小权限逐通道接入 | ⏳ 待凭证 |

## 3. 安全红线（已固化，不可绕过）
1. AI **不自动**承诺退款/赔偿，不自动执行支付/退款/争议/批量调价/改政策/正式发布——一律进老板拍板队列。
2. 服务端不持有 E2EE 解密密钥；第三方凭证落库为密文，仅内存解密。
3. 跨工作区物理隔离，任何查询必须带 `workspace_id`。
4. 提示注入硬拦截优先于升级；未知工具/参数在规划层前置拦截。
5. 外部渠道未配置一律 `unavailable/skeleton_pending`，绝不伪造"已连通"。

## 4. 本轮已落地的整改
- 补齐 `cryptography` 依赖声明（代码已用、原未列入 requirements）。
- `.gitignore` 增补 `logs/ backups/ data/ *.log`，防止运行日志/备份/本地库误入库。
- 一键回归脚本 `scripts/regression.py` 将安全相关纯函数（注入、隔离、分级、幂等、审计链、E2EE）纳入每次必跑。

> 说明：本报告为静态审计结论；P0-1/P0-3 属部署与密钥管理工程，需在真实发布环境落地，不在本机开发环境冒充完成。
