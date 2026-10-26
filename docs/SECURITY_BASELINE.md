# 安全基线

## 不变量

1. 所有工具调用必须经过统一执行网关 `app/gateway.py`，无其他执行路径。
2. 无有效权限的操作不能通过后端接口执行：角色权限 + 地区政策 + 风险审批三重检查。
3. R3+ 操作必须存在 APPROVED 的审批记录，且幂等键一致才能执行。
4. 每次执行都写审计日志（decision、reason、evidence），不可删除。
5. 支付/下单必须有 provider 回执并回查核验；未确认进入 UNKNOWN，不得标记完成。Karma 回查以 `/v1/settlement/{task_id}` 终态为准（settled→SUCCEEDED、refunded→REFUNDED）。

## 验证方式

- `tests/test_gateway.py`：越权/未知工具/审批/幂等。
- `tests/test_auth_tenant.py`：跨租户访问 403。
- `tests/test_task_loop.py`：审批中断与恢复、回执核验。
- `tests/test_connectors.py`：连接器回执与缺失连接器错误。

## 已知限制（MVP 阶段，后续补齐）

- 密码/API Key 无过期与轮换机制（当前仅 active 标志）。
- 模块执行隔离尚未实现（Phase 4）。
- 地区政策规则为占位值，未经过法务复核。
- Prompt Injection 防护依赖结构化意图与权限边界，未做专门对抗测试。
- Karma 客户端已对齐官方 API_REFERENCE，但链上托管锁定与 SDK 签名回执需生产凭证联调后才能用于真实资金。
