"""本地演示数据种子（幂等、可清空、只服务当前工作区，不伪造外部联调）。

用途：给空操作台填充一套中性自洽的演示样例，让首屏 KPI 与各面板有内容可看。
所有演示行写入独立注册表 ``demo_seed_registry`` 跟踪，展示文案不含任何标记字样；
``seed`` 幂等（先按注册表清空旧演示再插，重跑不翻倍），``reset`` 按注册表精确删除，
绝不动真实业务、凭证、模型/SMTP/Karma 配置（外部联调状态保持真实"未连接"）。

用法（在项目根目录运行，连同一个 krama.db）：
    python scripts/seed_demo.py                 # 填充（先清旧演示再插，幂等）
    python scripts/seed_demo.py --workspace-id <id>
    python scripts/seed_demo.py reset          # 只删除本工作区演示记录

安全：本脚本仅用于本地视觉演示；正式经营前先 ``reset``。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.db import SessionLocal, engine, create_all  # noqa: E402
from app import models as M  # noqa: E402


# 演示行登记顺序（子表在前，reset 反向删除）
REG_ORDER = [
    "messages", "conversations", "shipments", "stock_movements", "orders",
    "customers", "products", "todos", "approval_requests", "ai_agents",
    "goals", "agent_memories", "scripts", "scheduled_jobs",
]


def _ensure_registry() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS demo_seed_registry ("
            "id VARCHAR(32) PRIMARY KEY, table_name VARCHAR(32) NOT NULL, "
            "row_id VARCHAR(32) NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        ))


def latest_owner_workspace(db) -> str:
    row = db.execute(
        select(M.Membership.workspace_id)
        .where(M.Membership.role == "owner")
        .order_by(M.Membership.created_at.desc())
        .limit(1)
    ).scalar()
    if not row:
        raise SystemExit("未找到 owner 工作区，请用 --workspace-id 指定")
    return row


def _ago(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _reg(db, table: str, row_id: str) -> None:
    db.execute(text("INSERT INTO demo_seed_registry(id, table_name, row_id) VALUES (:i,:t,:r)"),
               {"i": M.new_id(), "t": table, "r": row_id})


def reset_demo(db, ws: str) -> None:
    """按注册表精确删除演示行，保持子先父后顺序。"""
    rows = db.execute(text(
        "SELECT table_name, row_id FROM demo_seed_registry"
    )).all()
    by_table: dict[str, list[str]] = {}
    for table, row_id in rows:
        by_table.setdefault(table, []).append(row_id)

    for table in reversed(REG_ORDER):
        ids = by_table.get(table) or []
        if not ids:
            continue
        model = {
            "messages": M.Message, "conversations": M.Conversation, "shipments": M.Shipment,
            "stock_movements": M.StockMovement, "orders": M.Order, "customers": M.Customer,
            "products": M.Product, "todos": M.Todo, "approval_requests": M.ApprovalRequest,
            "ai_agents": M.AIAgent, "goals": M.Goal, "agent_memories": M.AgentMemory,
            "scripts": M.Script, "scheduled_jobs": M.ScheduledJob,
        }[table]
        db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
    db.execute(text("DELETE FROM demo_seed_registry"))
    db.commit()


def seed(db, ws: str) -> None:
    reset_demo(db, ws)

    # ---- 商品 ----
    prod_rows = [
        ("简约纯棉圆领T恤", "服饰", 59.00, 128, "published"),
        ("宽松直筒休闲卫裤", "服饰", 129.00, 46, "published"),
        ("加厚连帽卫衣", "服饰", 169.00, 4, "published"),
        ("刺绣帆布托特包", "配饰", 89.00, 210, "published"),
        ("经典水洗牛仔外套", "服饰", 239.00, 3, "published"),
        ("新品针织开衫·待上架", "服饰", 149.00, 12, "draft"),
    ]
    products = {}
    for name, cat, price, stock, status in prod_rows:
        p = M.Product(workspace_id=ws, name=name, category=cat, price=price, stock=stock,
                      status=status, detail="基础款，现货常备。", specs={"材质": "棉", "尺码": "S/M/L"})
        db.add(p); db.flush(); products[name] = p
        _reg(db, "products", p.id)

    # ---- 客户 ----
    cust_rows = [
        ("cust-lin01", "独立站", "林女士", "won", "复购客户，偏好 M 码"),
        ("cust-chen01", "私信", "陈先生", "won", "成交过卫衣，沟通爽快"),
        ("cust-wang01", "独立站", "王女士", "lead", "咨询牛仔外套尺码与厚度"),
        ("cust-zhao01", "私信", "赵先生", "lead", "比价中，关注满减优惠"),
        ("cust-sun01", "独立站", "孙女士", "new", "刚咨询托特包容量"),
    ]
    customers = {}
    for ext, ch, nm, stage, note in cust_rows:
        c = M.Customer(workspace_id=ws, channel=ch, external_id=ext, name=nm, stage=stage,
                       profile={"note": note}, created_at=_ago(60 * 26), updated_at=_ago(30))
        db.add(c); db.flush(); customers[ext] = c
        _reg(db, "customers", c.id)

    # ---- 客户往来（林女士，in/out 各两条）----
    lin = customers["cust-lin01"]
    conv = M.Conversation(workspace_id=ws, customer_id=lin.id, channel="独立站", external_id="cust-lin01",
                          state="closed", summary="复购咨询卫衣补货", created_at=_ago(60 * 26), updated_at=_ago(60 * 3))
    db.add(conv); db.flush(); _reg(db, "conversations", conv.id)
    for direction, content, mins in [
        ("in", "您好，之前那件卫衣还有 M 码吗？想再入一件。", 60 * 26),
        ("out", "您好，M 码现货充足，今天下单可次日发出。", 60 * 26 - 5),
        ("in", "好的，那我直接拍下。", 60 * 3),
        ("out", "已为您备注优先发货，祝您购物愉快。", 60 * 3 - 4),
    ]:
        m = M.Message(workspace_id=ws, conversation_id=conv.id, direction=direction,
                      content=content, meta={}, created_at=_ago(mins))
        db.add(m); db.flush(); _reg(db, "messages", m.id)

    # ---- 订单（覆盖 CREATED/PAID/SHIPPED/DELIVERED）----
    order_rows = [
        ("SO2026101801", "独立站", "DELIVERED", "paid", 118.00, "林女士",
         [{"name": "简约纯棉圆领T恤", "quantity": 2, "price": 59.00}], 60 * 40),
        ("SO2026101901", "私信", "DELIVERED", "paid", 169.00, "陈先生",
         [{"name": "加厚连帽卫衣", "quantity": 1, "price": 169.00}], 60 * 30),
        ("SO2026102001", "独立站", "SHIPPED", "paid", 258.00, "王女士",
         [{"name": "经典水洗牛仔外套", "quantity": 1, "price": 239.00},
          {"name": "刺绣帆布托特包", "quantity": 1, "price": 19.00}], 60 * 18),
        ("SO2026102002", "私信", "SHIPPED", "paid", 89.00, "赵先生",
         [{"name": "刺绣帆布托特包", "quantity": 1, "price": 89.00}], 60 * 15),
        ("SO2026102101", "独立站", "PAID", "paid", 129.00, "孙女士",
         [{"name": "宽松直筒休闲卫裤", "quantity": 1, "price": 129.00}], 60 * 4),
        ("SO2026102102", "独立站", "CREATED", "unpaid", 239.00, "周先生",
         [{"name": "经典水洗牛仔外套", "quantity": 1, "price": 239.00}], 60 * 1),
    ]
    orders = {}
    for ref, ch, status, pay, amt, cname, items, mins in order_rows:
        o = M.Order(workspace_id=ws, channel=ch, order_reference=ref, status=status, payment_status=pay,
                    total_amount=amt, currency="CNY", customer={"name": cname}, items=items, created_at=_ago(mins))
        db.add(o); db.flush(); orders[ref] = o
        _reg(db, "orders", o.id)

    # ---- 物流运单（配两条 SHIPPED）----
    for ref, carrier, track, mins in [
        ("SO2026102001", "云途快运", "YD882041", 60 * 17),
        ("SO2026102002", "安迅速运", "SF663078", 60 * 14),
    ]:
        s = M.Shipment(workspace_id=ws, order_id=orders[ref].id, carrier=carrier,
                       tracking_number=track, status="SHIPPED", shipped_at=_ago(mins))
        db.add(s); db.flush(); _reg(db, "shipments", s.id)

    # ---- 库存出入库流水 ----
    mv = [
        (products["加厚连帽卫衣"].id, -1, "成交出库", orders["SO2026101901"].id, 60 * 30),
        (products["经典水洗牛仔外套"].id, -1, "成交出库", orders["SO2026102001"].id, 60 * 18),
        (products["简约纯棉圆领T恤"].id, 80, "到货入库", None, 60 * 50),
    ]
    for pid, delta, reason, oid, mins in mv:
        m = M.StockMovement(workspace_id=ws, product_id=pid, delta=delta, reason=reason,
                             order_id=oid, created_at=_ago(mins))
        db.add(m); db.flush(); _reg(db, "stock_movements", m.id)

    # ---- 待办 ----
    todos = [
        ("must_confirm", "确认本次批量调价方案", "全店商品统一下调 5 元，需主人输入确认码后执行。", "824613", 60 * 5),
        ("must_confirm", "确认开启自动报价", "客服在议价区间内自动报价，需主人确认码。", "391702", 60 * 9),
        ("needs_reply", "回复王女士的尺码咨询", "客户问牛仔外套是否偏厚，待主人回复口径。", "", 60 * 12),
        ("needs_reply", "跟进赵先生比价", "客户关注满减，待决定是否发券。", "", 60 * 20),
        ("info", "本周已完成 2 单送达", "系统汇总通知，无需处理。", "", 60 * 28),
    ]
    for kind, title, detail, code, mins in todos:
        t = M.Todo(workspace_id=ws, kind=kind, title=title, detail=detail, status="pending",
                   confirm_code=code, created_at=_ago(mins))
        db.add(t); db.flush(); _reg(db, "todos", t.id)

    # ---- 待审批（仅演示卡片，不触发真实资金动作）----
    approvals = [
        ("批量调价（全店 -5 元）", "R3", {"delta": -5.0}, "低库存商品清仓定价，需主人决策。", 60 * 6),
        ("开启客服自动回复", "R2", {"auto_reply": True}, "在白名单话术内自动应答，超出转人工。", 60 * 11),
    ]
    for action, risk, payload, reason, mins in approvals:
        a = M.ApprovalRequest(workspace_id=ws, action=action, risk_level=risk, payload=payload,
                              reason=reason, status="PENDING", idempotency_key="seed-" + str(mins), created_at=_ago(mins))
        db.add(a); db.flush(); _reg(db, "approval_requests", a.id)

    # ---- 经理人 ----
    agents = [
        ("店长经理人", "store_manager", "统管商品/订单/客户巡检",
         ["list_products", "list_orders", "list_customers"], "live"),
        ("客服助理", "cs_agent", "在白名单内应答客户、推进阶段",
         ["list_customers", "upsert_customer", "update_customer"], "test"),
        ("选品分析员", "analyst", "待配置的选品复盘岗位", [], "draft"),
    ]
    for name, role, desc, tools, status in agents:
        ag = M.AIAgent(workspace_id=ws, name=name, role=role, description=desc,
                       responsibilities=[f"负责{role}相关只读巡检"], allowed_tools=tools,
                       memory_policy={"scope": "workspace", "enabled": True},
                       execution_limits={"max_runtime_seconds": 300, "max_tool_calls": 20}, status=status)
        db.add(ag); db.flush(); _reg(db, "ai_agents", ag.id)

    # ---- 经营目标 ----
    goals = [
        ("本月成交 30 单", "稳定出单并提升复购。", "成交订单数达到 30，好评率 ≥95%",
         "已成交 4 单，进行中。", "active", 9),
        ("低库存补齐", "卫衣、牛仔外套补货至安全线以上。", "两款低库存商品库存 ≥20",
         "待向供应商下单。", "active", 3),
        ("搭建商品详情页", "完善在售商品图文。", "5 个在售商品均有完整详情",
         "已完成，归档。", "done", 0),
    ]
    for title, detail, crit, note, status, days in goals:
        g = M.Goal(workspace_id=ws, title=title, detail=detail, success_criteria=crit,
                   progress_note=note, status=status,
                   due_at=_ago(-60 * 24 * days) if days else None)
        db.add(g); db.flush(); _reg(db, "goals", g.id)

    # ---- 主脑持久记忆 ----
    mems = [
        ("default_tone", "对客户用简洁、专业、温和的语气，避免夸大承诺。", "preference"),
        ("shop_hours", "在线回复时段 09:00-22:00，夜间消息次日优先回。", "fact"),
        ("return_policy", "支持 7 天无理由退换，质量问题包邮退换。", "fact"),
    ]
    for key, value, kind in mems:
        mm = M.AgentMemory(workspace_id=ws, key=key, value=value, kind=kind)
        db.add(mm); db.flush(); _reg(db, "agent_memories", mm.id)

    # ---- 营销文案草稿 ----
    sc = M.Script(workspace_id=ws, title="新品到店问候语",
                  content="您好，新品已上架，现货可发，喜欢可直接拍下～",
                  tags=["问候", "新品"], wins=0, sends=0)
    db.add(sc); db.flush(); _reg(db, "scripts", sc.id)

    # ---- 定时任务（本地调度器关闭，不会真执行，仅展示）----
    jobs = [
        ("每日订单对账巡检", {"type": "interval", "minutes": 60}, "核对当日订单与收款，差异上报", True, 60),
        ("低库存预警扫描", {"type": "interval", "minutes": 30}, "库存 ≤5 自动提醒补货", True, 30),
        ("每周经营简报", {"type": "event", "event": "weekly_review"}, "汇总本周成交与客户漏斗", False, 60 * 24 * 7),
    ]
    for name, trigger, goal, enabled, mins in jobs:
        j = M.ScheduledJob(workspace_id=ws, name=name, trigger=trigger, goal=goal,
                           steps=[], enabled=enabled,
                           last_run_at=_ago(mins), next_run_at=_ago(-mins))
        db.add(j); db.flush(); _reg(db, "scheduled_jobs", j.id)

    # ---- 商家档案（缺失则建；若为旧标记名则更正为干净名，不覆盖用户真实店名）----
    mp = db.execute(select(M.MerchantProfile).where(M.MerchantProfile.workspace_id == ws)).scalar()
    if mp is None:
        db.add(M.MerchantProfile(workspace_id=ws, shop_name="星选优品",
                                 category="clothing", region="CN", currency="CNY"))
    elif mp.shop_name and ("演示" in mp.shop_name or "DEMO" in mp.shop_name.upper()):
        mp.shop_name = "星选优品"

    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", nargs="?", default="seed", choices=["seed", "reset"])
    parser.add_argument("--workspace-id", default="")
    args = parser.parse_args()

    create_all()
    _ensure_registry()
    db = SessionLocal()
    try:
        ws = args.workspace_id or latest_owner_workspace(db)
        if args.cmd == "reset":
            reset_demo(db, ws)
            print(f"[reset] 已清理工作区 {ws[:8]} 的演示记录")
        else:
            seed(db, ws)
            print(f"[seed] 演示数据已写入工作区 {ws[:8]}，刷新页面即可查看")
        for label, model in [("商品", M.Product), ("订单", M.Order), ("客户", M.Customer),
                             ("待办", M.Todo), ("待审批", M.ApprovalRequest), ("经理人", M.AIAgent),
                             ("目标", M.Goal), ("定时任务", M.ScheduledJob)]:
            n = db.execute(select(model).where(model.workspace_id == ws)).scalars().all()
            print(f"  {label}: {len(n)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
