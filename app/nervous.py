"""Neural dispatch spine: deterministic tentacle registry + CEO dispatcher.

This layer is **pure and stateless** — no LLM, no DB, no side effect. It answers
three questions the console must answer before any work happens:

1. 谁负责（assignee role）—— every panel / tool / action owns one responsible role.
2. 能不能自己做（autonomy）—— AUTO routine vs POLICY-within-guards vs ESCALATE-to-owner.
3. 怎么做、退路是什么（plan / plan_b / cost / worst_case / receipt）—— a deterministic
   dispatch card that pairs with the gateway's own risk approval (R3+ still requires
   owner confirmation inside the gateway; this layer only labels intent and routes).

Classification aligns with ``app.risk.requires_approval``:
- R0 read-only                 -> AUTO   (routine, answer directly)
- R1 reversible draft          -> POLICY (within owner-set guardrails)
- R2 bounded outbound/mutation -> POLICY (leave a trace, still reversible)
- R3/R4 money / irreversible   -> ESCALATE (CEO cannot decide alone; owner confirms)

Unknown tools never silently execute: they default to ESCALATE (safe failure).
"""
from __future__ import annotations

# ---- autonomy levels ----
AUTO = "AUTO"            # routine / read-only; CEO acts directly, no question needed
POLICY = "POLICY"        # acts only inside owner-set guardrails; stops at the boundary
ESCALATE = "ESCALATE"    # beyond CEO authority; must go to the owner for a decision

# ---- work nature ----
ROUTINE = "ROUTINE"      # fixed/repeated, low error rate, rule-driven
ANALYSIS = "ANALYSIS"    # needs judgment / reading / reasoning before acting

# ---- priority ----
P0 = "P0"  # urgent: money at risk, blocking order, security alert
P1 = "P1"  # high: active customer / ready-to-ship order
P2 = "P2"  # medium: back-office, content, reporting
P3 = "P3"  # low: housekeeping, notes, archival

# Role ids mirror app/roles.py; `ceo` is the main brain itself (orchestrator).
DOMAINS: list[dict] = [
    {
        "id": "ceo",
        "role": "ceo",
        "name": "主脑 CEO（统筹分派）",
        "mission": "接收指令、判断轻重缓急、分派给负责角色，能自己办的不打扰老板，办不了/越界的整理成待确认项提交老板。",
        "tools": ["workspace_summary", "business_overview", "list_todos", "create_todo",
                  "list_goals", "create_goal", "update_goal", "get_memory", "set_memory",
                  "list_team_roles"],
        "panels": ["overview", "account", "notify", "task", "approval", "system"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "只做统筹、读状态、立目标、记偏好；不直接动钱、不直接对外发不可收回的话。",
        "forbidden": ["不得代老板输入支付/退款确认码", "不得绕过网关审批", "不得删除审计与备份记录"],
        "priority": P1,
        "plan": "先汇总现状与待办，按 P0→P3 排序，能在政策内办的直接派给负责角色并回执；越界项整理成 must_confirm 提交老板。",
        "plan_b": "模型/上下文不可用时，退回只读汇总并列出待办，不做任何写动作。",
        "cost": "几乎无外部成本，主要是一次读库与整理。",
        "worst_case": "误派或漏派；兜底：每件事都落待办并留回执，漏派项进待办队列由 CEO 下一轮重排。",
        "receipt": "完成后回写任务状态并向老板汇报一句结果；待确认项给出确认码入口。",
    },
    {
        "id": "listing",
        "role": "listing",
        "name": "上架员（商品建档）",
        "mission": "商品建档、归类、规格详情、批量导入与上下架。",
        "tools": ["create_product_draft", "update_product", "import_products", "publish_product",
                  "add_layout_area", "link_products_to_area", "list_products", "list_layout_areas"],
        "panels": ["product"],
        "work": ROUTINE,
        "autonomy": POLICY,
        "boundary": "建档/改草稿/归类可自主；正式发布与改价受政策约束。",
        "forbidden": ["不得自行定低于底线的售价", "不得发布无资质/违禁品"],
        "priority": P2,
        "plan": "按模板建档→归类→补规格→先存草稿，确认无误再发布。",
        "plan_b": "批量导入失败时逐条回退，保留成功项并列出失败原因清单给老板。",
        "cost": "仅占用系统存储，无外部费用。",
        "worst_case": "错录规格/错分类；兜底：发布前可改回草稿，库存流水留痕可核对。",
        "receipt": "回传新建/更新商品数与待审清单。",
    },
    {
        "id": "support",
        "role": "support",
        "name": "客服（客户成交）",
        "mission": "理解客户、在折扣底线内报价与谈判、跟进意向、推进成交。",
        "tools": ["quote_product", "introduce_product", "reply_customer", "send_chat_message",
                  "upsert_customer", "update_customer", "get_customer", "list_customers",
                  "record_win"],
        "panels": ["customer"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "在 max_discount_percent 内报价/回复；超折扣或承诺超权限条件即停。",
        "forbidden": ["不得承诺超出政策的折扣/赠品/赔付", "不得代老板对外做最终交易承诺"],
        "priority": P1,
        "plan": "读客户历史与政策→给合规报价/话术→记录阶段推进→成交记 win。",
        "plan_b": "客户要求超底线时不硬答，转为 needs_reply 待办请老板定夺。",
        "cost": "仅消息通道，无直接费用。",
        "worst_case": "说错价格/过度承诺；兜底：外发前留草稿、政策兜底，争议升级老板。",
        "receipt": "回传客户阶段变化与本次沟通要点。",
    },
    {
        "id": "growth",
        "role": "growth",
        "name": "运营增长（内容复盘）",
        "mission": "营销文案、话术优化、数据分析、经营复盘建议。",
        "tools": ["generate_marketing_draft", "optimize_scripts", "record_script",
                  "summarize_data", "read_data", "write_report", "run_code"],
        "panels": ["content", "analytics"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "只出草稿与建议，不直接投放/不改价；分析只读+写报告文件。",
        "forbidden": ["不得把建议当既定决策执行", "不得跑非沙箱/联网代码"],
        "priority": P2,
        "plan": "读数据→出文案/复盘建议→写报告文件→交老板选用。",
        "plan_b": "数据缺失时基于现有样本给保守建议，并标注数据不足。",
        "cost": "模型调用 token 成本；代码分析在沙箱内无联网。",
        "worst_case": "建议偏差；兜底：仅为建议，落地前由老板/CEO 复核。",
        "receipt": "回传草稿/报告路径与关键结论。",
    },
    {
        "id": "fulfillment",
        "role": "fulfillment",
        "name": "履约员（订单发货）",
        "mission": "接单、库存核对、发货、签收、物流跟踪、按规取消。",
        "tools": ["create_order", "list_orders", "list_inventory", "add_stock",
                  "fulfill_order", "mark_delivered", "list_shipments", "cancel_order"],
        "panels": ["order"],
        "work": ROUTINE,
        "autonomy": POLICY,
        "boundary": "自动接单仅在 auto_accept 且未超阈值；发货/签收按单执行；取消留痕。",
        "forbidden": ["不得超卖（库存不足仍发货）", "不得在未确认收款时发货"],
        "priority": P1,
        "plan": "核库存与收款→生成/匹配运单→发货扣库存→回写物流状态。",
        "plan_b": "物流通道异常时挂起该单并建待办，不伪造运单号。",
        "cost": "物流费由真实渠道结算，系统不垫资。",
        "worst_case": "错发/漏发；兜底：库存流水与运单留痕，可追溯召回。",
        "receipt": "回传发货单号、状态与库存扣减结果。",
    },
    {
        "id": "finance",
        "role": "finance",
        "name": "财务（资金结算）",
        "mission": "收款、退款、对账、结算；资金与争议一律老板确认。",
        "tools": ["make_payment", "make_refund", "reconcile_payment", "dispute_payment",
                  "bulk_update_prices", "get_commerce_policy", "set_commerce_policy"],
        "panels": ["ledger", "settings"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "对账/读政策可做；任何收、付、退、争议、批量调价、改政策阈值都由强制升级集合拦截，必须老板确认。",
        "forbidden": ["不得自行退款/扣款/调价", "不得代老板确认资金动作", "不得改审批阈值"],
        "priority": P0,
        "plan": "先对账核对→把资金动作整理成 must_confirm 待办，附金额与依据，等老板确认码。",
        "plan_b": "渠道无回执时标记 UNKNOWN 待核验，绝不重复发起支付。",
        "cost": "涉及真实资金，错误代价最高。",
        "worst_case": "错付/错退；兜底：幂等去重+确认码只发老板+审计链不可删。",
        "receipt": "回传对账差异与待确认金额，老板确认后回写结算结果。",
    },
    {
        "id": "connect",
        "role": "ceo",
        "name": "连接/工具与渠道（接入管理）",
        "mission": "技能安装、自定义函数、渠道连接器登记与目录管理。",
        "tools": ["install_skill", "list_skills", "create_function", "run_function",
                  "promote_function", "list_channel_connectors", "get_channel_receipt"],
        "panels": ["connect", "market", "merchant", "security"],
        "work": ROUTINE,
        "autonomy": POLICY,
        "boundary": "登记/启用/安装已验证技能可做；密钥加密存、不明文；对外写回需老板知会。",
        "forbidden": ["不得明文回显密钥/令牌", "不得接入未验证第三方模块"],
        "priority": P2,
        "plan": "登记渠道→密钥加密入档→先 test 区跑通再 promote。",
        "plan_b": "渠道不通时保留登记并标记未连接，不阻塞其它面板。",
        "cost": "由各渠道实际计费，系统只存加密配置。",
        "worst_case": "误接异常渠道；兜底：连接可停用/删除，审计留痕。",
        "receipt": "回传连接状态与已装技能清单。",
    },
    {
        "id": "devops",
        "role": "ceo",
        "name": "系统工程（变更评审，不自动落地）",
        "mission": "读懂需求、拆变更单、列测试要点与回滚、登记回归结果；只出草案与评审意见。",
        "tools": ["read_structure", "draft_change", "regression_checklist", "file_change_ticket"],
        "panels": ["devops"],
        "work": ANALYSIS,
        "autonomy": ESCALATE,
        "boundary": "仅只读、拆单、列测试/回滚、登记回归；绝不自动写生产文件、装依赖、重启、联网拉代码。",
        "forbidden": [
            "不得直接写/覆盖 app 生产文件",
            "不得 pip/npm 安装升级依赖",
            "不得重启服务或自动上线",
            "不得联网 git clone / curl 拉取未知代码或包",
            "不得跳过回归直接批准",
        ],
        "priority": P2,
        "plan": "需求→安全扫描→确定性拆单→登记回归结果→老板审批→人工受控落盘→手动标记已落地。",
        "plan_b": "需求不清或命中危险意图时退回澄清/标 R3，不提交审批、不生成可执行产物。",
        "cost": "纯本地整理，无外部成本；真实落地由受控人工执行。",
        "worst_case": "误批/误落；兜底：审批门禁（fail 禁批、pending 需豁免）+变更前快照+拒绝即维持现状。",
        "receipt": "回传变更单状态、回归结论与审批结果，每步留痕可查。",
    },
    {
        "id": "copywriter",
        "role": "growth",
        "name": "文案 Agent（产品文案）",
        "mission": "按商品/人群/卖点产出结构化文案骨架与多版本，合规自检，交 CEO 拍板。",
        "tools": ["copy_draft", "content_submit", "content_approve", "content_reject",
                  "content_to_ready", "list_content"],
        "panels": ["copy"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "只产草稿与合规提醒，不虚构功效；批准前仅 draft，发布与对外承诺须 CEO/老板确认。",
        "forbidden": ["不得使用最佳/第一/根治等绝对化或医疗承诺词", "不得把草稿当已批准内容",
                      "不得自行对外发布或承诺疗效/退款"],
        "priority": P2,
        "plan": "收商品/人群/卖点→生成本地骨架+合规自检→提交拍板→approved→to-ready 交社交。",
        "plan_b": "卖点不足时给占位骨架并标注待补，绝不编造未验证功效；模型未配置则用本地骨架。",
        "cost": "模型润色 token 成本；本地骨架零外部成本。",
        "worst_case": "夸大/违规措辞；兜底：合规提醒清单+CEO 驳回回 draft，不进入发布队列。",
        "receipt": "回传草稿 id、多版本文案与合规提醒，状态变更全程审计留痕。",
    },
    {
        "id": "video",
        "role": "growth",
        "name": "视频 Agent（分镜/脚本/参数单）",
        "mission": "产出选题、分镜脚本、旁白与制作参数单；真实成片生成需凭证，不伪造。",
        "tools": ["video_draft"],
        "panels": ["video"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "只写脚本/分镜/参数单；不生成真实视频、不伪造成片 URL；批准前仅 draft。",
        "forbidden": ["不得伪造成片地址或生成成功状态", "不得承诺播放量/转化效果",
                      "不得自行发布视频"],
        "priority": P2,
        "plan": "收选题/时长/画幅→生成分镜脚本+参数单→提交拍板→approved→to-ready 交社交。",
        "plan_b": "生成 API 无凭证时保持 credential_required，仅交付可执行脚本，等待补凭证位。",
        "cost": "文生/图生视频 API 按量计费（待凭证）；脚本撰写零外部成本。",
        "worst_case": "空等或伪造成功；兜底：凭证位显式标注，脚本可先用于人工/外部制作。",
        "receipt": "回传脚本 id、分镜表、参数单与 generation=credential_required 状态。",
    },
    {
        "id": "social",
        "role": "growth",
        "name": "社交运营 Agent（发布/评论/热门）",
        "mission": "维护发布队列、评论箱与回复草稿、热门视频建档提炼；发布须老板+渠道凭证。",
        "tools": ["publish_queue", "content_publish", "comment_record", "comment_approve_reply",
                  "trending_record", "list_comments", "list_trending"],
        "panels": ["social"],
        "work": ANALYSIS,
        "autonomy": POLICY,
        "boundary": "评论录入/回复草稿/建档档案可做；真实发布、回复外发须老板确认且渠道凭证齐备。",
        "forbidden": ["不得在无渠道凭证时声称发布成功", "不得自动发送回复或承诺退款/赔偿",
                      "不得联网抓取或编造平台数据", "不得绕过 CEO 拍板直接发布"],
        "priority": P2,
        "plan": "ready 入队→闸门校验状态+凭证→老板确认发布；评论建档→草稿→老板确认；热门建档提炼。",
        "plan_b": "渠道/抓取无凭证时保持凭证位与排队态，不阻塞文案/视频草稿线。",
        "cost": "各平台 API 计费（待凭证）；本地分析零成本。",
        "worst_case": "误发/差评回复不当；兜底：闸门拒绝+回复仅草稿+审计 DENIED 留痕。",
        "receipt": "回传队列、评论状态、确认回执与热门点提炼结果，全程可追溯。",
    },
]


def _index() -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for d in DOMAINS:
        for t in d["tools"]:
            idx[t] = d
    return idx


_TOOL_INDEX = _index()

# panel key -> domain id (fallback ceo)
_PANEL_OWNER: dict[str, str] = {}
for d in DOMAINS:
    for p in d.get("panels", []):
        _PANEL_OWNER[p] = d["id"]

# Tools that always escalate regardless of domain (R3/R4 money + irreversible).
_FORCE_ESCALATE = {
    "make_payment", "make_refund", "dispute_payment",
    "bulk_update_prices", "set_commerce_policy",
}
# Public alias imported by the gateway dispatch gate.
FORCE_ESCALATE = set(_FORCE_ESCALATE)


def _card(domain: dict, tool: str | None = None, reason: str = "") -> dict:
    return {
        "domain": domain["id"],
        "role": domain["role"],
        "owner_name": domain["name"],
        "mission": domain["mission"],
        "work": domain["work"],
        "autonomy": domain["autonomy"],
        "escalate": domain["autonomy"] == ESCALATE,
        "boundary": domain["boundary"],
        "forbidden": list(domain["forbidden"]),
        "priority": domain["priority"],
        "plan": domain["plan"],
        "plan_b": domain["plan_b"],
        "cost": domain["cost"],
        "worst_case": domain["worst_case"],
        "receipt": domain["receipt"],
        "tool": tool,
        "reason": reason,
    }


def assign(tool: str) -> dict:
    """Return the CEO dispatch card for a tool/action name.

    Unknown tools resolve to a conservative ESCALATE card rather than executing.
    """
    if not tool:
        return _card(_DOMAIN_FALLBACK, reason="empty intent; default escalate")
    if tool in _FORCE_ESCALATE:
        d = _TOOL_INDEX.get(tool) or _DOMAIN_FALLBACK
        card = _card(d, tool, reason="资金/不可逆/改政策阈值，必须老板确认")
        card["escalate"] = True
        card["autonomy"] = ESCALATE
        return card
    d = _TOOL_INDEX.get(tool)
    if d is None:
        return _card(_DOMAIN_FALLBACK, tool, reason="未登记动作，安全失败：先提交老板判断")
    return _card(d, tool)


def dispatch_meta(tool: str) -> dict:
    """Compact dispatch metadata attached to an action record (who owns it,
    autonomy, fallback plan). Read-only; never blocks execution itself."""
    card = assign(tool)
    return {
        "dispatch_owner": card["owner_name"],
        "dispatch_role": card["role"],
        "dispatch_autonomy": card["autonomy"],
        "dispatch_work": card["work"],
        "dispatch_escalate": card["escalate"],
        "dispatch_plan_b": card["plan_b"],
        "dispatch_priority": card["priority"],
    }


def escalation_brief() -> str:
    """Deterministic escalation block injected into the planner prompt.

    Lists every force-escalation tool with its owning role and an explicit
    'never place directly in steps; open a must_confirm todo' instruction.
    Pure string; the gateway remains the last line of defense.
    """
    lines = ["【强制升级清单 · 不得直接执行】以下工具永远不能放进 steps 直接调用，"
             "也不得由 Agent 代主人确认；只能 create_todo(kind=\"must_confirm\") 整理成"
             "待办并说明金额/依据，等主人用邮件确认码拍板："]
    for tool in sorted(_FORCE_ESCALATE):
        owner = _TOOL_INDEX.get(tool, _DOMAIN_FALLBACK)
        lines.append(f"- {tool}（负责：{owner['name']}）：资金/不可逆/改政策阈值，必须主人确认。")
    return "\n".join(lines)


def panel_owner(panel: str) -> dict:
    """Dispatch card for a first-screen panel key (who owns that page)."""
    dom_id = _PANEL_OWNER.get(panel, "ceo")
    d = next(x for x in DOMAINS if x["id"] == dom_id)
    return _card(d, reason=f"panel:{panel}")


def tentacles_index() -> list[dict]:
    """Read-only index of every registered domain/tentacle for display."""
    return [
        {
            "id": d["id"],
            "role": d["role"],
            "name": d["name"],
            "mission": d["mission"],
            "work": d["work"],
            "autonomy": d["autonomy"],
            "priority": d["priority"],
            "tools": list(d["tools"]),
            "panels": list(d.get("panels", [])),
            "boundary": d["boundary"],
            "forbidden": list(d["forbidden"]),
            "plan": d["plan"],
            "plan_b": d["plan_b"],
            "cost": d["cost"],
            "worst_case": d["worst_case"],
            "receipt": d["receipt"],
        }
        for d in DOMAINS
    ]


def overview() -> dict:
    """Global neural ledger: counts, force-escalation list, priority/autonomy rollups."""
    all_tools: set[str] = set()
    by_priority: dict[str, list[dict]] = {P0: [], P1: [], P2: [], P3: []}
    autonomy_dist: dict[str, int] = {AUTO: 0, POLICY: 0, ESCALATE: 0}
    domain_rows = []
    for d in DOMAINS:
        for t in d["tools"]:
            all_tools.add(t)
        autonomy_dist[d["autonomy"]] = autonomy_dist.get(d["autonomy"], 0) + 1
        by_priority.setdefault(d["priority"], []).append(
            {"domain": d["id"], "name": d["name"], "role": d["role"]}
        )
        domain_rows.append({
            "id": d["id"],
            "name": d["name"],
            "role": d["role"],
            "work": d["work"],
            "autonomy": d["autonomy"],
            "priority": d["priority"],
            "tools_count": len(d["tools"]),
            "panels": list(d.get("panels", [])),
            "boundary": d["boundary"],
            "forbidden_count": len(d["forbidden"]),
        })

    force = []
    for tool in sorted(_FORCE_ESCALATE):
        owner = _TOOL_INDEX.get(tool, _DOMAIN_FALLBACK)
        force.append({
            "tool": tool,
            "owner": owner["name"],
            "reason": "资金/不可逆/改政策阈值，必须老板确认",
        })

    return {
        "domains_total": len(DOMAINS),
        "tools_registered": len(all_tools),
        "force_escalate": force,
        "by_priority": by_priority,
        "autonomy_distribution": autonomy_dist,
        "domains": domain_rows,
    }


_DOMAIN_FALLBACK = {
    "id": "fallback",
    "role": "ceo",
    "name": "主脑 CEO（待判定）",
    "mission": "未登记动作先暂停，提交老板判断，不擅自执行。",
    "tools": [],
    "work": ANALYSIS,
    "autonomy": ESCALATE,
    "boundary": "只上报，不执行。",
    "forbidden": ["不得猜测执行未登记动作"],
    "priority": P1,
    "plan": "整理上下文提交老板，等待明确指令。",
    "plan_b": "保持现状，不做写动作。",
    "cost": "无。",
    "worst_case": "误判；兜底：安全失败即停止。",
    "receipt": "回传待判定项给老板。",
}
