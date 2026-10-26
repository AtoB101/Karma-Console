# 第三方平台真实回执接入清单

> 目的：把「Karma / 淘宝 / 抖店 / 顺丰」四类真实回执接入路径核对清楚，明确每一步的官方端点、鉴权、关键方法、凭证、以及当前 Krama 面板还缺什么。
> 原则：只标「已核实」表示检索到官方文档可佐证；标「预核实」表示还需向官方确认后才能上线。
> 生成日期：2026-10-18。代码基线：`app/connectors.py`、`app/config.py`、`app/functions.py`。

## 1. 结论速览

| 平台 | 状态 | 官方端点/入口 | 核心鉴权 | 能否只用 URL+Key 接入 |
| --- | --- | --- | --- | --- |
| Karma 支付 | 已对齐官方 API_REFERENCE（生产凭证联调待做） | `https://api.karma.xyz/v1`（GitHub `AtoB101/Karma` → `docs/API_REFERENCE.md`） | `POST /v1/auth/token`（`agent_id`+`api_key`）换 24h JWT，`Authorization: Bearer` | 可按 Key 接入；真实资金仍需生产 agent 身份 + SDK 签名回执 + 链上托管锁定 |
| 淘宝开放平台 | 已核实 | `https://gw.api.taobao.com/router/rest` | `app_key` + `app_secret`，MD5 签名 | 否，需按请求签名 |
| 抖店（抖音电商） | 已核实 | `op.jinritemai.com`（签名工具 `op.jinritemai.com/docs/guide-docs/gen-sign`） | `app_id` + `app_secret`，md5/hmac-sha256 签名 | 否，需按请求签名 |
| 顺丰丰桥 | 已核实 | `http://bsp-oisp.sf-express.com/bsp-oisp/sfexpressService` | `partnerID`(clientCode) + `checkword`(校验码) | 否，需按请求生成校验码 |

**一句话结论**：Karma 可按 Key 走通用 HTTP 回执；淘宝/抖店/顺丰三家的官方接口都必须「请求级签名」，现已由 `app/signing.py` + `SignedApiConnector` 补齐（连接器配置 `signer: taobao | douyin | sfexpress` + `actions.<action>.method|service_code`），签名算法已按公开文档实现并有确定性单测；上线前仍需用各平台官方签名调试工具做一次交叉验证。

## 2. 当前回执接入能力（已确认）

- 连接器层 `app/connectors.py` 已定义 8 类渠道：Karma 收款、独立站、社交网店、外卖配送、电商平台、外贸业务、物流管理、跨境电商（`ConnectorType`）。
- 电商/预留渠道支持配置 `endpoint_url` + `method`（GET/POST/PUT/PATCH）+ 鉴权：`bearer` / `api_key` / `basic` / `none`。
- 支持 `allow_private`（SSRF 白名单放行）与回执核验规则 `verification.status_path` / `reference_path` / `success_values`。
- 未配置 `endpoint_url` 时回退到确定性 stub，保证离线链路可跑通。
- 局限：**鉴权只支持静态 Header**，无法在每次请求前用 `app_secret + timestamp + 参数` 动态生成签名；因此淘宝/抖店/顺丰这类「签名式」接口无法直接用现有配置接通。

## 3. Karma 支付（已对齐官方协议）

- 状态：**代码已对齐官方 `docs/API_REFERENCE.md`**，生产资金链路待真实凭证联调。
- 官方协议（Bilateral）：`Lock USDC → mint Bill Token(1:1) → bind buyer+agent bills → verify delivery → settle/finalize → burn bills, release USDC`；结算状态机为 `draft → pending → accepted → in_progress → progress_submitted → progress_confirmed → delivered → disputed → arbitrated → settled/refunded`，终态 `settled/refunded/cancelled`。
- 鉴权：`POST /v1/auth/token`（body `{agent_id, api_key}`）返回 24h `access_token`；后续 `Authorization: Bearer <token>`。
- `app/karma.py` 已实现：`/v1/agents`、`/v1/contracts`（含 `assign`）、`/v1/receipts`、`/v1/bundles`、`/v1/verify`、`/v1/settlement/{task_id}/*`（create/lock/pending/start/submit/fail/partial/regret/dispute/auto-arbitrate）、`/v1/capacity/{identity_id}/*`、`/v1/reputation/{agent_id}`。
- 动作映射：`pay`=创建合约+指派 worker+建 draft 结算；`refund`=`/settlement/{task_id}/regret`；`reconcile`=读 settlement+verify；`dispute`=`/settlement/{task_id}/dispute`；统一用 settlement 终态做 verify-before-complete 回查。
- 尚未能由客户端完成的步骤（需生产环境）：链上托管锁定（capacity lock / bill mint）、SDK 签名的 `ExecutionReceipt` 提交、仲裁执行——这些在真实资金流动前必须联调验证，当前以 mock transport 覆盖 wire 形状。
- 当前面板填写位置：`设置 → 渠道回执`（Karma 收款），填入 API Key（加密落库）。

## 4. 淘宝开放平台（已核实）

- 端点：`https://gw.api.taobao.com/router/rest`
- 公共参数：`method`、`app_key`、`timestamp`（格式 `yyyy-MM-dd HH:mm:ss`）、`format=json`、`v=2.0`、`sign_method=md5`、`sign`。
- 签名：所有参数按 key 升序拼接 `keyvalue`，再拼 `secret` 后 MD5（32 位大写/小写按文档）。
- 凭证：`APP_KEY` + `APP_SECRET`；订单/私域类 API 额外需要 `session`（商家授权 token）。
- 关键方法（示例，需以官方类目为准）：订单查询、发货、物流、商品上/下架、评价。
- 接入方式：不能只用 URL+Key，需要 SDK 式签名适配器。

## 5. 抖店 / 抖音电商（已核实）

- 文档入口：`op.jinritemai.com`；签名工具：`https://op.jinritemai.com/docs/guide-docs/gen-sign`。
- 示例方法：`order.list`（订单列表）。
- 参数：`appId`、`timestamp`、`param_json`（业务参数 JSON 字符串）、`v=2`、`sign_method`（`md5` 或 `hmac-sha256`）、`sign`。
- 凭证：`app_id` + `app_secret`（及商家/店铺 `access_token`）。
- 关键方法：订单列表/详情、发货、售后、库存、商品管理。
- 接入方式：需要签名适配器 + `access_token` 刷新管理。

## 6. 顺丰丰桥（已核实）

- 端点：`http://bsp-oisp.sf-express.com/bsp-oisp/sfexpressService`
- 参数：`partnerID`（clientCode）、`requestID`、`serviceCode`、`timestamp`、`msgData`（业务数据）、`checkword`（校验码）。
- 校验码：来自丰桥平台的 `checkword`（月结卡号 + 校验码），按丰桥规范生成请求校验。
- 凭证：丰桥平台的月结卡号、校验码。
- 关键方法：下单、路由查询、运费查询、电子面单。
- 接入方式：需要丰桥专用校验码适配器。

## 7. 当前硬缺口：签名适配器

- 淘宝、抖店、顺丰的共同点：每次请求都要「动态签名」，而现有 `_headers_from_config` 只能写死 Header，做不到按请求签名。
- 需要补的适配能力（二选一）：
  - 方案 A（推荐）：在 `app/connectors.py` 新增 `SignedApiConnector`，按渠道预置 `taobao / douyin / sfexpress` 的签名函数，配置里只存 `app_key/app_secret` 等凭证。
  - 方案 B：在 `app/functions.py` 的自定义功能里新增 `kind=custom_signer`，用户可选签名算法模板。
- 还需补：`access_token` 刷新与过期重试（抖店/淘宝私域）、`requestID` 幂等（顺丰）、签名失败的可读错误回传。
- 安全：`app_secret`/`checkword` 必须与 Karma Key 一样走 Fernet 加密落库（`app/crypto.py`），回执核验沿用 `verification.*` 规则。

## 8. 推荐实施顺序

1. Karma：先按 Key 跑通回执，同时向官方确认 Base URL 与字段。
2. 淘宝：实现 MD5 签名适配器 + 订单查询/发货两个方法做最小闭环。
3. 抖店：实现 md5/hmac-sha256 签名适配器 + `order.list` / 发货。
4. 顺丰：实现丰桥校验码适配器 + 下单/路由查询。
5. 收尾：四家统一接入 `设置 → 渠道回执` 面板，凭证加密存储，回执经 `verify_connector_receipt` 核验后才标记完成。

## 9. 与功能区的关系

- 以上真实回执属于「执行链路」的底层连接器，不占用户功能区；用户在 `设置 → 渠道回执` 填凭证后，Agent 自动读取该渠道的回执权限与库存/订单/物流数据。
- 用户自建的回执网址（独立站/社交网店）仍走现有「功能区 → 添加回执网址」路径，两者并行不冲突。
