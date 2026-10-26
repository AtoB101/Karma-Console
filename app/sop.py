"""P0-2 SOP 跟进节奏（3-5-7 法则）纯函数。

只做节奏派生与下一步建议，不直接写库；自动建待办由调度器在可验证阶段启用。
依据 docs/LEAD_CUSTOMER_BLUEPRINT.md 第 7 节。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


# 非成交线索的跟进节点：(距建档天数, 动作)
LEAD_STEPS = [
    (0, "D0 首触：欢迎语 + 需求确认，记录来源与痛点"),
    (3, "D3 跟进：发资料/案例，确认客户痛点"),
    (5, "D5 跟进：给出方案/报价建议，促进决策"),
    (7, "D7 定下一步：转商机 / 成交 / 关闭并转培育"),
]

# 成交后回访节点
WON_STEPS = [
    (7, "成交D7 回访：确认收货/使用，收集满意度，引导复购"),
]


@dataclass(frozen=True)
class NextAction:
    action: str
    sla_text: str
    urgency: str  # urgent | due | soon | calm
    due_in_hours: float | None


def sop_plan(stage: str) -> list[tuple[int, str]]:
    """按阶段返回跟进节点列表 [(offset_days, 动作)]。"""
    if stage == "won":
        return list(WON_STEPS)
    return list(LEAD_STEPS)


def next_action(grade: str, stage: str, last_touch_at: datetime | None,
               next_follow_at: datetime | None, now: datetime) -> NextAction:
    """派生当前应执行的下一步动作与紧迫度（只读建议）。"""
    # 已成交 → 回访
    if stage == "won":
        return NextAction(
            action="成交回访：确认收货使用、收集满意度、引导复购",
            sla_text="成交后第7天内完成",
            urgency="soon",
            due_in_hours=None,
        )

    # 冷线索 → 培育，不占热线
    if grade == "C":
        return NextAction(
            action="冷线索培育：归档入公海，本周复盘一次，不主动占用热线",
            sla_text="本周内复盘",
            urgency="calm",
            due_in_hours=None,
        )

    # 已有下次跟进时间且已过期 → overdue
    if next_follow_at and next_follow_at <= now:
        overdue = (now - next_follow_at).total_seconds() / 3600
        return NextAction(
            action="跟进超时未触达：立即补齐上一节点动作并记录",
            sla_text="已超时，需立即处理",
            urgency="urgent" if overdue >= 24 else "due",
            due_in_hours=0,
        )

    # 按分级给 SLA
    if grade == "H":
        return NextAction(
            action="热线索当天派单：15-30分钟内完成首触，锁定成交意向",
            sla_text="15-30分钟内首触",
            urgency="urgent",
            due_in_hours=0.5,
        )
    if grade == "A":
        return NextAction(
            action="温线索当天派单：当天内尽快首触，发需求确认",
            sla_text="当天内首触",
            urgency="due",
            due_in_hours=8,
        )
    # B 淡线索
    return NextAction(
        action="淡线索：排入队列，4小时内完成一次轻量触达",
        sla_text="4小时内触达",
        urgency="soon",
        due_in_hours=4,
    )


def schedule_after_create(created_at: datetime) -> list[tuple[datetime, str]]:
    """建档后生成全量计划时点（供后续调度器落待办）。"""
    plan: list[tuple[datetime, str]] = []
    for offset, action in LEAD_STEPS:
        plan.append((created_at + timedelta(days=offset), action))
    return plan
