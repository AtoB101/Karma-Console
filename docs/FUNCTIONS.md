# 用户自定义功能区（Functions）

用户不再受限于硬编码工具，可以在工作区内把「回执网址读取」「库存数据源」「Karma 收款」「模型文案」等能力作为数据自由创建、排版、启用和停用。所有自定义功能仍然只能通过统一执行网关调用，创建功能区本身不会授予任何后端权限。

## 核心概念

- `workspace_functions`：每个工作区自己的功能定义（SQLite 表）。
- `layout_areas`：用户自由排版的「功能区」widget，通过 `target_type="function"` + `target_id` 指向某个自定义功能。
- 执行链路不变：权限 → 地区政策 → 风险审批 → 幂等 → 执行 → 回执核验 → 审计。

## Function kind 与风险下限

| kind | 含义 | 风险下限 | 说明 |
|------|------|----------|------|
| `http_read` | 读取外部 URL（回执、库存、价格） | R0 | 仅允许 GET |
| `model` | 调用现有模型生成文案/总结 | R1 | 通过 `app/llm.py` 路由 |
| `http_write` | 写回外部系统（下单、同步库存） | R2 | POST/PUT/PATCH/DELETE |
| `karma` | Karma Network 收款/退款/核验 | R3 | 必须显式审批 |

用户声明的 `risk_level` 永远不低于 kind 对应的风险下限（`effective_risk`），因此用户不能把收款声明为 R0。

## 回执网址（电商独立站 / 社交网店）

用户在自己的功能区添加一条 `http_read` + `category=receipt` 的功能，`endpoint_url` 支持 `{占位符}` 插值：

```
POST /workspaces/{ws}/functions
{
  "name": "read_receipt",
  "kind": "http_read",
  "category": "receipt",
  "endpoint_url": "https://shop.example.com/orders/{order_id}/receipt",
  "verification": {
    "type": "http",
    "status_path": "receipt_status",
    "reference_path": "provider_reference",
    "success_values": ["SUCCEEDED", "paid", "completed"]
  }
}
```

Agent 通过网关调用 `read_receipt`，传入 `{"order_id": "..."}`，即可读取真实回执；`verification` 规则把回执状态抽出来，未确认时不会把任务标记为完成。

## 库存数据源

库存同样由用户自行添加为 `http_read` + `category=inventory` 的功能，指向自己的库存 JSON 接口。平台不内置任何私有数据模型要求。

## 鉴权配置

`auth` 字段支持以下形式（密钥建议用环境变量引用，避免明文入库）：

- `{"type": "none"}`
- `{"type": "bearer", "env": "MY_TOKEN"}` 或 `{"type": "bearer", "token": "..."}`
- `{"type": "api_key", "header": "X-Api-Key", "env": "MY_TOKEN"}`
- `{"type": "basic", "username": "...", "password": "..."}`

## 网络安全（SSRF 防护）

- 仅允许 `http` / `https`。
- 默认阻断私网、回环、链路本地、保留地址（`127.*`、`10.*`、`192.168.*`、`172.16-31.*`、`169.254.*`、`localhost`、`*.local` 等）。
- 单个功能可设 `config.allow_private=true` 豁免；或全局 `FUNCTION_ALLOW_PRIVATE=1`（仅限受信任部署）。

## Karma 接入

- `app/karma.py` 实现官方公开 HTTP API（`https://api.karma.xyz/v1`）客户端。
- 未配置 `KRAMA_API_KEY` 时自动退化为可核验回执的确定性 stub，不产生真实资金动作。
- 配置 `KRAMA_API_KEY` 后，`pay` 走「创建合约 → 创建回执 → 返回 provider_reference」流程；核验回查 `/receipts/{id}`。
- 预发布注意：端点依据官方参考文档实现，真实资金上线前必须对生产 Karma 环境重新联调验证。

## 地区 / 地缘法律

地区限制仍是占位规则（`region_policy_rules`），法务细则在产品完全上线前补齐。当前已把 `make_payment`（CN）、`publish_product`/`bulk_update_prices`（EU）等动作纳入「需审批」占位规则，任何地区都不能关闭支付审批硬约束。
## 模块市场（雏形）

- `POST /workspaces/{ws}/modules`：把功能打包成模块（可用 `source_function_id` 从已有功能复制）。
- `PATCH .../modules/{id}` 设 `visibility=published`：仅 owner 可发布；发布时自动脱敏（移除 `auth.token`、`auth.password`、`authorization`/`x-api-key` 等敏感头）。
- `GET /marketplace`：浏览已发布模块。
- `POST /marketplace/{id}/install`：跨工作区安装为 `WorkspaceFunction`；`min_role`（viewer/member/owner）控制可安装/运行的最低角色，安装方用 `auth`/`endpoint_url` 覆盖提供自己的凭据。

模块安装得到的仍然是受网关约束的普通功能，不会因为“已发布”就获得额外权限。完整端点与测试见 `tests/test_modules.py`。

## Web 控制台

运行 `python -m uvicorn app.main:app --reload` 后打开 `http://127.0.0.1:8000/`：一键建环境 → 左侧组件库拖入画布 → 画布内拖拽排序 / 启停 / 删除 → 右侧 Agent 自然语言目标或直接调用 + 待审批处理。页面为无构建步骤的单文件 `app/static/console.html`。
## 对话式操作与子功能层级

- `POST /workspaces/{ws}/chat`：GPT 式对话入口，`app/chat.py` 做确定性意图解析并编排网关动作；所有写操作仍经统一网关（权限→审批→核验→审计）。
- `layout_areas.parent_id`：功能区支持父子层级（子功能）。
- `products.layout_area_id`：商品可直接归类到某个功能区。
- 内置工具 `add_layout_area` / `list_layout_areas` / `link_products_to_area` 让模型/对话通过网关操作功能区。

示例：「在测试功能区给我加上一个 产品展示区 棉麻衬衫 99元、牛仔裤 159元」会创建父子层级并把 2 件商品归类进「产品展示区」，回复中带执行结果。
## 工作区设置（模型 / 聊天工具接入）

- `GET/PUT /workspaces/{ws}/settings`：保存工作区级 `model` 与 `chat` 配置；读取时密钥自动脱敏。
- `model`：保存 base_url / api_key / model 后，`app/llm.generate` 优先使用工作区模型（营销文案、聊天协调均走该模型），未配置则回退 mock / 环境变量模型。
- `chat`：保存聊天工具 provider + token 后，`POST /chat/inbound`（Body：`{"token","message"}`）即可让 Telegram/微信/Discord 等通过 webhook 直接把指令发给 Agent，复用同一套意图解析 + 网关安全链路。
- 安全边界：模型只负责协调与文案，不直接执行；执行仍由确定性解析器 + 统一网关完成。
