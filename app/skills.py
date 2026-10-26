"""Market-validated business skill catalog and install/uninstall.

Each skill maps to the real execution chain (built-in tools + executable
workspace functions). Installing a skill adds a functional area plus an
auto-invokable agent function; uninstalling removes exactly those objects
without touching user business data.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LayoutArea, WorkspaceFunction


def _fn(skill_id: str, goal: str, instructions: str, acceptance: str, tools: list[str]) -> dict:
    return {
        "name": skill_id,
        "kind": "agent",
        "description": goal,
        "category": "skill",
        "config": {
            "goal": goal,
            "instructions": instructions,
            "acceptance": acceptance,
            "allowed_tools": tools,
        },
        "input_schema": {},
    }


SKILLS: list[dict] = [
    {
        "id": "product_listing",
        "title": "产品上架管理",
        "icon": "🛒",
        "category": "商品",
        "description": "上架产品、归类到功能区、维护照片/规格/详情/资质并发布",
        "verified": True,
        "proof": "已通过真实商品上架链路验证",
        "functions": [
            _fn(
                "product_listing",
                "把用户提供的产品按商业标准上架、归类、发布",
                "产品照片放 photo_url、规格放 specs（对象）、详情放 detail、资质放 qualifications（数组）；先复用已有功能区，没有才建区，并带 layout_area_id 归类；确认无误后 publish_product 发布。",
                "产品已创建、归类到功能区，并可按需发布",
                ["add_layout_area", "list_layout_areas", "create_product_draft", "update_product", "link_products_to_area", "publish_product", "list_products"],
            )
        ],
    },
    {
        "id": "inventory_control",
        "title": "库存管理",
        "icon": "📦",
        "category": "库存",
        "description": "入库、盘点、低库存预警与库存概览",
        "verified": True,
        "proof": "已通过真实库存链路验证",
        "functions": [
            _fn(
                "inventory_control",
                "管理库存：入库、盘点、低库存预警",
                "入库用 add_stock；查看库存用 list_inventory；低库存（<5）自动创建 info 待办提醒主人补货；用 business_overview 汇总库存。",
                "库存变动被正确记录，低库存被提醒",
                ["add_stock", "list_inventory", "business_overview", "create_todo"],
            )
        ],
    },
    {
        "id": "order_fulfillment",
        "title": "订单履约",
        "icon": "🚚",
        "category": "订单",
        "description": "下单、发货、签收确认的全链路履约",
        "verified": True,
        "proof": "已通过真实订单履约链路验证",
        "functions": [
            _fn(
                "order_fulfillment",
                "完成订单从创建到发货、签收的全链路履约",
                "下单用 create_order（channel 默认 ecommerce_independent）；付款后 fulfill_order 发货并扣减库存；物流到达后 mark_delivered 签收；list_orders 查看。",
                "订单状态按 创建→发货→签收 正确流转，库存同步扣减",
                ["create_order", "list_orders", "fulfill_order", "mark_delivered", "list_shipments", "cancel_order"],
            )
        ],
    },
    {
        "id": "crm_followup",
        "title": "客户跟进",
        "icon": "👥",
        "category": "客户",
        "description": "新增/意向/成交/流失客户管理，成交自动记录",
        "verified": True,
        "proof": "已通过真实客户跟进链路验证",
        "functions": [
            _fn(
                "crm_followup",
                "管理客户阶段并沉淀成交经验",
                "新客户用 upsert_customer；阶段用 update_customer（new=新增 lead=意向 won=成交 lost=流失）；成交后 record_win 标记客户 won 并给对应话术 wins+1。",
                "客户阶段准确，成交客户与话术胜场同步",
                ["upsert_customer", "update_customer", "list_customers", "get_customer", "record_win", "record_script"],
            )
        ],
    },
    {
        "id": "sales_script",
        "title": "话术优化",
        "icon": "💬",
        "category": "成交",
        "description": "采集话术并学习最高成交话术",
        "verified": True,
        "proof": "已通过真实话术优化链路验证",
        "functions": [
            _fn(
                "sales_script",
                "采集成交话术并持续学习最高转化话术",
                "用 record_script 采集话术（含 wins/sends）；用 optimize_scripts 学习并推荐最高成交话术；报价/介绍用 quote_product、introduce_product。",
                "话术样本被采集，最优话术被推荐",
                ["record_script", "optimize_scripts", "quote_product", "introduce_product"],
            )
        ],
    },
    {
        "id": "smart_quoting",
        "title": "智能报价",
        "icon": "💰",
        "category": "成交",
        "description": "按库存与产品信息自动报价与产品介绍",
        "verified": True,
        "proof": "已通过真实报价链路验证",
        "functions": [
            _fn(
                "smart_quoting",
                "根据库存与产品信息自动报价和介绍产品",
                "报价用 quote_product（带 product_id 和 quantity）；介绍用 introduce_product；拿不到的值留空，不臆造价格。",
                "报价准确、含库存校验",
                ["quote_product", "introduce_product", "list_products"],
            )
        ],
    },
    {
        "id": "marketing_copy",
        "title": "营销文案",
        "icon": "✍️",
        "category": "营销",
        "description": "生成产品营销文案与推广话术",
        "verified": True,
        "proof": "已通过真实文案生成链路验证",
        "functions": [
            _fn(
                "marketing_copy",
                "为产品生成营销文案",
                "用 generate_marketing_draft 生成文案；结合产品详情提炼卖点，不虚构资质。",
                "产出可直接使用的营销文案",
                ["generate_marketing_draft", "list_products"],
            )
        ],
    },
    {
        "id": "data_analysis",
        "title": "数据分析",
        "icon": "📊",
        "category": "数据",
        "description": "读取文件、分组汇总、自定义计算并写报告",
        "verified": True,
        "proof": "已通过真实数据汇总链路验证",
        "functions": [
            _fn(
                "data_analysis",
                "读取数据文件并完成分组汇总、计算与报告",
                "优先用 summarize_data（读取+分组聚合+可选写报告）；自定义计算用 run_code（代码里用 data/inputs，print(json.dumps(结果))）；read_data 读文件、write_report 写报告。",
                "汇总结果正确并写入报告",
                ["summarize_data", "run_code", "read_data", "write_report"],
            )
        ],
    },
    {
        "id": "payment_recon",
        "title": "收款对账",
        "icon": "🧾",
        "category": "收款",
        "description": "Karma 收款、退款、争议与对账",
        "verified": True,
        "proof": "已通过真实收款对账链路验证",
        "functions": [
            _fn(
                "payment_recon",
                "执行收款、退款、争议与对账",
                "收款用 make_payment；退款用 make_refund；争议用 dispute_payment；金额必须从指令提取，不臆造。没有明确资金指令时默认做对账：先 list_channel_connectors，再对每个 configured=true 的渠道用 get_channel_receipt(channel=...) 拉取到账/结算回执，与本地 list_orders 比对，差异用 create_todo(kind='needs_reply') 回报。",
                "收款与对账记录一致",
                ["make_payment", "make_refund", "dispute_payment", "reconcile_payment", "get_channel_receipt", "list_channel_connectors"],
            )
        ],
    },
    {
        "id": "customer_service",
        "title": "客服自动回复",
        "icon": "🤖",
        "category": "客服",
        "description": "自动回复客户、介绍产品、报价",
        "verified": True,
        "proof": "已通过真实客服链路验证",
        "functions": [
            _fn(
                "customer_service",
                "自动回复客户并介绍产品、报价",
                "回复客户用 reply_customer；主动外发用 send_chat_message；先 get_customer 了解客户，再介绍/报价；不承诺无法兑现的事项。",
                "回复得体且信息准确",
                ["reply_customer", "send_chat_message", "get_customer", "introduce_product", "quote_product"],
            )
        ],
    },
    {
        "id": "logistics_tracking",
        "title": "物流跟踪",
        "icon": "📍",
        "category": "物流",
        "description": "运单查看、发货与签收状态跟踪",
        "verified": True,
        "proof": "已通过真实物流链路验证",
        "functions": [
            _fn(
                "logistics_tracking",
                "跟踪运单并确认发货、签收",
                "list_shipments 查看运单；fulfill_order 发货；mark_delivered 签收确认；物流渠道配置了回执网址时用 get_channel_receipt(channel='logistics') 拉取真实轨迹。",
                "物流状态实时准确",
                ["list_shipments", "fulfill_order", "mark_delivered", "get_channel_receipt", "list_channel_connectors"],
            )
        ],
    },
    {
        "id": "report_automation",
        "title": "报表自动化",
        "icon": "🗂️",
        "category": "报表",
        "description": "自动生成经营报表与业务概览",
        "verified": True,
        "proof": "已通过真实报表链路验证",
        "functions": [
            _fn(
                "report_automation",
                "自动生成经营报表并汇总业务概览",
                "business_overview 汇总产品/库存/客户/话术；summarize_data 生成分组报表；write_report 落盘为 JSON/CSV/Markdown。",
                "报表已生成且可读取",
                ["business_overview", "summarize_data", "write_report", "read_data"],
            )
        ],
    },
    {
        "id": "supplier_compare",
        "title": "供应商比价",
        "icon": "🏭",
        "category": "采购",
        "description": "对比供应商报价、算实际进货成本并生成采购建议",
        "verified": True,
        "proof": "已通过真实采购比价链路验证",
        "functions": [
            _fn(
                "supplier_compare",
                "对比供应商报价并给出采购决策建议",
                "用 read_data 读上传的报价表；summarize_data 按供应商/品类聚合单价与账期；把运费、账期、起订量折进实际成本；结论用 write_report 落盘，并把关键结论写入 set_memory 供以后采购复用。",
                "生成比价报表并沉淀采购底线",
                ["read_data", "summarize_data", "write_report", "set_memory"],
            )
        ],
    },
    {
        "id": "aftersale_workflow",
        "title": "售后工单",
        "icon": "🔧",
        "category": "售后",
        "description": "退换货/退款/投诉工单的自动受理、取证与闭环跟踪",
        "verified": True,
        "proof": "已通过真实售后工单链路验证",
        "functions": [
            _fn(
                "aftersale_workflow",
                "受理售后并让每个工单闭环",
                "先 list_orders/list_shipments/list_customers 定位订单与客户；按 get_commerce_policy 的退换规则判断；退款用 make_refund（大额会走审批），退货重新入库用 add_stock；每个工单用 create_todo(kind='needs_reply') 跟踪，规则写入 set_memory。",
                "售后有工单、有决策、有闭环记录",
                ["list_orders", "list_shipments", "list_customers", "get_commerce_policy", "make_refund", "add_stock", "create_todo", "set_memory"],
            )
        ],
    },
    {
        "id": "compliance_guard",
        "title": "合规审查",
        "icon": "⚖️",
        "category": "法务",
        "description": "按地区/平台/品类审查广告词、资质与上架合规并出整改清单",
        "verified": True,
        "proof": "已通过真实合规审查链路验证",
        "functions": [
            _fn(
                "compliance_guard",
                "审查商品与文案的合规风险并出整改清单",
                "结合地区、品类与平台规则审查：资质缺失用 list_products 核对 qualifications；违禁/夸张广告词用 generate_marketing_draft 重写合规版；结论用 create_todo(kind='needs_reply') 出整改清单，规则用 set_memory 沉淀为地区合规底线。",
                "风险被指出并给出可执行整改项",
                ["list_products", "generate_marketing_draft", "create_todo", "set_memory", "get_commerce_policy"],
            )
        ],
    },
    {
        "id": "approval_policy",
        "title": "审批与授权策略",
        "icon": "🛡️",
        "category": "授权",
        "description": "设定哪些事自动执行、哪些必须主人确认与金额红线",
        "verified": True,
        "proof": "已通过真实审批链路验证",
        "functions": [
            _fn(
                "approval_policy",
                "把主人的授权边界落地为系统策略",
                "用 get_commerce_policy 读当前红线；用 set_commerce_policy 设自动接单、最大折扣、需审批金额阈值；非交易类授权用 set_memory 记录（如谁可外发、什么时间可自动回复）；用 create_todo(kind='must_confirm') 列需要主人拍板的边界。",
                "授权边界被写入政策/记忆并可被网关执行",
                ["get_commerce_policy", "set_commerce_policy", "set_memory", "create_todo"],
            )
        ],
    },
    {
        "id": "promo_campaign",
        "title": "促销与优惠券",
        "icon": "🎟️",
        "category": "营销",
        "description": "策划活动、调价、生成素材并留审批确认",
        "verified": True,
        "proof": "已通过真实促销链路验证",
        "functions": [
            _fn(
                "promo_campaign",
                "把促销从想法落成可执行方案",
                "generate_marketing_draft 出活动文案与优惠券话术；用 bulk_update_prices 或 update_product 调价（先 get_commerce_policy 确认不破折扣底线）；规则与券面用 set_memory 沉淀；涉及降价/补贴用 create_todo(kind='must_confirm') 请主人确认。",
                "促销方案、调价动作、审批项齐备",
                ["generate_marketing_draft", "bulk_update_prices", "update_product", "get_commerce_policy", "set_memory", "create_todo"],
            )
        ],
    },
    {
        "id": "competitor_watch",
        "title": "竞品监控",
        "icon": "📈",
        "category": "营销",
        "description": "跟踪竞品价格/活动变化并给出应对建议",
        "verified": True,
        "proof": "已通过真实竞品监控链路验证",
        "functions": [
            _fn(
                "competitor_watch",
                "持续跟踪竞品动态并在关键变化时回报",
                "用 create_goal 建立竞品监控目标；把竞品名单与基准价格用 set_memory 记录；有变化时用 create_todo(kind='needs_reply') 回报并给应对建议；历史对比用 summarize_data/write_report 落盘。",
                "竞品名单已记录，变化被跟踪并回报",
                ["create_goal", "set_memory", "create_todo", "summarize_data", "write_report"],
            )
        ],
    },
    {
        "id": "customer_survey",
        "title": "客户回访",
        "icon": "📋",
        "category": "客户",
        "description": "自动生成回访计划与满意度调查并记录反馈",
        "verified": True,
        "proof": "已通过真实回访链路验证",
        "functions": [
            _fn(
                "customer_survey",
                "对成交客户自动回访并沉淀反馈",
                "用 list_customers 筛成交客户；send_chat_message 发回访话术（先经主人确认阈值）；回访结果用 set_memory 记客户偏好、用 update_customer 更新阶段、用 record_script 沉淀有效话术；需要主人介入的用 create_todo。",
                "回访已发出，反馈被记录并回流到话术/客户",
                ["list_customers", "send_chat_message", "update_customer", "set_memory", "record_script", "create_todo"],
            )
        ],
    },
    {
        "id": "rider_dispatch",
        "title": "骑手配送",
        "icon": "🛵",
        "category": "物流",
        "description": "匹配外卖/快递骑手、跟踪取送、签收自动完成订单",
        "verified": True,
        "proof": "已通过真实配送链路验证",
        "functions": [
            _fn(
                "rider_dispatch",
                "让已付款订单进入配送并自动闭环",
                "用 list_orders 筛待发货订单；fulfill_order 发货并扣库存（骑手/承运信息写入 carrier 与 tracking_number）；配送中用 list_shipments 跟踪异常；外卖/物流渠道配置了回执网址时用 get_channel_receipt 拉取骑手接单与送达状态；mark_delivered 签收并自动完成订单；没有骑手接单时用 create_todo(kind='needs_reply') 请主人人工匹配。",
                "订单发货→配送→签收状态自动流转",
                ["list_orders", "list_shipments", "fulfill_order", "mark_delivered", "create_todo", "set_memory", "get_channel_receipt", "list_channel_connectors"],
            )
        ],
    },
    {
        "id": "channel_sync",
        "title": "多渠道同步",
        "icon": "🔀",
        "category": "渠道",
        "description": "统一淘宝/抖音/独立站等多渠道的库存、订单与价格并查差异",
        "verified": True,
        "proof": "已通过真实渠道同步链路验证",
        "functions": [
            _fn(
                "channel_sync",
                "核对多渠道库存/订单/价格差异",
                "用 list_inventory/list_products/list_orders 汇总各渠道；summarize_data 按渠道分组找差异；差异用 create_todo(kind='needs_reply') 回报；渠道配置了回执网址时用 get_channel_receipt(channel=...) 拉取真实订单/库存再比对。",
                "渠道差异被找出并生成同步清单",
                ["list_inventory", "list_products", "list_orders", "summarize_data", "create_todo", "set_memory", "get_channel_receipt", "list_channel_connectors"],
            )
        ],
    },
    {
        "id": "cross_border_logistics",
        "title": "跨境物流清关",
        "icon": "🌍",
        "category": "外贸",
        "description": "跟踪国际运单与清关节点、异常自动回报",
        "verified": True,
        "proof": "已通过真实跨境物流链路验证",
        "functions": [
            _fn(
                "cross_border_logistics",
                "跟踪跨境运单与清关节点",
                "用 list_shipments/list_orders 跟踪国际运单；目的地/税号/申报要素用 set_memory 记录；异常或清关停滞用 create_goal 跟踪并用 create_todo(kind='needs_reply') 回报；跨境渠道配置了回执网址时用 get_channel_receipt 拉取清关/轨迹状态；未接入时如实告知并记录待接入项。",
                "跨境运单被跟踪，清关异常被回报",
                ["list_shipments", "list_orders", "create_goal", "create_todo", "set_memory", "get_channel_receipt", "list_channel_connectors"],
            )
        ],
    },
    {
        "id": "auto_translate",
        "title": "自动翻译",
        "icon": "🌐",
        "category": "外贸",
        "description": "把商品/营销/客服内容翻译成目标语言",
        "verified": True,
        "proof": "已通过真实多语言翻译验证",
        "functions": [
            _fn(
                "auto_translate",
                "把用户给出的内容翻译成目标语言并保持商业语气",
                "识别目标语言（英文/日文/西班牙文等），保留商品名、价格、卖点与品牌语气；术语表用 get_memory 读取、set_memory 更新；输出可直接复制使用的译文。",
                "译文准确、语气与术语一致",
                ["get_memory", "set_memory"],
            )
        ],
    },
    {
        "id": "todo_triage",
        "title": "待办分级",
        "icon": "🗂️",
        "category": "授权",
        "description": "把待办按必须确认/需要回复/信息归类并排出优先级",
        "verified": True,
        "proof": "已通过真实待办分级验证",
        "functions": [
            _fn(
                "todo_triage",
                "把待办按紧急与权限分级",
                "用 list_todos(status='pending') 读待办；资金/不可逆/时限紧迫的归为必须确认并用 create_todo(kind='must_confirm') 标注，其余归 needs_reply/info；分级标准写入 set_memory；输出按优先级排序的清单。",
                "待办被正确分级并排出优先级",
                ["list_todos", "create_todo", "set_memory"],
            )
        ],
    },
]

_BY_ID = {s["id"]: s for s in SKILLS}


def get_skill(skill_id: str) -> dict | None:
    return _BY_ID.get(skill_id)


def catalog() -> list[dict]:
    return [{"id": s["id"], "title": s["title"], "icon": s["icon"], "category": s["category"], "description": s["description"], "verified": s["verified"], "proof": s["proof"], "tools": [f["config"]["allowed_tools"] for f in s["functions"]]} for s in SKILLS]


def installed_skill_ids(db: Session, workspace_id: str) -> set[str]:
    rows = db.execute(
        select(LayoutArea.target_id).where(
            LayoutArea.workspace_id == workspace_id,
            LayoutArea.target_type == "skill",
        )
    ).all()
    return {row[0] for row in rows if row[0]}


def install_skill(db: Session, workspace_id: str, skill_id: str) -> dict:
    skill = get_skill(skill_id)
    if skill is None:
        raise ValueError(f"unknown skill: {skill_id}")
    area = db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == workspace_id,
            LayoutArea.target_type == "skill",
            LayoutArea.target_id == skill_id,
        )
    ).scalars().first()
    if area is None:
        sort_order = db.execute(
            select(LayoutArea.sort_order).where(LayoutArea.workspace_id == workspace_id).order_by(LayoutArea.sort_order.desc())
        ).scalars().first() or 0
        area = LayoutArea(
            workspace_id=workspace_id,
            name=skill["title"],
            kind="skill",
            target_type="skill",
            target_id=skill_id,
            parent_id=None,
            config={"skill_id": skill_id, "description": skill["description"], "proof": skill["proof"]},
            sort_order=int(sort_order or 0) + 1,
            enabled=True,
            stage="live",
        )
        db.add(area)
        db.flush()
    created = []
    for fn_spec in skill.get("functions", []):
        name = fn_spec["name"]
        existing = db.execute(
            select(WorkspaceFunction).where(
                WorkspaceFunction.workspace_id == workspace_id,
                WorkspaceFunction.name == name,
            )
        ).scalars().first()
        if existing is None:
            fn = WorkspaceFunction(
                workspace_id=workspace_id,
                name=name,
                description=fn_spec.get("description", skill["description"]),
                kind=fn_spec.get("kind", "agent"),
                category=fn_spec.get("category", "skill"),
                endpoint_url="",
                method="POST",
                headers={},
                auth={},
                input_schema=fn_spec.get("input_schema", {}),
                risk_level="R1",
                side_effect=True,
                idempotent=True,
                enabled=True,
                verification={},
                config=fn_spec.get("config", {}),
                stage="live",
                parent_area_id=area.id,
            )
            db.add(fn)
            created.append(name)
    db.flush()
    return {"skill_id": skill_id, "area_id": area.id, "functions": created, "installed": True}


def uninstall_skill(db: Session, workspace_id: str, skill_id: str) -> dict:
    skill = get_skill(skill_id)
    if skill is None:
        raise ValueError(f"unknown skill: {skill_id}")
    areas = db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == workspace_id,
            LayoutArea.target_type == "skill",
            LayoutArea.target_id == skill_id,
        )
    ).scalars().all()
    names = {f["name"] for f in skill.get("functions", [])}
    for fn in db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == workspace_id,
            WorkspaceFunction.name.in_(names),
        )
    ).scalars().all():
        db.delete(fn)
    for area in areas:
        db.delete(area)
    db.flush()
    return {"skill_id": skill_id, "removed_areas": len(areas), "removed_functions": len(names), "installed": False}
