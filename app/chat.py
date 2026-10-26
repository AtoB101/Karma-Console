"""Conversational layer for the GPT-style chat.

Parses natural-language requests into gateway actions so the chat window can
actually change the workspace. Every mutation still flows through the unified
gateway (permission -> policy -> approval -> execution -> verification ->
audit); the chat only orchestrates and reports.

The parser is intentionally small and deterministic for the MVP; swapping it
for a model-driven planner later does not change the execution contract.
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentMemory, Decision, Goal, Intent, LayoutArea, MerchantProfile, RiskLevel, Task, TaskStatus, Todo, Workspace, WorkspaceFunction
from .security import Actor

AREA_RE = re.compile(r"(产品展示区|商品展示区|产品管理|产品区|商品区|展示区|价格区|库存区|订单区)")
PARENT_RE = re.compile(r"在\s*([\u4e00-\u9fa5A-Za-z0-9]+?功能区)\s*(?:给我|帮我|上|里|加|建|放|添加)")
PRODUCT_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9][\u4e00-\u9fa5A-Za-z0-9·\-]*?)\s+(\d+(?:\.\d{1,2})?)\s*(?:元|块)?"
)
STOP_WORDS = {"元", "块", "给我", "帮我", "加上", "添加", "一个", "产品", "商品", "单价", "价格", "归类", "到", "这个", "展示区", "产品展示区", "产品区", "商品区"}


def _key(prefix: str, message: str, index: int = 0) -> str:
    digest = hashlib.sha256(f"{prefix}:{message}:{index}".encode("utf-8")).hexdigest()
    return digest[:32]


def parse_products(message: str) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    normalized = re.sub(r"[¥￥]", " ", message)
    for match in PRODUCT_RE.finditer(normalized):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" -·")
        price_text = match.group(2)
        if not name or name in STOP_WORDS or not re.search(r"[\u4e00-\u9fa5A-Za-z]", name):
            continue
        price = float(price_text)
        if price <= 0 or name in seen:
            continue
        seen.add(name)
        found.append({"name": name, "price": price})
    return found


def parse_product_details(message: str) -> dict:
    """Best-effort extraction of photo/specs/detail/qualifications from free text."""
    details: dict = {"photo_url": "", "specs": {}, "detail": "", "qualifications": []}
    photo = re.search(r"(?:图片|照片|图|photo)\s*[:：]?\s*(https?://\S+)", message, re.IGNORECASE)
    if photo:
        details["photo_url"] = photo.group(1).rstrip("，。,. ")
    detail_match = re.search(r"(?:详情|描述|说明|detail)\s*[:：]\s*(.{0,400})", message, re.IGNORECASE)
    if detail_match:
        details["detail"] = detail_match.group(1).strip()
    quals = re.findall(r"(?:资质|证书|认证)\s*[:：]\s*([^\n，。;；]+)", message)
    if quals:
        details["qualifications"] = [q.strip() for q in quals if q.strip()]
    for key, value in re.findall(r"((?:规格|参数|颜色|尺码|型号|容量|材质|重量|尺寸))\s*[:：]\s*([^\n，。;；]+)", message):
        details["specs"][key] = value.strip()
    for match in re.finditer(r"(颜色|尺码|型号|规格|材质|尺寸)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9#/·\-]+)", message):
        details["specs"][match.group(1)] = match.group(2)
    return details


def extract_area_name(message: str) -> str | None:
    match = AREA_RE.search(message)
    return match.group(1) if match else None


def extract_parent_area(message: str) -> str | None:
    match = PARENT_RE.search(message)
    return match.group(1) if match else None


BUSINESS_NOUN = (
    "老板|掌柜|店主|负责人|餐厅|餐馆|饭店|饭馆|川菜|湘菜|粤菜|火锅|奶茶|咖啡|餐饮|外卖|小吃|服装|鞋店|电商|网店|独立站|外贸|跨境|工厂|公司|工作室|门店|店铺|生意|店|馆|物流|快递|货运|出行|美容|美发|美甲|化妆|化妆品|美妆|护肤|彩妆|香水|口红|面膜|日化|洗护|家政|教育|培训|健身|瑜伽|维修|摄影|婚庆|咨询|律师|设计|装修|宠物|洗衣|洗车|房产|酒店|民宿"
)
BUSINESS_ID_RE = re.compile(
    rf"(?:我是|我开|我经营|我做|我有一家|我这家|咱是|咱开)[^，。？！\n]{{0,24}}(?:{BUSINESS_NOUN})"
)
BUSINESS_ASK_RE = re.compile(
    r"(?:我是|我开|我经营|我做|我有一家|我这家|咱是|咱开)[^，。？！\n]{0,24}?(?:你能帮我做什么|能帮我做什么|你能做什么|帮我做什么|能为我做什么|你会做什么)"
)

BUSINESS_KEYWORDS = {
    "food": ["川菜", "湘菜", "粤菜", "火锅", "餐厅", "餐馆", "饭店", "餐饮", "外卖", "奶茶", "咖啡", "烘焙", "烧烤", "面馆", "小吃", "食堂", "快餐", "茶饮", "菜单", "出餐", "菜品"],
    "clothing": ["服装", "衣服", "服饰", "鞋", "箱包", "饰品", "穿搭", "女装", "男装"],
    "beauty": ["化妆", "化妆品", "美妆", "护肤", "彩妆", "香水", "口红", "面膜", "日化", "洗护", "洁面", "精华", "粉底", "眼影", "唇膏", "卸妆", "爽肤水", "面霜"],
    "housing": ["房产", "住宿", "酒店", "民宿", "租房", "公寓", "家政"],
    "transport": ["物流", "快递", "货运", "出行", "打车", "汽修", "租车"],
    "ecommerce": ["电商", "网店", "独立站", "外贸", "跨境", "代购", "直播带货", "亚马逊", "速卖通", "淘宝", "天猫", "京东", "拼多多", "抖音", "小红书", "shopify"],
    "service": ["美容", "美发", "美甲", "家政", "教育", "培训", "健身", "瑜伽", "维修", "摄影", "婚庆", "咨询", "律师", "设计", "洗车", "洗衣", "宠物", "装修"],
}

CATEGORY_PLAYBOOK = {
    "food": (
        "围绕餐饮/川菜馆，我可以立刻接手这些（全部走统一网关，重要动作需你确认）：\n"
        "1. 菜单上线：把菜品、单价、图片、规格、辣度/份量、介绍一次建好并归类到「菜单管理」区。\n"
        "2. 调价与菜品文案：改单价、换图、写介绍/营销文案。\n"
        "3. 自动接单：平台来单后用 create_order 建单并自动回复确认。\n"
        "4. 出餐派单：出餐后用 fulfill_order 记录骑手与运单号并扣库存。\n"
        "5. 送达确认：骑手送达后用 mark_delivered 自动完成订单。\n"
        "6. 客户与话术：记录新客/意向/成交，采集并优化成交话术。\n"
        "7. 待办与汇报：退款、异常单等需要你拍板的事进待办并发确认码。\n"
        "要「自动匹配骑手」需先接入配送平台（美团/饿了么/达达/闪送）：把平台回执/API 地址在功能区添加为功能，我会读回执并按单号履约。"
    ),
    "ecommerce": (
        "围绕电商/独立站/外贸，我可以立刻接手这些：建商品与归区、改价与文案、自动报价回复、订单与履约、客户阶段与成交话术、库存物流、以及接入你的回执网址读取库存订单。重要动作同样需你确认。"
    ),
    "clothing": (
        "围绕服装店，我可以立刻接手这些：商品建区归类、价格/规格/详情/资质、营销文案与报价、客户与成交话术、订单履约与库存。"
    ),
    "transport": (
        "围绕物流/出行，我可以立刻接手这些：运单跟踪、订单履约与送达确认、客户跟进与话术、对账与待办汇报。"
    ),
    "housing": (
        "围绕住宿/房产，我可以立刻接手这些：房源/商品信息与价格、咨询报价与文案、客户意向与成交跟进、订单与确认、待办汇报。"
    ),
    "service": (
        "围绕服务/预约类生意（美容、家政、教育、维修、摄影、咨询等），我可以立刻接手这些：服务项目与价格管理、咨询报价与文案、客户建档与预约跟进、成交话术优化、待办与预约提醒、经营报表。"
    ),
    "beauty": (
        "围绕化妆品/美妆/护肤品生意，我可以立刻接手这些：\n"
        "1. 产品建档：把名称、单价、类目、规格（容量/功效/成分）、产品详情、资质（备案号/检测报告）一次整理进产品库并归入「产品展示区」。\n"
        "2. 批量导入：你把产品清单贴给我（每行「产品名 价格」），或上传 CSV/JSON，我自动解析归类。\n"
        "3. 定价与文案：改价、写卖点/功效文案、生成营销话术。\n"
        "4. 报价与客户：客户询价自动报价，记录新增/意向/成交并优化成交话术。\n"
        "5. 订单履约与库存：下单、发货、签收确认、库存预警。\n"
        "6. 资质与合规提醒：备案/资质到期等事项进待办，重要动作需你确认。"
    ),
    "general": (
        "我可以围绕你的生意接手这些：建商品/功能并归类、定价与营销文案、报价与客户话术、订单履约、库存物流、待办确认与对账。告诉我具体品类和第一件想自动化的任务，我就直接开始搭建。"
    ),
}

CATEGORY_HINTS = {
    "food": "餐饮履约：接单用 create_order；出餐派骑手用 fulfill_order(order_id, carrier=配送平台, tracking_number=骑手单号)；骑手确认送达用 mark_delivered；菜单/单价用 update_product、bulk_update_prices；菜品介绍用 introduce_product/generate_marketing_draft。",
    "ecommerce": "电商履约：订单 create_order/cancel_order，发货 fulfill_order，签收 mark_delivered；回执/库存读取走用户添加的 read 类功能或 read_data。",
    "clothing": "服装：商品/规格/资质用 create_product_draft/update_product；报价 quote_product；客户 upsert_customer/update_customer。",
    "beauty": "美妆/化妆品：产品建档用 create_product_draft/update_product（规格 specs、详情 detail、资质 qualifications、照片 photo_url）；批量导入用 import_products；报价 quote_product；客户 upsert_customer/update_customer；合规/资质到期用 create_todo(kind='needs_reply')。",
    "transport": "物流：运单 list_shipments，发货 fulfill_order，签收 mark_delivered。",
    "housing": "住宿：房源当商品管理，报价 quote_product，客户 upsert_customer/update_customer，确认 create_todo。",
    "service": "服务预约：服务项当商品管理（create_product_draft/update_product）；咨询报价 quote_product；客户建档与跟进 upsert_customer/update_customer；预约/提醒用 create_todo(kind='needs_reply')；服务完成用 record_win 记成交。",
    "general": "通用：先用 business_overview/list_products 摸清现状，再按需执行。",
}


SCENARIO_PACKS = {
    "food": ["product_listing", "inventory_control", "order_fulfillment", "crm_followup", "smart_quoting", "marketing_copy", "customer_service", "payment_recon", "logistics_tracking", "report_automation"],
    "ecommerce": ["product_listing", "inventory_control", "order_fulfillment", "crm_followup", "smart_quoting", "marketing_copy", "customer_service", "payment_recon", "logistics_tracking", "report_automation", "data_analysis"],
    "clothing": ["product_listing", "inventory_control", "order_fulfillment", "crm_followup", "smart_quoting", "marketing_copy", "customer_service", "payment_recon", "logistics_tracking"],
    "beauty": ["product_listing", "inventory_control", "order_fulfillment", "crm_followup", "smart_quoting", "marketing_copy", "customer_service", "payment_recon", "logistics_tracking", "data_analysis"],
    "transport": ["order_fulfillment", "logistics_tracking", "crm_followup", "report_automation"],
    "housing": ["product_listing", "crm_followup", "smart_quoting", "customer_service", "report_automation"],
    "service": ["product_listing", "crm_followup", "customer_service", "smart_quoting", "marketing_copy", "report_automation", "data_analysis"],
    "general": ["product_listing", "inventory_control", "order_fulfillment", "crm_followup", "marketing_copy", "report_automation"],
}


def _business_category(message: str) -> str:
    for category, keywords in BUSINESS_KEYWORDS.items():
        if any(keyword in message for keyword in keywords):
            return category
    return "general"


def _shop_name(message: str) -> str:
    match = re.search(r"(?:店名(?:叫|是)|叫)([\u4e00-\u9fa5A-Za-z0-9]{1,20})", message)
    if match:
        return match.group(1)
    match = re.search(r"(?:我是一家|我开了(?:一家)?|我的)([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:店|馆|餐厅|公司|工作室))", message)
    if match:
        return match.group(1)
    return "我的店"


def detect_intent(message: str) -> str:
    lowered = message.lower()
    if BUSINESS_ASK_RE.search(message):
        return "business_onboarding"
    if BUSINESS_ID_RE.search(message) and re.search(r"(你能帮我做什么|能帮我做什么|能为我做什么|你能做什么|会做什么|能做什么)", message):
        return "business_onboarding"
    if "功能" in message and any(v in message for v in ("开发", "做一个", "帮我做", "搭建", "创建", "新增", "加一个", "添加一个", "建一个")):
        return "develop_feature"
    if any(v in message for v in ("跟踪", "监控", "持续关注", "持续跟进", "盯着", "盯一下", "长期目标", "设立目标", "目标跟踪", "每日盯", "每天盯")):
        return "goal"
    if ("支付" in message) or ("付款" in message) or ("pay" in lowered):
        return "payment"
    if ("文案" in message) or ("营销" in message) or ("copy" in lowered):
        return "copy"
    if ("产品" in message or "商品" in message) and any(v in message for v in ("丢给你", "发给你", "发你", "整理", "导入", "产品库", "都给你", "全部给你")):
        return "product_import"
    if ("产品" in message or "商品" in message) and ("区" in message or "归类" in message or "单价" in message or parse_products(message)):
        return "product_display"
    if "库存" in message:
        return "inventory"
    if "运单" in message or "物流" in message or "发货单" in message:
        return "shipments"
    if "订单" in message or "下单" in message:
        return "orders"
    if ("技能" in message) or ("你能" in message and any(v in message for v in ("调用", "会", "做什么", "干"))) or ("功能" in message and any(v in message for v in ("有哪些", "有什么", "能做什么", "会什么"))):
        return "capabilities"
    identity = BUSINESS_ID_RE.search(message)
    if identity:
        rest = message[identity.end():].strip("，。！？、；; \n")
        if not rest:
            return "business_onboarding"
    return "help"


def _record_task(db: Session, actor: Actor, message: str, result: dict) -> Task:
    intent = Intent(workspace_id=actor.workspace_id, actor_id=actor.user.id, goal=message, risk_level=RiskLevel.R1.value)
    db.add(intent)
    db.flush()
    task = Task(
        workspace_id=actor.workspace_id,
        actor_id=actor.user.id,
        intent_id=intent.id,
        status=TaskStatus.COMPLETED.value,
        plan=result.get("plan", []),
        result=result,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


async def _execute(db: Session, actor: Actor, tool: str, params: dict, message: str, index: int):
    from .gateway import execute

    return await execute(db, actor, tool, params, idempotency_key=_key(tool, message, index))


async def _handle_product_display(db: Session, actor: Actor, message: str) -> dict:
    area_name = extract_area_name(message) or "产品展示区"
    parent_name = extract_parent_area(message)

    actions: list[dict] = []
    parent_id: str | None = None

    if parent_name:
        existing = db.execute(
            select(LayoutArea).where(LayoutArea.workspace_id == actor.workspace_id, LayoutArea.name == parent_name)
        ).scalars().first()
        if existing is not None:
            parent_id = existing.id
        else:
            res = await _execute(db, actor, "add_layout_area", {"name": parent_name, "kind": "module", "target_type": "group"}, message, 0)
            if res.decision is Decision.ALLOWED and res.result and res.result.get("area_id"):
                parent_id = res.result["area_id"]
            actions.append({"tool": "add_layout_area", "name": parent_name, "decision": res.decision.value, "result": res.result})

    area = db.execute(
        select(LayoutArea).where(
            LayoutArea.workspace_id == actor.workspace_id,
            LayoutArea.name == area_name,
            (LayoutArea.parent_id == parent_id) if parent_id is not None else (LayoutArea.parent_id.is_(None)),
        )
    ).scalars().first()
    if area is not None:
        area_id = area.id
    else:
        target_type = "product_manage" if "管理" in area_name else "product_display"
        res = await _execute(
            db, actor, "add_layout_area",
            {"name": area_name, "kind": "module", "target_type": target_type, "parent_id": parent_id},
            message, 1,
        )
        area_id = res.result.get("area_id") if (res.decision is Decision.ALLOWED and res.result) else None
        actions.append({"tool": "add_layout_area", "name": area_name, "decision": res.decision.value, "result": res.result})

    products = parse_products(message)
    details = parse_product_details(message)
    created: list[dict] = []
    for index, product in enumerate(products):
        res = await _execute(
            db, actor, "create_product_draft",
            {
                "name": product["name"],
                "price": product["price"],
                "category": area_name,
                "layout_area_id": area_id,
                **details,
            },
            message, index + 2,
        )
        if res.decision is Decision.ALLOWED and res.result and res.result.get("product_id"):
            created.append(res.result)
        actions.append({"tool": "create_product_draft", "name": product["name"], "decision": res.decision.value, "result": res.result})

    parent_text = f"「{parent_name}」下的子功能" if parent_name else "功能区"
    if products:
        lines = "\n".join(f"- {p['name']} ¥{p['price']:g}" for p in created)
        reply = f"已添加{parent_text}「{area_name}」，并把 {len(created)} 件商品归类进去（链路已通过网关核验）：\n{lines}"
    else:
        reply = f"已添加{parent_text}「{area_name}」。请把商品和单价一起发给我（例如：棉麻衬衫 99元、牛仔裤 159元），我会自动归类进去。"

    plan = [a["tool"] for a in actions]
    result = {"reply": reply, "intent": "product_display", "area_id": area_id, "parent_id": parent_id, "created": created, "actions": actions, "plan": plan}
    _record_task(db, actor, message, result)
    return result


async def _handle_product_import(db: Session, actor: Actor, message: str) -> dict:
    """Take over a product dump: import whatever is inline, else explain how to send."""
    products = parse_products(message)
    if products:
        return await _handle_product_display(db, actor, message)
    reply = (
        "可以，直接把产品发我就行，两种方式任选：\n"
        "1. 直接粘贴清单，每行一个「产品名 价格」，例如：\n"
        "   玻尿酸精华 89元\n"
        "   氨基酸洁面 39元\n"
        "2. 在左侧「上传数据文件」传 CSV/JSON（表头可用 name,price,category,specs,detail,qualifications），我会解析并全部归入「产品展示区」。\n"
        "照片 / 规格 / 详情 / 资质也可以一起写在每行后面。"
    )
    result = {"reply": reply, "intent": "product_import"}
    _record_task(db, actor, message, result)
    return result


async def _handle_copy(db: Session, actor: Actor, message: str) -> dict:
    res = await _execute(db, actor, "generate_marketing_draft", {"prompt": message, "context": message}, message, 0)
    draft = res.result.get("draft") if (res.decision is Decision.ALLOWED and res.result) else ""
    error = res.result.get("error") if (res.decision is Decision.ALLOWED and isinstance(res.result, dict)) else ""
    if not draft and error:
        reply = f"文案生成失败：{error}"
    else:
        reply = f"已生成营销文案草稿：\n{draft}"
    result = {"reply": reply, "intent": "copy", "decision": res.decision.value, "draft": draft}
    _record_task(db, actor, message, result)
    return result


async def _handle_payment(db: Session, actor: Actor, message: str) -> dict:
    amount = 0.0
    match = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块|¥|￥)", message)
    if match:
        amount = float(match.group(1))
    res = await _execute(db, actor, "make_payment", {"amount": amount, "currency": "CNY"}, message, 0)
    result = {
        "reply": f"支付请求已提交（金额 ¥{amount:g}）。决策：{res.decision.value}",
        "intent": "payment",
        "decision": res.decision.value,
        "approval_id": res.approval_id,
        "result": res.result,
        "reason": res.reason,
    }
    _record_task(db, actor, message, result)
    return result


async def _handle_inventory(db: Session, actor: Actor, message: str) -> dict:
    res = await _execute(db, actor, "list_inventory", {}, message, 0)
    inventory = res.result.get("inventory") if (res.decision is Decision.ALLOWED and isinstance(res.result, dict)) else []
    lines = "\n".join(f"- {item['name']}：库存 {item['stock']}" for item in inventory) or "（暂无商品）"
    result = {"reply": f"当前库存：\n{lines}", "intent": "inventory", "inventory": inventory}
    _record_task(db, actor, message, result)
    return result


async def _handle_orders(db: Session, actor: Actor, message: str) -> dict:
    res = await _execute(db, actor, "list_orders", {}, message, 0)
    orders = res.result.get("orders") if (res.decision is Decision.ALLOWED and isinstance(res.result, dict)) else []
    lines = "\n".join(f"- {order['order_reference'] or order['id'][:8]}：{order['status']}（¥{order['total_amount']:g}）" for order in orders) or "（暂无订单）"
    result = {"reply": f"订单列表：\n{lines}", "intent": "orders", "orders": orders}
    _record_task(db, actor, message, result)
    return result


async def _handle_shipments(db: Session, actor: Actor, message: str) -> dict:
    res = await _execute(db, actor, "list_shipments", {}, message, 0)
    shipments = res.result.get("shipments") if (res.decision is Decision.ALLOWED and isinstance(res.result, dict)) else []
    lines = "\n".join(f"- {shipment['carrier'] or '物流'} {shipment['tracking_number']}：{shipment['status']}" for shipment in shipments) or "（暂无运单）"
    result = {"reply": f"运单列表：\n{lines}", "intent": "shipments", "shipments": shipments}
    _record_task(db, actor, message, result)
    return result


async def _handle_capabilities(db: Session, actor: Actor, message: str) -> dict:
    """Answer capability questions with the workspace's real, live skills."""
    from . import skills as skills_mod
    from .llm import workspace_model_config
    from .tools import registry

    installed_ids = skills_mod.installed_skill_ids(db, actor.workspace_id)
    catalog_by_id = {item["id"]: item for item in skills_mod.catalog()}
    skill_lines = []
    for skill_id in sorted(installed_ids):
        item = catalog_by_id.get(skill_id)
        if item:
            skill_lines.append(f"- {item['icon']} {item['title']}（{item['category']}，已安装）")
        else:
            skill_lines.append(f"- {skill_id}（已安装）")
    if not skill_lines:
        skill_lines = ["（尚未安装技能，可在技能区选择安装）"]

    live_functions = db.execute(
        select(WorkspaceFunction).where(
            WorkspaceFunction.workspace_id == actor.workspace_id,
            WorkspaceFunction.enabled.is_(True),
            WorkspaceFunction.stage == "live",
        )
    ).scalars().all()
    function_lines = [f"- {fn.name}（{fn.kind}）" for fn in live_functions] or ["（暂无已上线自定义功能）"]

    tool_lines = [f"- {spec.name}" for spec in registry.all()]

    config = workspace_model_config(db, actor.workspace_id)
    model_text = f"已配置模型 {config['model']}（{config['provider']}）" if config else "尚未配置模型，请在设置中保存模型 Key"

    reply = (
        "是的：只要是「已安装（live）」的技能和「已上线」的自定义功能，我都能通过统一网关调用；"
        "每次执行都会经过权限、地区合规、风险审批、验收与审计链路。测试功能区里尚未上线的功能我不会擅自执行。\n\n"
        f"【已安装技能】\n{chr(10).join(skill_lines)}\n\n"
        f"【已上线自定义功能】\n{chr(10).join(function_lines)}\n\n"
        f"【内置工具】\n{chr(10).join(tool_lines)}\n\n"
        f"【模型状态】{model_text}"
    )
    result = {"reply": reply, "intent": "capabilities", "skills": sorted(installed_ids), "functions": function_lines, "tools": tool_lines}
    _record_task(db, actor, message, result)
    return result


async def _handle_goal(db: Session, actor: Actor, message: str) -> dict:
    """Create a durable Muse-style goal and an owner-facing tracking todo."""
    title = message.strip()
    if len(title) > 60:
        title = title[:60] + "…"
    goal = Goal(workspace_id=actor.workspace_id, title=title, detail=message, status="active")
    db.add(goal)
    db.flush()
    todo = Todo(
        workspace_id=actor.workspace_id,
        kind="needs_reply",
        title=f"目标跟踪：{title}",
        detail="Agent 会在后台持续监控相关数据，只有出现进展、到期或需要你决策时才回来。",
        status="pending",
        ref_type="goal",
        ref_id=goal.id,
    )
    db.add(todo)
    db.commit()
    db.refresh(goal)
    result = {
        "reply": f"已建立持续跟踪目标「{title}」。我会在后台监控相关数据，只有出现进展、到期或需要你决策时才回来；也已在待办里留了一条跟踪项。",
        "intent": "goal",
        "actions": [{"tool": "create_goal", "params": {"title": title}, "decision": "ALLOWED", "result": {"goal_id": goal.id, "title": goal.title, "status": goal.status}}],
    }
    _record_task(db, actor, message, result)
    return result


async def _handle_business_onboarding(db: Session, actor: Actor, message: str) -> dict:
    """Persist a merchant profile from an identity statement and answer with a domain playbook."""
    category = _business_category(message)
    shop_name = _shop_name(message)
    workspace = db.get(Workspace, actor.workspace_id)
    if workspace is not None:
        workspace.business_category = category
    profile = db.execute(
        select(MerchantProfile).where(MerchantProfile.workspace_id == actor.workspace_id)
    ).scalars().first()
    if profile is None:
        profile = MerchantProfile(
            workspace_id=actor.workspace_id,
            shop_name=shop_name,
            category=category,
            region=workspace.region if workspace else "CN",
            currency="CNY",
        )
        db.add(profile)
    else:
        if shop_name != "我的店":
            profile.shop_name = shop_name
        profile.category = category
    db.flush()

    from . import skills as skills_mod

    skill_titles = {item["id"]: item["title"] for item in skills_mod.catalog()}
    pack = SCENARIO_PACKS.get(category) or SCENARIO_PACKS["general"]
    installed_ids = skills_mod.installed_skill_ids(db, actor.workspace_id)
    newly: list[str] = []
    for skill_id in pack:
        if skill_id in installed_ids:
            continue
        try:
            skills_mod.install_skill(db, actor.workspace_id, skill_id)
            newly.append(skill_id)
            installed_ids.add(skill_id)
        except Exception:  # noqa: BLE001 - one skill failing must not block onboarding
            continue
    db.flush()

    playbook = CATEGORY_PLAYBOOK.get(category) or CATEGORY_PLAYBOOK["general"]
    pack_lines = "\n".join(f"- {skill_titles.get(sid, sid)}" for sid in pack)
    installed_text = f"本次新装 {len(newly)} 项" if newly else "已就绪"
    reply = (
        f"好的，已把你的店铺登记为「{profile.shop_name}」（品类 {category}），并为该场景装好市场验证技能（{installed_text}）。\n\n"
        f"【已装技能】\n{pack_lines}\n\n"
        f"【能干什么】\n{playbook}\n\n"
        f"不想要的技能可在「技能区」一键卸载；现在直接给我第一件事，例如：「把菜单发我」或「帮我接一单」。"
    )
    result = {"reply": reply, "intent": "business_onboarding", "category": category, "shop_name": profile.shop_name, "skills_installed": newly}
    _record_task(db, actor, message, result)
    return result


BUILTIN_AREA_TYPES = {
    "库存": ("tool", "inventory"),
    "订单": ("tool", "orders"),
    "客户": ("tool", "customers"),
    "运单": ("tool", "shipments"),
    "物流": ("tool", "shipments"),
    "话术": ("tool", "scripts"),
    "收款": ("connector", "karma_payment"),
    "支付": ("connector", "karma_payment"),
}


def _feature_name(message: str) -> str:
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9·\-]{1,24})\s*功能", message)
    if m:
        name = m.group(1)
        lead = ("帮我", "给我", "请", "开发", "搭建", "创建", "新增", "添加", "做一个", "加一个", "建一个", "上线", "做", "加", "建", "一个", "这个", "新的", "个")
        changed = True
        while changed:
            changed = False
            for word in lead:
                if name.startswith(word):
                    name = name[len(word):]
                    changed = True
                    break
        return name.strip() or "新功能"
    tail = re.sub(r"(请|帮我|给我|开发|做一个|添加|创建|新增|建|加|一个|功能)", "", message)
    tail = tail.strip(" 的了，。")
    return tail[:24] or "新功能"


async def _design_function(db: Session, actor: Actor, message: str) -> dict:
    """Use the configured model to turn a one-line request into a function spec."""
    import hashlib

    from .llm import generate, workspace_model_config
    from .tools import registry

    display_name = _feature_name(message)
    fallback = {
        "name": "fn_" + hashlib.md5(display_name.encode("utf-8")).hexdigest()[:12],
        "display_name": display_name,
        "description": display_name,
        "category": "general",
        "kind": "agent",
        "config": {"goal": display_name, "instructions": "", "acceptance": "", "allowed_tools": []},
        "input_schema": {},
    }
    model_config = workspace_model_config(db, actor.workspace_id)
    if not model_config:
        return fallback
    tool_names = ", ".join(t.name for t in registry.all())
    prompt = (
        "你是商业功能设计师。用户一句话需求：" + message + "\n"
        "请只输出一个 JSON 对象，不要解释或 markdown：\n"
        '{"name":"英文工具名(小写字母开头可含下划线)","display_name":"中文功能名","description":"一句话描述",'
        '"category":"分类","kind":"agent","config":{"goal":"功能目标","instructions":"商业执行标准(逐条,含触发条件/步骤/报价话术规则/数据写回/禁止事项)",'
        '"acceptance":"验收标准","allowed_tools":["允许调用的工具名"]},"input_schema":{}}\n'
        "allowed_tools 只能从这些工具里选：" + tool_names + "\n"
    )
    try:
        raw = generate(prompt=prompt, context=message, model_config=model_config).strip()
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start != -1 and end > start else {}
    except Exception:  # noqa: BLE001 - fall back to defaults
        data = {}
    if not isinstance(data, dict):
        data = {}
    name = data.get("name") or fallback["name"]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$", name or ""):
        name = fallback["name"]
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    return {
        "name": name,
        "display_name": data.get("display_name") or display_name,
        "description": data.get("description") or display_name,
        "category": data.get("category") or "general",
        "kind": data.get("kind") or "agent",
        "config": {
            "goal": config.get("goal") or display_name,
            "instructions": config.get("instructions") or "",
            "acceptance": config.get("acceptance") or "",
            "allowed_tools": config.get("allowed_tools") or [],
        },
        "input_schema": data.get("input_schema") if isinstance(data.get("input_schema"), dict) else {},
    }


async def _handle_develop_feature(db: Session, actor: Actor, message: str) -> dict:
    """One-line request -> a real, executable function in the test area."""
    feature = _feature_name(message)
    is_domain = lambda keyword: keyword in feature and ("管理" in feature or "展示" in feature or len(feature) <= 4)
    if any(is_domain(k) for k in ("产品", "商品", "展示")):
        return await _handle_product_display(db, actor, message)

    for keyword, (kind, target_type) in BUILTIN_AREA_TYPES.items():
        if is_domain(keyword):
            name = f"{keyword}管理"
            res = await _execute(db, actor, "add_layout_area", {"name": name, "kind": kind, "target_type": target_type, "stage": "live"}, message, 0)
            result = {
                "reply": f"已把「{name}」作为内置功能添加到功能区（Agent 已具备该能力，可直接使用）。",
                "intent": "develop_feature",
                "actions": [{"tool": "add_layout_area", "name": name, "decision": res.decision.value, "result": res.result}],
                "plan": ["add_layout_area"],
            }
            _record_task(db, actor, message, result)
            return result

    spec = await _design_function(db, actor, message)
    res = await _execute(db, actor, "create_function", spec, message, 0)
    actions = [{"tool": "create_function", "name": spec["name"], "decision": res.decision.value, "result": res.result}]
    if res.decision is Decision.ALLOWED and res.result and res.result.get("function_id"):
        acceptance = spec["config"].get("acceptance") or "（未提供）"
        reply = (
            f"已在测试功能区生成功能「{spec['display_name']}」。\n"
            f"目标：{spec['config'].get('goal')}\n"
            f"验收标准：{acceptance}\n"
            "测试：说「测试/运行这个功能」；测试通过后说「上线」即可加入主功能区，Agent 会自动读取并联动。"
        )
    else:
        reply = f"功能创建未完成：{res.result or res.reason or '未知错误'}"
    result = {"reply": reply, "intent": "develop_feature", "spec": spec, "actions": actions, "plan": ["create_function"]}
    _record_task(db, actor, message, result)
    return result


HELP_REPLY = (
    "我可以帮你（所有操作都会经过权限与安全链路）：\n"
    "- 建功能区并归类商品：如「在产品展示区加入 棉麻衬衫 99元、牛仔裤 159元」\n"
    "- 在指定功能区下加子功能：如「在测试功能区给我加上一个产品展示区」\n"
    "- 生成营销文案：如「写一段棉麻衬衫的营销文案」\n"
    "- 发起 Karma 支付：如「支付 10 元」\n"
    "- 查看库存：如「看看库存」\n"
    "- 查看订单：如「订单列表」\n"
    "- 查看运单：如「发货单」"
)


_PARAM_HINTS = {
    "list_products": {},
    "workspace_summary": {},
    "create_product_draft": {"name": "", "price": 0, "category": "", "photo_url": "", "specs": {}, "detail": "", "qualifications": [], "layout_area_id": None},
    "update_product": {"product_id": "", "name": "", "price": 0, "category": "", "photo_url": "", "specs": {}, "detail": "", "qualifications": [], "layout_area_id": None},
    "generate_marketing_draft": {"prompt": "", "context": ""},
    "publish_product": {"product_id": ""},
    "create_order": {"channel": "ecommerce_independent", "items": []},
    "list_orders": {},
    "list_inventory": {},
    "list_shipments": {},
    "add_stock": {"product_id": "", "quantity": 0},
    "fulfill_order": {"order_id": "", "carrier": "", "tracking_number": ""},
    "mark_delivered": {"shipment_id": "", "order_id": ""},
    "cancel_order": {"channel": "ecommerce_independent", "order_reference": ""},
    "bulk_update_prices": {"delta": 0},
    "make_payment": {"amount": 0, "currency": "CNY"},
    "make_refund": {"provider_reference": "", "amount": 0, "reason": ""},
    "reconcile_payment": {"provider_reference": ""},
    "dispute_payment": {"provider_reference": "", "reason": ""},
    "add_layout_area": {"name": "", "kind": "module", "target_type": "", "parent_id": None, "stage": "test"},
    "list_layout_areas": {},
    "link_products_to_area": {"product_ids": [], "layout_area_id": ""},
    "create_function": {"name": "", "display_name": "", "description": "", "category": "general", "kind": "agent", "config": {"goal": "", "instructions": "", "acceptance": "", "allowed_tools": []}, "input_schema": {}, "parent_area_id": None},
    "run_function": {"function_id": "", "name": "", "params": {}},
    "promote_function": {"name": "", "function_id": ""},
    "run_code": {"code": "", "inputs": {}, "timeout": 5},
    "read_data": {"file_name": ""},
    "write_report": {"file_name": "", "content": ""},
    "summarize_data": {"file_name": "", "group_by": "", "value_col": "", "agg": "sum", "output_file": None},
    "list_customers": {},
    "get_customer": {"customer_id": "", "channel": "chat", "external_id": ""},
    "business_overview": {},
    "list_team_roles": {},
    "list_skills": {"query": ""},
    "install_skill": {"skill_id": ""},
    "get_channel_receipt": {"channel": "", "params": {}},
    "list_channel_connectors": {},
    "update_customer": {"customer_id": "", "channel": "chat", "external_id": "", "name": "", "stage": "", "profile": {}},
    "promote_area": {"area_id": "", "area_name": ""},
    "record_script": {"title": "", "content": "", "tags": [], "wins": 0, "sends": 1},
    "record_win": {"customer_id": "", "script_id": "", "title": "", "content": ""},
    "optimize_scripts": {},
    "upsert_customer": {"channel": "chat", "external_id": "", "name": "", "profile": {}},
    "quote_product": {"product_id": "", "product_name": "", "customer_id": "", "quantity": 1},
    "introduce_product": {"product_id": "", "product_name": ""},
    "reply_customer": {"customer_id": "", "text": ""},
    "send_chat_message": {"channel": "chat", "external_id": "", "text": ""},
    "create_todo": {"kind": "info", "title": "", "detail": ""},
    "list_todos": {"kind": "", "status": "pending"},
    "list_goals": {},
    "create_goal": {"title": "", "detail": "", "success_criteria": "", "progress_note": "", "due_at": None},
    "update_goal": {"goal_id": "", "status": "", "progress_note": "", "detail": "", "success_criteria": ""},
    "get_memory": {"key": ""},
    "set_memory": {"key": "", "value": "", "kind": "preference"},
    "import_products": {"items": [], "text": "", "file_name": "", "area_name": "产品展示区"},
    "get_commerce_policy": {},
    "set_commerce_policy": {"auto_accept_orders": False, "max_discount_percent": 0, "approval_threshold_amount": 0, "currency": "CNY", "shipping_fee": 0, "payment_method": "karma", "terms": ""},
}


def _business_context(db: Session, actor: Actor) -> str:
    """Compact workspace snapshot the planner can use to ground decisions."""
    from .models import AgentMemory, CommercePolicy, Customer, Goal, MerchantProfile, Order, Product, Script, Shipment, Todo, WorkspaceFunction

    products = db.execute(
        select(Product).where(Product.workspace_id == actor.workspace_id).order_by(Product.created_at).limit(30)
    ).scalars().all()
    product_lines = [f"- {p.name} | id={p.id} | 价 ¥{p.price:g} | 库存 {p.stock} | {p.status} | 区 {p.layout_area_id or ''}" for p in products]
    customers = db.execute(
        select(Customer).where(Customer.workspace_id == actor.workspace_id).order_by(Customer.updated_at.desc()).limit(20)
    ).scalars().all()
    customer_lines = [f"- {c.name or '未命名'} | id={c.id} | {c.channel}:{c.external_id} | 阶段 {c.stage}" for c in customers]
    areas = db.execute(
        select(LayoutArea).where(LayoutArea.workspace_id == actor.workspace_id).order_by(LayoutArea.sort_order)
    ).scalars().all()
    area_lines = [f"- {a.name} | id={a.id} | {a.target_type or a.kind} | {'已上线' if a.stage == 'live' else '测试中'}" for a in areas] or ["（暂无功能区）"]
    scripts = db.execute(
        select(Script).where(Script.workspace_id == actor.workspace_id).order_by(Script.wins.desc()).limit(5)
    ).scalars().all()
    script_lines = [f"- [{s.wins}胜/{s.sends}发] {s.title}：{s.content[:120]}" for s in scripts] or ["（暂无话术样本）"]
    functions = db.execute(
        select(WorkspaceFunction).where(WorkspaceFunction.workspace_id == actor.workspace_id, WorkspaceFunction.enabled.is_(True))
    ).scalars().all()
    function_lines = [
        f"- {fn.name}（{(fn.config or {}).get('display_name') or fn.name}）| id={fn.id} | {fn.kind} | {'已上线' if fn.stage == 'live' else '测试中'}"
        for fn in functions
    ] or ["（暂无自定义功能）"]
    merchant = db.execute(
        select(MerchantProfile).where(MerchantProfile.workspace_id == actor.workspace_id)
    ).scalars().first()
    merchant_lines = (
        [f"- {merchant.shop_name} | 品类 {merchant.category} | {merchant.region} {merchant.currency}"]
        if merchant else ["（未设置商家画像）"]
    )
    workspace = db.get(Workspace, actor.workspace_id)
    category = (merchant.category if merchant else "") or (workspace.business_category if workspace else "general") or "general"
    hint = CATEGORY_HINTS.get(category) or CATEGORY_HINTS["general"]
    todos = db.execute(
        select(Todo).where(Todo.workspace_id == actor.workspace_id, Todo.status == "pending").order_by(Todo.created_at.desc()).limit(10)
    ).scalars().all()
    todo_lines = [
        f"- [{t.kind}] {t.title}" + (f"（确认码已邮件发送给主人）" if t.kind == "must_confirm" else "")
        for t in todos
    ] or ["（暂无待办）"]
    from .memory_layers import build_memory_block

    memory_block = build_memory_block(db, actor.workspace_id)
    memory_lines = memory_block.splitlines() or ["（暂无长期记忆）"]
    goals = db.execute(
        select(Goal).where(Goal.workspace_id == actor.workspace_id, Goal.status == "active").order_by(Goal.created_at.desc())
    ).scalars().all()
    goal_lines = [
        f"- {g.title}" + (f"（进度：{g.progress_note}）" if g.progress_note else "")
        for g in goals
    ] or ["（暂无进行中的目标）"]
    orders = db.execute(
        select(Order).where(Order.workspace_id == actor.workspace_id).order_by(Order.created_at.desc()).limit(20)
    ).scalars().all()
    order_lines = [
        f"- {o.order_reference or o.id[:8]} | id={o.id} | {o.status} | ¥{o.total_amount:g} | 客户 {o.customer.get('name') if isinstance(o.customer, dict) else ''}"
        for o in orders
    ] or ["（暂无订单）"]
    shipments = db.execute(
        select(Shipment).where(Shipment.workspace_id == actor.workspace_id).order_by(Shipment.created_at.desc()).limit(20)
    ).scalars().all()
    shipment_lines = [
        f"- 运单 id={s.id} | 订单 {s.order_id} | {s.carrier} {s.tracking_number} | {s.status}"
        for s in shipments
    ] or ["（暂无运单）"]
    policy = db.execute(
        select(CommercePolicy).where(CommercePolicy.workspace_id == actor.workspace_id)
    ).scalars().first()
    policy_lines = (
        [
            f"- 自动接单={'开' if policy.auto_accept_orders else '关'} | 最大折扣 {policy.max_discount_percent:g}% | "
            f"订单金额超 ¥{policy.approval_threshold_amount:g} 需审批 | 币种 {policy.currency} | 运费 ¥{policy.shipping_fee:g} | 收款 {policy.payment_method}"
            + (f" | 条款 {policy.terms[:80]}" if policy.terms else "")
        ]
        if policy
        else ["（未设置商务政策，默认交易动作都需主人确认）"]
    )
    return (
        "商家画像：\n" + "\n".join(merchant_lines) + "\n"
        f"履约提示：{hint}\n"
        "商务政策（报价/谈判/接单必须遵守的底线）：\n" + "\n".join(policy_lines) + "\n"
        "Agent 长期记忆（主人偏好/事实，优先遵循）：\n" + "\n".join(memory_lines) + "\n"
        "进行中的目标（持续跟踪，有变化才回报）：\n" + "\n".join(goal_lines) + "\n"
        "功能区（含已上线/测试中）：\n" + "\n".join(area_lines) + "\n"
        "商品目录（含库存/售价）：\n" + ("\n".join(product_lines) if product_lines else "（空）") + "\n"
        "客户（阶段 new=新增 lead=意向 won=成交 lost=流失）：\n" + ("\n".join(customer_lines) if customer_lines else "（空）") + "\n"
        "订单（CREATED/PAID/SHIPPED/DELIVERED）：\n" + "\n".join(order_lines) + "\n"
        "运单：\n" + "\n".join(shipment_lines) + "\n"
        "成交话术（按成交次数排序）：\n" + "\n".join(script_lines) + "\n"
        "自定义功能（上线后 Agent 自动读取）：\n" + "\n".join(function_lines) + "\n"
        "主人待办（pending，确认码只邮件发给主人，Agent 不得代确认）：\n" + "\n".join(todo_lines)
    )


def build_customer_memory(db: Session, workspace_id: str, channel: str, external_id: str, limit: int = 8) -> str:
    """Compact recent history for one customer, formatted for a planner prompt."""
    from .models import Conversation, Customer, Message

    customer = db.execute(
        select(Customer).where(
            Customer.workspace_id == workspace_id,
            Customer.channel == channel,
            Customer.external_id == external_id,
        )
    ).scalars().first()
    if customer is None:
        return ""
    conversation = db.execute(
        select(Conversation).where(Conversation.customer_id == customer.id).order_by(Conversation.updated_at.desc())
    ).scalars().first()
    if conversation is None:
        return f"客户 {customer.name or external_id}（首次对话）"
    messages = db.execute(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).limit(limit)
    ).scalars().all()
    lines = [f"{'客户' if m.direction == 'in' else 'Agent'}: {m.content}" for m in reversed(messages)]
    return "\n".join(lines)


def _tool_catalog(db: Session, actor: Actor) -> str:
    from .tools import registry

    lines = []
    for tool in registry.all():
        lines.append(f"- {tool.name}: {tool.description}; 参数示例 {json.dumps(_PARAM_HINTS.get(tool.name, {}), ensure_ascii=False)}")
    fns = db.execute(
        select(WorkspaceFunction).where(WorkspaceFunction.workspace_id == actor.workspace_id, WorkspaceFunction.enabled.is_(True))
    ).scalars().all()
    for fn in fns:
        stage_tag = "" if fn.stage == "live" else "（测试中，不可自动调用，只能用 run_function 显式测试）"
        lines.append(f"- {fn.name}: {fn.description or ('用户功能 ' + fn.kind)}; kind={fn.kind}; stage={fn.stage}{stage_tag}")
    return "\n".join(lines)


def _workspace_agents(db: Session, actor: Actor) -> list[dict]:
    """Owner-configured AI managers eligible for orchestration (live/test)."""
    from .models import AIAgent

    rows = db.execute(
        select(AIAgent).where(
            AIAgent.workspace_id == actor.workspace_id,
            AIAgent.status.in_(["live", "test"]),
        ).order_by(AIAgent.updated_at.desc())
    ).scalars().all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "description": a.description or "",
            "responsibilities": a.responsibilities or [],
            "allowed_tools": a.allowed_tools or [],
            "status": a.status,
        }
        for a in rows
    ]


def _roles_context(db: Session, actor: Actor) -> str:
    """Polsia-style AI team plus owner-configured managers."""
    from .roles import catalog

    lines = [
        f"- {r['icon']}{r['name']}（{r['id']}）：{r['mission']}；常用工具 {', '.join(r['tools'])}"
        for r in catalog()
    ]
    agents = _workspace_agents(db, actor)
    if agents:
        lines.append("工作区自定义经理人（live/test，参与编排，工具仍经统一网关）：")
        for a in agents:
            tools = ", ".join(a["allowed_tools"]) or "（未配白名单，沿用角色默认工具）"
            lines.append(
                f"- 🤖 {a['name']}（agent_id={a['id']}，role={a['role']}，status={a['status']}）："
                f"{a['description'] or a['role']}；允许工具 {tools}"
            )
    return "当前 AI 团队（Polsia 模式：按任务选角色视角，但所有动作仍走统一安全网关）：\n" + "\n".join(lines) + "\n"


_PLANNER_SYSTEM = '''你是「商家 Agent」的规划器（proposer），不是执行器。
职责：先理解主人或客户真正要什么，再决定要不要做、怎么做；你只提出工具步骤建议，
是否允许执行由统一安全网关（decider）决定。你永远不能绕过审批、不能代主人输入确认码、
不能擅自执行资金或不可逆动作。只输出一个 JSON 对象，不要输出任何解释、markdown 或代码块。'''

_PLANNER_CONTRACT = '''JSON 键名固定：intent、steps、reply、done、clarify。
形如 {"intent":"...","steps":[{"tool":"工具名","params":{...}}],"reply":"给用户的一句中文确认","done":false}
- 纯聊天/无需工具：{"intent":"chat","steps":[],"reply":"...","done":true}
- 信息不足、无法安全执行：只输出 {"clarify":"需要用户补充的一个最关键问题"}
- 多步任务：每步一个 tool，按依赖顺序排列；本步不是最后一步时 done=false 且可省略 reply；全部完成后 done=true 并写 reply。
- 严格使用「可用工具」列表里的工具名与参数键；拿不到的值留空，禁止臆造。'''

_PLANNER_FEW_SHOT = '''示例（用户：在产品展示区加入 棉麻衬衫 99元）：
{"intent":"product_display","steps":[{"tool":"add_layout_area","params":{"name":"产品展示区","kind":"module","target_type":"product_display","stage":"test"}},{"tool":"create_product_draft","params":{"name":"棉麻衬衫","price":99}}],"reply":"已把棉麻衬衫 99元 归类到产品展示区","done":true}
说明：已建区后，后续商品步骤可省略 layout_area_id（系统自动归类到上一步的区）。'''

_PLANNER_RULES = '''执行规则：
1) 只使用列出的工具名与参数键；金额/数量从指令提取，缺失就留空。
2) 商品/归类：先看「当前业务上下文」是否已有同名功能区，有就复用其 id；没有才 add_layout_area 建区（默认 stage="test"）。建商品用 create_product_draft（照片放 photo_url、规格放 specs 对象、详情放 detail、资质放 qualifications 数组）；已有商品用 update_product 补全。
3) 批量导入/用户把产品丢给你：优先 import_products（items 数组、text 粘贴多行「名称 价格」、或 file_name 上传文件），它会自动归类。
4) 文案 generate_marketing_draft；报价 quote_product；介绍 introduce_product；回复客户 reply_customer；主动外发 send_chat_message。
5) 开发/做新功能：create_function（kind 默认 agent，config 必须含 goal/instructions/acceptance），自动进测试功能区；测试用 run_function 实际运行；测试通过用户说上线，用 promote_function 上线；功能区上线用 promote_area。
6) 客户阶段 update_customer（new=新增 lead=意向 won=成交 lost=流失）；成交后 record_win 标记脚本胜利。
7) 数据分析 summarize_data（读取+分组聚合+可选写报告）；复杂计算 run_code（沙箱，只允许纯 Python + 安全标准库，变量 data/inputs，print(json.dumps(结果)) 输出）；read_data 读文件、write_report 写报告。
8) 待办/确认：资金、不可逆、重要动作一律走审批，绝不代主人确认；需要主人决策用 create_todo(kind="needs_reply")；完成通知用 create_todo(kind="info")；must_confirm 的确认码只邮件发给主人，Agent 永远不要输出或输入确认码。
9) 目标/记忆：跟踪用 create_goal（title 必填，可带 detail/success_criteria）；更新用 update_goal；偏好/事实用 set_memory（key 简短英文，value 中文）；读取用 get_memory。
10) 商务政策：报价/谈判前先 get_commerce_policy；折扣不超过 max_discount_percent；金额超 approval_threshold_amount 必须审批；auto_accept_orders 为 true 且未超阈值可 create_order 自动接单，否则 create_todo(kind="must_confirm") 请主人确认。
11) 团队角色：按任务选角色视角（店长/上架员/客服/运营/履约员/财务），工具选择不受角色限制；需要时 list_team_roles。
12) 落地优先：主人问「怎么做/管理/合规/授权/优化/设置」时，把结论落到系统——偏好 set_memory、持续跟踪 create_goal、新能力 create_function、需要拍板 create_todo(kind="needs_reply")；只有纯观点/闲聊才直接回文字。
13) 技能复用：先 list_skills；有语义匹配就 install_skill 安装对应 skill_id，禁止重复 create_function；确实没有才 create_function。'''


def _escalation_brief() -> str:
    from . import nervous

    return nervous.escalation_brief()


def _learning_brief(db: Session, workspace_id: str) -> str:
    """本店已采纳的执行经验注入（轨迹自学习闭环）。"""
    from .learning import brief

    return brief(db, workspace_id, max_items=4)


def _rulepack_brief() -> str:
    """激活规则包注入（.yaml 即插即用）。"""
    from .rulepack import brief

    return brief()


def _call_model(db: Session, actor: Actor, message: str) -> str | None:
    """Ask the configured workspace model to produce a JSON plan, if available."""
    from .llm import chat, workspace_model_config

    config = workspace_model_config(db, actor.workspace_id)
    if not config:
        return None
    source_note = (
        "这是客户发来的消息：先理解客户真正需要什么（询价/下单/售后/砍价/闲聊），"
        "能成交就引导成交，信息不足只问一个最关键的问题。"
        if actor.source == "customer"
        else ""
    )
    from .context_assembler import assemble_relevant_facts, format_facts
    relevant_facts = format_facts(
        assemble_relevant_facts(db, actor.workspace_id, message, k=5)
    )
    prompt = "\n".join(
        part
        for part in (
            source_note,
            _PLANNER_CONTRACT,
            _PLANNER_FEW_SHOT,
            _roles_context(db, actor),
            relevant_facts,
            "当前业务上下文：\n" + _business_context(db, actor),
            "可用工具及参数：\n" + _tool_catalog(db, actor),
            _PLANNER_RULES,
            _escalation_brief(),
            _learning_brief(db, actor.workspace_id),
            _rulepack_brief(),
            "用户指令：" + message,
        )
        if part
    )
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        return chat(messages, model_config=config, temperature=0.2, max_tokens=1600, json_mode=True)
    except Exception:  # noqa: BLE001 - retry without JSON mode
        try:
            return chat(messages, model_config=config, temperature=0.2, max_tokens=1600, json_mode=False)
        except Exception:  # noqa: BLE001 - model planning is best-effort
            return None



def _parse_plan(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        return None
    return data


def _parse_agent_plan(raw: str) -> dict | None:
    """Parse a planner JSON that may also be a clarify or final-only answer."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("steps"), list) or data.get("clarify") or data.get("reply"):
        return data
    return None


MAX_AGENT_ITERATIONS = 8


def _allowed_tools(db: Session, actor: Actor) -> set[str]:
    from .tools import registry

    allowed = {tool.name for tool in registry.all()}
    fns = db.execute(
        select(WorkspaceFunction).where(WorkspaceFunction.workspace_id == actor.workspace_id, WorkspaceFunction.enabled.is_(True))
    ).scalars().all()
    allowed |= {fn.name for fn in fns if fn.stage == "live"}
    return allowed


_PARAM_CARRY = {
    "layout_area_id": "last_area_id",
    "product_id": "last_product_id",
    "order_id": "last_order_id",
    "customer_id": "last_customer_id",
    "shipment_id": "last_shipment_id",
    "provider_reference": "last_provider_reference",
}

_RESULT_CARRY = {
    "area_id": "last_area_id",
    "product_id": "last_product_id",
    "order_id": "last_order_id",
    "customer_id": "last_customer_id",
    "shipment_id": "last_shipment_id",
    "provider_reference": "last_provider_reference",
}


async def _execute_plan_steps(db: Session, actor: Actor, message: str, steps: list[dict], carry: dict) -> list[dict]:
    allowed = _allowed_tools(db, actor)
    actions: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            actions.append({"tool": "", "params": {}, "decision": "DENIED", "result": {"error": "invalid step shape"}})
            continue
        tool = step.get("tool")
        params = step.get("params") or {}
        if not tool or tool not in allowed:
            actions.append({"tool": tool or "", "params": params, "decision": "DENIED", "result": {"error": f"unknown or not-allowed tool: {tool}"}})
            continue
        # Model-output injection guard: a proposed step whose tool/params carry
        # an override/hijack/direct-tool-injection pattern is rejected before it
        # reaches the gateway. Read-only/clean params never match BLOCK rules.
        from .guard import BLOCK, analyze_injection

        scan = analyze_injection(
            json.dumps({"tool": tool, "params": params}, ensure_ascii=False),
            check_json_call=False,
        )
        if scan["severity"] == BLOCK:
            actions.append({
                "tool": tool,
                "params": params,
                "decision": "DENIED",
                "result": {"error": f"blocked by injection guard: {scan['label']}"},
            })
            continue
        if not isinstance(params, dict):
            actions.append({"tool": tool, "params": params, "decision": "DENIED", "result": {"error": "params must be a JSON object"}})
            continue
        for param_key, carry_key in _PARAM_CARRY.items():
            if not params.get(param_key) and carry.get(carry_key):
                params[param_key] = carry[carry_key]
        res = await _execute(db, actor, tool, params, message, carry["step_index"])
        carry["step_index"] += 1
        if res.decision is Decision.ALLOWED and isinstance(res.result, dict):
            for result_key, carry_key in _RESULT_CARRY.items():
                if res.result.get(result_key):
                    carry[carry_key] = res.result[result_key]
        from . import nervous

        actions.append({
            "tool": tool,
            "params": params,
            "decision": res.decision.value,
            "result": res.result,
            **nervous.dispatch_meta(tool),
        })
    return actions


def _finalize(db: Session, actor: Actor, message: str, reply: str, intent: str, actions: list[dict], iterations: int) -> dict:
    result = {
        "reply": reply,
        "intent": intent,
        "plan_source": "model",
        "actions": actions,
        "plan": [a["tool"] for a in actions],
        "iterations": iterations,
    }
    _record_task(db, actor, message, result)
    return result


def _natural_reply(raw: str) -> str | None:
    """Treat a non-JSON model answer as a conversational reply (questions/status)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if "{" in text or "}" in text:
        return None
    return text or None


async def _run_agent_loop(db: Session, actor: Actor, message: str, memory: str = "") -> dict | None:
    """Iteratively plan -> execute -> feed results back until the model finishes.

    Returns None when no workspace model is configured or the first plan is
    unparseable, so the deterministic parser can take over. The model only
    proposes tools; every mutation still flows through the unified gateway.
    """
    carry: dict = {
        "last_area_id": None,
        "last_product_id": None,
        "last_order_id": None,
        "last_customer_id": None,
        "last_shipment_id": None,
        "last_provider_reference": None,
        "step_index": 0,
    }
    actions: list[dict] = []
    feedback: list[str] = []
    iterations = 0
    json_retry_done = False

    for _ in range(MAX_AGENT_ITERATIONS):
        iterations += 1
        effective = message
        if memory:
            effective = message + "\n\n【客户历史对话】\n" + memory
        if feedback:
            from .context_assembler import trim_feedback

            effective = (
                effective
                + "\n\n【已执行步骤与结果】\n"
                + "\n".join(trim_feedback(feedback))
            )
        raw = _call_model(db, actor, effective)
        plan = _parse_agent_plan(raw) if raw else None
        if plan is not None:
            from .plan import lint_known_tools, lint_params, validate_agent_plan

            ok, plan_errors, plan = validate_agent_plan(plan)
            if ok:
                from .tools import registry

                known = {t.name for t in registry.all()}
                known |= {
                    fn.name
                    for fn in db.execute(
                        select(WorkspaceFunction).where(
                            WorkspaceFunction.workspace_id == actor.workspace_id,
                            WorkspaceFunction.enabled.is_(True),
                        )
                    ).scalars().all()
                }
                allowed_params = {
                    tool: set(hints.keys()) for tool, hints in _PARAM_HINTS.items()
                }
                lint_errors = lint_known_tools(plan["steps"], known)
                lint_errors += lint_params(plan["steps"], allowed_params)
                if lint_errors:
                    plan_errors = lint_errors
                    ok = False
            if not ok:
                feedback.append("- 计划校验失败：" + "；".join(plan_errors) + "。请只输出合法 JSON：{\"intent\":\"...\",\"steps\":[{\"tool\":\"...\",\"params\":{}}],\"reply\":\"...\",\"done\":true}")
                plan = None
        if plan is None:
            if iterations == 1:
                prose = _natural_reply(raw) if raw else None
                if prose:
                    return _finalize(db, actor, message, prose, "model", actions, iterations)
                if raw and ("{" in raw or "tool" in raw or "steps" in raw):
                    json_retry_done = True
                    feedback.append("- 上次输出不是合法 JSON，请严格只输出一个 JSON 对象（不要 markdown、不要解释）。")
                    continue
                return None
            if json_retry_done and iterations == 2:
                return None
            break

        clarify = plan.get("clarify")
        if clarify:
            return _finalize(db, actor, message, str(clarify), "clarify", actions, iterations)

        steps = plan.get("steps") or []
        failed = False
        if steps:
            batch = await _execute_plan_steps(db, actor, message, steps, carry)
            actions.extend(batch)
            for action in batch:
                result_json = json.dumps(action["result"], ensure_ascii=False)[:1200]
                decision = action.get("decision") or ""
                result = action.get("result") if isinstance(action.get("result"), dict) else {}
                retryable = (
                    decision not in ("ALLOWED", "PENDING")
                    or bool(result.get("error"))
                    or result.get("receipt_status") == "UNKNOWN"
                )
                if retryable:
                    failed = True
                    feedback.append(f"- {action['tool']}: 失败/被拒 decision={decision} result={result_json}；请修正工具名或参数后重试。")
                else:
                    feedback.append(f"- {action['tool']}: decision={decision} result={result_json}")

        done = plan.get("done") in (True, "true", "True", 1)
        reply = plan.get("reply")
        if not steps or ((done or reply) and not failed):
            final_reply = reply or "已完成。"
            return _finalize(db, actor, message, final_reply, plan.get("intent") or "model", actions, iterations)

    return _finalize(db, actor, message, "已完成部分步骤。", "model", actions, iterations)


def _coordinate(db: Session, actor: Actor, message: str, reply: str) -> str:
    """When a workspace model is configured, let it coordinate/confirm the reply.

    The model never executes actions itself; the deterministic parser plus the
    gateway remain the authority. On any model error we fall back to the
    deterministic reply so the chain always stays complete.
    """
    from .llm import chat, workspace_model_config

    config = workspace_model_config(db, actor.workspace_id)
    if not config:
        return reply
    try:
        guidance = chat(
            [
                {"role": "system", "content": "你是商家 Agent 协调器。用一句简短中文向主人确认结果，并说明可继续的下一步。"},
                {"role": "user", "content": "用户指令：" + message + "\n已安全完成的动作摘要：" + reply},
            ],
            model_config=config,
            temperature=0.4,
            max_tokens=200,
        ).strip()
        if guidance:
            return reply + "\n\n（模型协调）" + guidance
    except Exception:  # noqa: BLE001 - model coordination is best-effort
        return reply
    return reply


async def _maybe_model_drive(db: Session, actor: Actor, message: str, fallback: dict, memory: str = "") -> dict:
    """Let the configured model actually reason and act when it is available.

    Deterministic handlers still register the shop/skills (their side effects
    persist). When a workspace model is configured the real agent loop takes
    over so the reply is grounded and action-oriented instead of a static list.
    Without a model we keep the deterministic reply and only coordinate it.
    """
    from .llm import workspace_model_config

    if not workspace_model_config(db, actor.workspace_id):
        fallback["reply"] = _coordinate(db, actor, message, fallback.get("reply", ""))
        fallback.setdefault("model_used", "本地规则（未接大模型）")
        fallback["reply"] += "\n\n（当前未接入大模型，以上为内置规则响应；在「设置→模型接入」配好 Key 后，我才能真正理解、推理并规划复杂任务。）"
        return fallback
    model_result = await _run_agent_loop(db, actor, message, memory)
    if model_result is None:
        fallback["reply"] = _coordinate(db, actor, message, fallback.get("reply", ""))
        return fallback
    for key in ("category", "shop_name", "skills_installed"):
        if key in fallback and key not in model_result:
            model_result[key] = fallback[key]
    model_result["intent"] = fallback.get("intent", model_result.get("intent", "model"))
    return model_result


async def handle_message(db: Session, actor: Actor, message: str, memory: str = "") -> dict:
    from .input_guard import sanitize_inbound

    cleaned, guard_error = sanitize_inbound(message)
    if guard_error:
        return {"reply": guard_error, "intent": "rejected", "actions": []}
    message = cleaned

    intent = detect_intent(message)
    if intent == "business_onboarding":
        result = await _handle_business_onboarding(db, actor, message)
        return await _maybe_model_drive(db, actor, message, result, memory)
    if intent == "develop_feature":
        result = await _handle_develop_feature(db, actor, message)
        result["reply"] = _coordinate(db, actor, message, result.get("reply", ""))
        return result

    try:
        from .multiagent import is_compound, run_multiagent

        if is_compound(message):
            multi = await run_multiagent(db, actor, message)
            if multi is not None:
                return multi
    except Exception:  # noqa: BLE001 - multi-agent is opt-in and must never block the chain
        pass

    result = await _run_agent_loop(db, actor, message, memory)
    if result is not None:
        return result

    if intent == "product_display":
        result = await _handle_product_display(db, actor, message)
    elif intent == "product_import":
        result = await _handle_product_import(db, actor, message)
    elif intent == "copy":
        result = await _handle_copy(db, actor, message)
    elif intent == "payment":
        result = await _handle_payment(db, actor, message)
    elif intent == "inventory":
        result = await _handle_inventory(db, actor, message)
    elif intent == "orders":
        result = await _handle_orders(db, actor, message)
    elif intent == "shipments":
        result = await _handle_shipments(db, actor, message)
    elif intent == "capabilities":
        result = await _handle_capabilities(db, actor, message)
    elif intent == "goal":
        result = await _handle_goal(db, actor, message)
    else:
        result = {"reply": HELP_REPLY, "intent": "help"}
        _record_task(db, actor, message, result)
    result["reply"] = _coordinate(db, actor, message, result.get("reply", ""))
    return result
