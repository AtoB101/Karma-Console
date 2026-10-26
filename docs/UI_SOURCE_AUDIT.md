# AI_Super_Console 源码包复用评估（UI_SOURCE_AUDIT）

> 评估对象：项目根 `AI_Super_Console/`（用户提供的"完整 UI 源码包"）
> 评估日期：2026-10-21
> 结论先行：**不能整体直接替换或直接运行；但视觉资产（背景、皮肤版式、电流动效、信息架构）可移植到现有 FastAPI 控制台。**

## 1. 这套源码是什么

- 技术栈：Next.js 16.3 + React 19 + TypeScript + Tailwind 4 + shadcn/radix + Drizzle ORM + Cloudflare Workers / D1。
- 运行要求：Node.js ≥22.13.0、pnpm 11.25、联网 `pnpm install --frozen-lockfile`、`pnpm build`、Wrangler 本地 D1。
- 前端：`app/console.tsx`（1536×1024 固定舞台 + scale 自适应）、`app/energy.tsx`（Canvas 电流/轨道动效）、`app/globals.css`（整套皮肤与绝对定位版式）。
- 后端：`app/api/console/route.ts`（`cloudflare:workers`、`env.DB` D1、`oai-authenticated-user-id` 宿主鉴权头、SSE ndjson 执行进度），`db/`、`drizzle/` 为 D1 schema/迁移。
- 业务：任务/客户/商品库存/商家/规则经理人/计划/账务记账/文案草稿/浮动面板/文本资料 CRUD，确定性规则工作流（经营汇总、库存预警、CRM 阶段、营销模板），SHA-256 收据与审计。明确**未接大模型、真实支付、订票、外部消息**。

## 2. 为什么不能"整体直接复用"

| 维度 | 源码包 | 现有 krama 系统 | 是否兼容 |
|---|---|---|---|
| 运行时 | Cloudflare Workers / Next RSC | FastAPI (Python) + SQLite | 否 |
| 数据库 | D1（SQLite 方言，经 env.DB） | 自有 SQLite + SQLAlchemy | 否 |
| 鉴权 | 宿主注入 `oai-authenticated-user-id` | Bearer api_key + X-Workspace-Id | 否 |
| 执行/进度 | 自有 SSE `/api/console` | 现有网关/审批/回执链路 | 否 |
| 本机环境 | 需 Node ≥22.13 | 本机 Node 11.17.0，版本不足，pnpm 未就绪 | 否（需升级 Node + 联网装包） |

- 整体替换会**丢弃现有已闭环资产**：权限/审批、执行幂等回放、E2EE 密文保管、MCP/HTTP 工具目录、工作流条件/触发、记忆分层、审计哈希链、就绪探针等（P1–P77）。
- 直接 `pnpm install/build` 在本机 Node 11 下不可行，且与现有服务端口/数据不通。

## 3. 可直接移植的资产（推荐路线：视觉移植，不换栈）

1. **背景图 `public/scene.png`**：去 UI 的 KARMA 全息地球舱纯背景，可直接复制到 `app/static/` 作全屏底图。
2. **设计稿 `public/reference.png`**：即用户最初给的成品参考图，可作像素对照基准。
3. **`app/globals.css` 皮肤与版式**：1536×1024 绝对定位坐标、青蓝全息配色、玻璃拟态、3D 倾斜 Hero 卡、发光描边、composer 语音条样式——可剥离为原生 CSS 套到现有 `console.html`。
4. **`app/energy.tsx` 电流动效**：纯 Canvas 2D（三圈轨道 + 贝塞尔数据流 + 待命低速/执行高速），可改写成原生 JS，挂到现有"保存/执行请求"状态。
5. **信息架构与字段（`app/catalog.ts`）**：11 项导航、6 张 Hero 卡、实时任务、系统状态、AI 主脑、快捷入口、今日计划；客户阶段、商品 SKU/库存/预警线等字段可与现有后端实体对齐。
6. **交互范式**：Ctrl+K 全局搜索、语音转写（浏览器 Web Speech）、浮动可拖面板、乐观版本校验、中断任务恢复——可选择性吸收。

## 4. 明确不搬

- `app/api/console/route.ts`、`db/`、`drizzle/`、`lib/`（D1/Worker 专属）；数据仍走现有 FastAPI 接口。
- `app/chatgpt-auth.ts`、`.openai/` 宿主托管配置、Next/Tailwind 工程脚手架。
- 它的"规则经理人/记账"不重复造，现有网关与业务闭环优先。

## 5. 推荐落地顺序（视觉移植，保持 FastAPI 后端不动）

1. 把 `scene.png` 接入为现有控制台全屏背景，校准青蓝配色与现有皮肤一致。
2. 移植 globals.css 的 Hero 卡倾斜发光、玻璃面板、composer、实时任务/系统状态/AI主脑区块样式到现有页面。
3. 移植 energy.tsx 电流轨道为原生 Canvas，绑定现有执行/保存状态。
4. 各卡片数字改接现有 FastAPI 真实接口（商品/库存/订单/客户/待办/审批/健康探针），不使用示例数。
5. 每步静态校验 + 全量 pytest RC=0，同步三份文档。
