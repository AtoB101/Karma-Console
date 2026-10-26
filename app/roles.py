"""Polsia-inspired AI team: one agent orchestrated as named business roles.

Each role is a view over the same gateway: it has a mission and a default tool
set, but every mutation still flows through permission -> policy -> approval.
"""
from __future__ import annotations

ROLES: list[dict] = [
    {
        "id": "store_manager",
        "name": "店长",
        "icon": "👑",
        "mission": "统筹全局、拆解任务、把需要主人拍板的事升级为待办、向主人汇报",
        "tools": ["business_overview", "workspace_summary", "create_todo", "list_todos", "create_goal", "update_goal", "get_commerce_policy", "set_commerce_policy"],
    },
    {
        "id": "listing",
        "name": "上架员",
        "icon": "📦",
        "mission": "商品建档、归类、规格/详情/资质、批量导入与上架",
        "tools": ["create_product_draft", "update_product", "import_products", "add_layout_area", "link_products_to_area", "publish_product", "list_products"],
    },
    {
        "id": "support",
        "name": "客服",
        "icon": "💬",
        "mission": "理解客户、报价、在折扣底线内谈判、跟进意向与成交",
        "tools": ["quote_product", "introduce_product", "reply_customer", "send_chat_message", "upsert_customer", "update_customer", "get_customer", "list_customers", "get_commerce_policy"],
    },
    {
        "id": "growth",
        "name": "运营增长",
        "icon": "📈",
        "mission": "营销文案、话术采集与优化、数据分析、经营复盘建议",
        "tools": ["generate_marketing_draft", "record_script", "optimize_scripts", "record_win", "summarize_data", "read_data", "write_report", "run_code"],
    },
    {
        "id": "fulfillment",
        "name": "履约员",
        "icon": "🚚",
        "mission": "订单、库存、发货、签收、物流跟踪",
        "tools": ["create_order", "list_orders", "list_inventory", "add_stock", "fulfill_order", "mark_delivered", "list_shipments", "cancel_order"],
    },
    {
        "id": "finance",
        "name": "财务",
        "icon": "💰",
        "mission": "收款、退款、对账、结算与经营报表",
        "tools": ["make_payment", "make_refund", "reconcile_payment", "dispute_payment", "list_orders", "business_overview", "get_commerce_policy"],
    },
]


def catalog() -> list[dict]:
    return [dict(role) for role in ROLES]