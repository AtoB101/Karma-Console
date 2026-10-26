# 分阶段补齐路线图（PHASED ROADMAP）

> 目标：把 `docs/CURRENT_STATE.md` 与 `docs/GAP_ANALYSIS.md` 中可由本地代码扎实实现的缺口，
> 分阶段逐个落地；每一阶段都包含「代码实现 → 配套自动化测试 → 全量 pytest 回归 → 文档同步」，
> 全量回归 EXIT=0 才算完成。依赖外部条件的项只登记接口与待办，不伪造联调完成。

## 阶段总览

| 阶段 | 主题 | 状态 | 交付门槛 |
|---|---|---|---|
| P1 | 工作流可靠性：瞬态重试/指数退避 + 补偿幂等去重 + 对账失败退避 | 已完成 | 重试分类正确、退避可配置、补偿不重复执行、对账连续失败降频；新增 9 用例全绿，全量 242 passed / 2 skipped |
| P2 | Prompt Injection 防护硬化（guard.py） | 已完成 | BLOCK/WARN 分级、模型输出侧扫描、正常文本不误伤；新增 13 用例全绿，全量 255 passed / 2 skipped |
| P3 | 模块审核与发布审批（供应链） | 已完成 | draft→pending_review→approved/rejected→published、内容指纹防篡改、owner 审批、非 published 不可安装；新增 6 用例全绿，全量 261 passed / 2 skipped |
| P4 | 模块配置隔离边界（轻量沙箱） | 已完成 | kind 白名单、input_schema 强校验、SSRF 复检、禁任意执行代码字段，create/PATCH/install 三处校验；新增 8 用例全绿，全量 269 passed / 2 skipped |
| P5 | 可观测性：请求 ID 贯穿 + 告警规则引擎 | 已完成 | X-Request-ID 贯穿日志/审计，阈值告警（错误率/5xx/突发/待审批）经 notify；新增 11 用例全绿，全量 280 passed / 2 skipped |
| P6 | 数据分级保留策略与自动清理 | 已完成 | 易失流水按天清理、审计/记忆/业务表永久保护、后台 opt-in + owner 手动端点；新增 6 用例全绿，全量 286 passed / 2 skipped |
| P7 | 外部依赖项登记 | 已完成 | 集中登记于 `docs/EXTERNAL_DEPENDENCIES.md`，仅文档/接口，不伪造联调完成 |
| P8 | 执行侧幂等回放 + 聊天 token 索引硬化 | 已完成 | Execution 记 workspace_id，同 key SUCCEEDED 回放不重复副作用（防重复支付/退款）；`chat_token_index` 唯一索引 O(1) 解析、回 settings 复核；新增 8 用例全绿，全量 294 passed / 2 skipped |
| P9 | 全息丝滑转场 + 面板联动钻取 + 鲜活刷新 | 已完成 | 面板扫描加载态/淡入转场、总览指标钻取联动、聚焦面板 15s 静默刷新、事件计数去重修复；纯前端，node --check 通过、浏览器实测零 JS 错误零失败请求，全量 294 passed / 2 skipped |
| P10 | 全息业务面板：商品库存/订单/客户 | 已完成 | 新增商品库存（低库存红警）、订单中心（支付/履约色带）、客户档案面板，接现有受控 GET API，总览钻取直达；node --check + 浏览器实测钻取成功零错误，全量 294 passed / 2 skipped |
| P11 | 经理人独立执行闭环 | 已完成 | `app/agent_runtime.py` 只读巡检运行器 + `POST /agents/{aid}/run`；live/test 才可跑、工具须在白名单且只读、限额约束、经网关执行、写持久记忆+审计；新增 5 用例，全量 299 passed / 2 skipped |
| P12 | 全息经理人面板 | 已完成 | holo 新增 AI 经理人面板（状态/白名单展示 + 手动只读巡检按钮，回显执行/成功/拦截计数）；node --check + 浏览器实测本地匹配打开零错误，全量 EXIT=0 |
| P13 | 经理人定时自动巡检挂接调度器 | 已完成 | `_patrol_live_agents` 按 patrol_minutes（默认360）周期自动只读巡检、时间戳存记忆、周期内去重，test/draft 不自动跑；新增 2 用例，全量 301 passed / 2 skipped |
| P14 | 主脑相关记忆召回（轻量检索增强） | 已完成 | `app/context_assembler.py` 确定性字面相关度 top-K 召回，主脑置顶相关持久记忆，避免全量倾倒稀释、降幻觉；新增 4 用例，全量 305 passed / 2 skipped |
| P15 | 计划工具名静态自检 | 已完成 | `lint_known_tools` 对照 registry+已启用功能名拦截臆造工具，执行前打回修正；新增 3 用例，全量 308 passed / 2 skipped |
| P16 | 计划参数键静态校验 | 已完成 | `lint_params` 对照 `_PARAM_HINTS` 合法键拦截臆造参数，未登记工具跳过；新增 3 用例，全量 311 passed / 2 skipped |
| P17 | 订单面板发货操作（经网关） | 已完成 | 订单中心 PAID 订单显示「确认发货」，经 `/gateway/execute` 调 fulfill_order，走网关审批/风险/幂等，PENDING 提示去审批；node --check 通过，全量 EXIT=0 |
| P18 | 故障注入/恢复演练 | 已完成 | `_recover_stale_workspace` 停滞 EXECUTING 任务恢复一次、新鲜任务不误恢复；新增 2 用例，全量 313 passed / 2 skipped |
| P19 | 入站消息边界防护 | 已完成 | `input_guard.sanitize_inbound` 在意图识别/模型调用前拦截空、空白、超长(>4000字)消息，不产生动作；新增 6 用例，全量 319 passed / 2 skipped |
| P20 | 客户面板阶段推进 | 已完成 | 列表暴露 stage，客户档案可经网关 update_customer 推进 new→lead→won/lost（R1 直接 ALLOWED）；新增 2 用例，node --check 通过，全量 321 passed / 2 skipped |
| P21 | 库存面板补货 | 已完成 | 低库存商品经网关 add_stock 补货并记 receive 流水；新增 1 用例，node --check 通过，全量 322 passed / 2 skipped |
| P22 | 总览告警角标 | 已完成 | overview 暴露 low_stock/pending_ship/pending_approvals，前端非零告警条可跳转；新增 2 用例，node --check 通过，全量 324 passed / 2 skipped |
| P23 | 语音状态反馈 | 已完成 | 识别 interim 实时回显、麦克风聆听态、TTS onend 结束 talking；纯前端，node --check 通过，全量 EXIT=0 |
| P24 | 审批/待办后总览同步 | 已完成 | 批准/拒绝/确认/完成后同时刷新待办、总览角标、事件流；纯前端，node --check 通过，全量 EXIT=0 |
| P25 | 执行反馈预算截断 | 已完成 | `trim_feedback` 只保留最近 N 条反馈防 prompt 膨胀；新增 3 用例，全量 327 passed / 2 skipped |
| P26 | 提交进行中锁 | 已完成 | sendChat in-flight 锁忽略连点/语音连发，finally 释放恢复输入；纯前端，node --check 通过，全量 EXIT=0 |
| P27 | TTS 情绪语调 | 已完成 | 按成功/警告/错误语气自适应 rate/pitch；纯前端，node --check 通过，全量 EXIT=0 |
| P28 | 端到端走查验收 | 已完成 | /holo 200、/docs 200、未认证 401、前端零 JS 异常；隔离浏览器回连拒绝为沙箱限制 |
| P29 | 面板偏好持久化 | 已完成 | 播报开关/最后面板写 localStorage，启动恢复语音偏好；纯前端，node --check 通过，全量 EXIT=0 |
| P30 | 审批同键去重测试 | 已完成 | 需审批同 key 复用 PENDING 审批不重复创建；新增 1 用例，全量 328 passed / 2 skipped |
| P31 | 工具接入面板 + MCP 路线 | 已完成 | 全息台只读展示已注册 API/model/agent 工具；文档登记 MCP/第三方工具路线与安全边界；node --check，全量 EXIT=0 |
| P32 | 审计哈希链完整性 + zk 路线 | 已完成 | integrity.py 哈希链/verify_chain，AuditLog 链上哈希可检出篡改；zkSNARK/E2EE 登记外部 |
| P33 | 审计链状态校验与可视化 | 已完成 | verify_workspace_chain + GET /audit/integrity，事件面板显示链完整/断点；+2 用例 |
| P34 | 重复连发幂等冒烟 | 已完成 | 同 key 连发 5 次仅首次执行、其余回放，SUCCEEDED 恒为 1；+1 用例 |
| P35 | 跨工作区隔离边界 | 已完成 | 两独立 ws，A 的商品/自定义函数对 B 列表与网关不可见；+3 用例 |
| P36 | 订单/客户跨区隔离补测 | 已完成 | A 的订单/客户在 B 列表不可见、按 ID 访问 404；+2 用例 |
| P37 | 审批/执行记录跨区隔离补测 | 已完成 | A 的 PENDING 审批 B 不可见/不可决策(400)，Execution 按 ws 归属；+2 用例 |
| P38 | 经理人记忆跨区隔离补测 | 已完成 | A 的 AgentMemory B 不可见、删除 404、同 key 互不覆盖；+1 用例，344 passed / 2 skipped |
| P39 | owner-only E2EE 设计基线 | 已完成 | 纯文档 docs/E2EE_DESIGN.md：威胁模型/字段分级/取舍/分阶段，未写码；待 owner 拍板 |
| P40 | 拍板版隐私路线 + 自备备份目录校验 | 已完成 | 备份目录可配置/可写校验+3 用例；E2EE_DESIGN 升级拍板版；347 passed / 2 skipped |
| P41 | 虚拟号↔真实号映射占位 | 已完成 | VirtualNumber 表+allocate/bind/release/list，#8001 起占位、真实号密文占位、跨区隔离；+4 用例 |
| P42 | E2EE 密文保管层 | 已完成 | E2EEBlob+vault，服务端只存密文信封/盲索引、无密钥无 decrypt，upsert/取/精确检索/隔离；+6 用例，357 passed / 2 skipped |
| P43 | 多设备配对登记层 | 已完成 | DevicePairing+device_pairing，一次性配对码/授权存凭证句柄/吊销/列表，服务端不存主密钥，跨区隔离；+4 用例，361 passed / 2 skipped |
| P44 | 前端 WebCrypto 加密库 | 部分完成 | e2ee.js（PBKDF2/AES-GCM/盲索引/P42 REST）node 实测往返通过并挂接 holo.html；Passkey 与操作 UI 待真实浏览器联调 |
| P45 | 全息台科幻丝滑 UI | 已完成 | 纯 CSS 增强：网格纵深/弹性转场/侧栏暗化/扫光/浮起发光/状态脉冲/气泡上浮/降级；Chrome 实测双面板切换无破版；361 passed / 2 skipped |
| P46 | 3D环绕面板环+收起折叠+主动发问 | 已完成 | CSS 3D 圆环慢转、点卡/语音文字拉近展开、展开时环暗化暂停、⤓ 收起折叠回环、初始虚拟人主动发问；Chrome 实测开/收/点环全链路；361 passed / 2 skipped |
| P47 | 坐姿主播+大屏新闻墙+轻量隔空动效 | 已完成 | 坐姿女主播全息图、半透明深空数据大屏墙背景、talking 口部光晕脉动、开面板手部→面板隔空光轨、展开时主播缩左上小窗常驻可交互；实时口型/真手部追踪登记为后续外部项；Chrome 实测；361 passed / 2 skipped（仅改前端） |
| P48 | 主播融入背景+三层景深+背景放大+细腻动效 | 已完成 | Pillow 亮度键控转真透明 RGBA（备份 src 可重抠）、弃用 screen 混合消除黑方块/残影；大屏墙 150% 提亮虚化作远景、中景环绕环、前景主播三层景深；光晕收紧贴合、底部渐隐去座椅残留；呼吸/口部/手部光点/光轨保留；后端零改动，pytest EXIT=0 全 PASSED / 2 skipped |
| P49 | 环绕环卡片大幅放大+收起按钮修复 | 已完成 | 环卡 128×72→360×200、图标/标题同步放大，半径252→780、透视→2400、环上移至头顶上方成半透明面板墙不挡脸；修复 hd 冒泡抢走收起点击（stopPropagation），⤓ 贴最右加大命中区；Chrome 实测展开/收起闭环；pytest RC=0，仅改前端 |
| P50 | 开场视觉：面板居中+环正向慢转+主播原位淡化 | 已完成 | 环正向 rotateY(360) 左→右经过正面（64s/圈，展开暂停）；展开面板绝对定位 left50%/top50%/translate(-50%,-50%) 钉屏幕正中、560宽半透明毛玻璃清晰；主播不移位仅原位 dim(.22)；openPanel 只留当前卡、focusPanel 单卡强制 active，消除残留侧卡偏位；DevTools 实测 rect 居中；pytest RC=0，仅改前端 holo.html |
| P51 | 流体全息画质：深空通透+流光+缓慢纵深运动 | 已完成 | 对标深空螺旋流光参考（黑底星点/半透明白线卡慢转/暖色流光/大面积黑；抖音受登录墙未取文件，取免登录帧风格）；底压更深；新增 #aurora 漂移星云(青/紫/暖金,26s)与 #stars 星点闪烁层；newsWall .9→.5 透出深空；环卡近乎透明(.2/.32,blur10,细线)+环外46s缓转青/金conic流光光圈；展开面板液态玻璃(.56/.68,blur18)+9s自上而下液态高光；新动效入 reduced-motion；CSS250/250配平、JS node --check=0；pytest RC=0，仅改前端 holo.html CSS |
| P52 | 环墙统一线性发光SVG图标替换emoji | 已完成 | 13面板emoji实色块换统一线性SVG(24网格/fill:none/stroke currentColor/1.7线宽)，ICONS字典+iconHtml()，环卡与展开标题栏共用、无匹配回退emoji；青蓝#9fe6ff+青色发光，环卡64/46px、栏20px；环墙彻底线框化；CSS252/252配平、JS=0；Chrome实测；pytest RC=0，仅改前端 holo.html |
| P53 | 环墙点选反馈与侧面可读性增强 | 已完成 | 环卡叠加中心青柔光径向渐变提升侧面可读；rc-t文字青紫微光、hover转亮白；hover边框转青+外发光+上浮3px+图标scale1.08发光增强；active-card点亮增强；transition补transform；CSS256/256配平、JS=0；Chrome实测hover反馈；pytest RC=0，仅改前端 holo.html |
| P65 | MCP兼容JSON-RPC服务端最小层 | 已完成 | 新建 `app/mcp_server.py` + `POST /workspaces/{id}/mcp`：initialize/ping/tools/list/tools/call 信封；目录聚合内置工具+本工作区启用自定义函数，密钥(auth/headers/endpoint)不外泄；tools/call 全部经统一网关（权限/政策/审批/幂等/核验/审计不变）；未知方法-32601、缺参-32602、待审批/拒绝经isError回传；新增10用例全绿，全量 RC=0；真实 MCP 握手/stdio/SSE 传输仍登记外部 |
| P66 | 持久记忆容量治理（note封顶淘汰，保护preference/fact） | 已完成 | 新建 `app/memory_gov.py`：`enforce_capacity` 仅淘汰超容的最旧 note，preference/fact 永不删；HTTP upsert 与 set_memory 工具写后自动治理；新增 `POST /workspaces/{id}/agent/memory/gc`（owner）返回 cap/total/removed/remaining/protected_kept；零表结构迁移；新增4用例（淘汰顺序、保护关键记忆、未超容no-op、跨区隔离、GC结构），全量 RC=0 |
| P67 | HTTP工具目录与MCP对齐（聚合内置+启用函数，密钥不外泄） | 已完成 | `GET /workspaces/{id}/tools` 复用 `mcp_server.list_tools`，聚合内置与本工作区启用函数，平铺 name/description/risk_level/side_effect/idempotent/source/kind，不下发 auth/headers/endpoint；新增3用例（内置在册、函数在册且无密钥、跨区不可见），全量 RC=0 |
| P68 | 自营闭环确定性入参校验（商品/库存/订单/客户） | 已完成 | 工具层统一 `_parse_money/_parse_positive_int/_clean_text` 与状态白名单，坏输入返回可读 error 而非抛原生异常；create_draft/update/publish/add_stock/create_order/fulfill/mark_delivered/upsert/update_customer 全覆盖；HTTP Pydantic 名称非空/价格≥0/status 枚举(422)；空 items 透传 channel 结果不误伤补偿链路；新增11用例，全量 RC=0 |
| P69 | 可靠性门槛：故障兜底+只读性能预算+源码静态安全扫描 | 已完成 | handler 异常被网关捕获返回可读 error 不500；只读 R0/目录 <2s 预算；静态扫描无 shell=True/eval/exec/硬编码密钥，subprocess 仅隔离沙箱参数列表；新增6用例，全量 RC=0；跨进程并发锁/库存原子条件更新/生产压测留部署阶段 |
| P70 | 工作流条件分支与确定性跳过（M05 起步） | 已完成 | 步骤可选 condition/if（结构化 op：present/absent/truthy/contains/in/eq/ne/gt/gte/lt/lte，来源最近步或指定早前步）；谓词假→skipped 不执行/不重试/不补偿；checkpoint 识别 skipped；ctx_map 按 tool 索引结果；无 eval；新增8用例，全量 RC=0；并行DAG/事件触发留后续 |
| P71 | 事件/一次性/间隔触发工作流（M05 触发层收口） | 已完成 | trigger 三型 interval/once/event；时间循环只跑 interval(循环)/once(单次后disable)；dispatch_event 按 event 名命中、condition 对 payload 求值、cooldown 冷却去重(AgentMemory)，命中走网关；新增 POST /workspaces/{id}/events/{name}；新增6用例，全量 RC=0；并行DAG留后续 |
| P72 | 面板间数据编排总线（M03 声明式只读聚合） | 已完成 | app/data_bus.py：select 标量 count/sum/avg/min/max/first + items 投影(where/project/limit)，7个命名 collector 按 workspace 隔离，结构化 op 无 eval、无写无副作用，坏 spec 收集 error 不500；新增 POST /workspaces/{id}/bus/resolve；新增6用例，全量 RC=0 |
| P73 | 跨Agent依赖编排与命名交接（M04 确定性内核） | 内核完成/未接LLM链路 | app/collab.py：声明式DAG节点+depends_on，拓扑排序+环/未知依赖检测安全失败，前置非OK下游SKIPPED短路，$from命名handoff确定性交接(无eval,缺失warning)，executor注入可单测；新增7用例，全量RC=0；run_multiagent的LLM规划改用本内核留下一步 |
| P74 | collab依赖内核对接run_multiagent（M04收口） | 已完成 | 编排器可选id/depends_on并透传；无依赖走原顺序(行为不变)，有depends_on用collab拓扑执行，PENDING不算失败，DAG非法回退安全顺序；新增1依赖拓扑用例，原4测试保持绿，全量RC=0；跨会话长时记忆交接留后续 |
| P75 | 长时记忆分层与确定性压缩（M07/M04） | 已完成 | app/memory_layers.py：核心preference/fact保护、digest摘要层防再压缩、note可压缩；超阈值最旧一批确定性合并为digest(无LLM)，build_memory_block按预算core→digest→recent拼接，chat上下文已接入；新增POST /agent/memory/compact；6用例，全量RC=0；LLM语义摘要留后续 |
| P76 | E2EE擦除/清单与盲索引安全收紧 | 已完成 | e2ee_vault加delete/delete_owner/list_fields，find拒绝空blind_index与非法field；路由DELETE /e2ee、DELETE /e2ee/owner、GET /e2ee/list；服务端仍无decrypt；+5用例(共11)，全量RC=0；前端WebCrypto/Passkey联动后期套用户代码 |
| P77 | 就绪探针readiness与DB探活（部署地基） | 部分完成 | /health恒200 liveness，新增/health/ready执行SELECT 1，通200/异常503；4用例，全量RC=0；真实PG连接/迁移/压测留部署阶段 |
| UI-SPEC | KARMA仪表盘视觉标杆规格与落地 | 阶段1皮肤完成 | docs/UI_VISUAL_SPEC.md 拆解左导航/顶栏/中央球体6卡/右侧任务状态/底部工作台/AI主脑/语音条/配色材质动效；阶段1已在console.html落地青蓝全息皮肤(备份.bak-previsual，未改JS)；阶段2版式重排接真实接口待续，数字以后端真实接口填充 |

## P1 详细范围（本阶段）

现状：`app/workflow.py` 已有断点续跑（checkpoint）、超时恢复、失败反向补偿（退款/取消订单）、
步骤级幂等键；`app/scheduler.py` 每周期跑对账/恢复/监控。缺口：

1. **步骤瞬态错误分类**（新 `app/retry.py`）：
   - 可重试：回执未决（verification UNKNOWN/PENDING、needs_reconcile）、明确网络瞬态错误
     （timeout / unreachable / transport / 5xx / connection reset）且工具声明幂等。
   - 不重试：参数无效、权限不足、未知工具、策略拒绝、未分类错误（安全默认不盲目重试副作用）。
2. **指数退避重试**：可配置最大次数与基础间隔，`base * 2^n` 封顶；同一幂等键重放，
   成功步骤记录 attempts；耗尽仍失败走既有补偿与 UNKNOWN/BLOCKED 路径。
3. **补偿事务严格幂等去重**：已 DONE 的补偿项（按 tool+reference）在重跑/批准后重入时
   直接返回，不再向网关重复发退款/取消。
4. **对账失败计数退避**：连续失败的工作区在后续周期降频跳过，成功清零，避免雪崩。

## 外部依赖（P7，本地不硬做）

完整状态、所需外部条件、本地已就绪项与阻塞验收统一登记在 **`docs/EXTERNAL_DEPENDENCIES.md`**，以下为摘要：

- Karma 生产资金语义（托管锁定、仲裁、SDK 签名回执）→ 需官方联调。
- 平台开放审核与消息渠道生产联调（Telegram/微信/Discord）→ 需各平台资质。
- 法律责任矩阵与目标市场合规细则 → 待法务。
- 物流/客服/预约真实平台连接器 → 需平台开放文档。
- DNS 钉扎 / 出网代理白名单 → 部署环境决策。
- 分布式追踪、外部告警通道、多节点拆分/编排 → 待基础设施。
