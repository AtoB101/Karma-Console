"""P1 客服路由纯函数：转人工判定、FAQ 命中、话术推荐。

纯本地规则，不接外部模型、不写库；供接待层与工单层调用。
依据 docs/LEAD_CUSTOMER_BLUEPRINT.md 第 8/9 节。
"""
from __future__ import annotations

from dataclasses import dataclass


# 需要立即转人工的高敏感词（投诉/退款/纠纷/监管）
_HUMAN_TRIGGERS = [
    "投诉", "退款", "退货", "退换", "差评", "举报", "曝光", "起诉", "律师",
    "12315", "骗人", "欺骗", "诈骗", "钱没到账", "纠纷", "骗子", "工商",
    "315", "维权", "差评", "起诉你们",
]

# 售后/质量类，优先人工并建工单
_AFTERSALE_TRIGGERS = ["质量问题", "坏了", "破损", "漏发", "发错", "售后", "保修", "质保"]


@dataclass(frozen=True)
class RouteDecision:
    route: str          # "ai" | "human"
    need_human: bool
    reason: str
    priority: str       # P0 高 | P1 中 | P2 低
    action: str


def needs_human(text: str) -> RouteDecision:
    """判断单条客户消息是否必须转人工。"""
    t = (text or "").lower()
    for w in _HUMAN_TRIGGERS:
        if w.lower() in t:
            return RouteDecision(
                route="human", need_human=True,
                reason=f"命中高敏感词「{w}」，需人工介入",
                priority="P0",
                action="立即转人工并建立工单，不自动承诺",
            )
    for w in _AFTERSALE_TRIGGERS:
        if w.lower() in t:
            return RouteDecision(
                route="human", need_human=True,
                reason=f"命中售后/质量词「{w}」",
                priority="P1",
                action="转人工处理并建售后工单",
            )
    return RouteDecision(
        route="ai", need_human=False,
        reason="常规咨询，AI 可先应答",
        priority="P2",
        action="FAQ 命中后应答，答不准再升级",
    )


def _overlap(text: str, keywords: list[str]) -> int:
    t = (text or "").lower()
    return sum(1 for k in keywords if k and k.lower() in t)


@dataclass(frozen=True)
class FaqHit:
    matched: bool
    question: str
    answer: str
    score: int
    related: list


def match_faq(text: str, faqs: list[dict]) -> FaqHit:
    """按关键词重合度在 FAQ 列表中命中最匹配项。faqs: [{question,answer,tags,related_products}]。"""
    best = FaqHit(False, "", "", 0, [])
    for faq in faqs or []:
        q = faq.get("question", "")
        tags = faq.get("tags", []) or []
        related = faq.get("related_products", []) or []
        score = _overlap(text, [q, *tags])
        if score > best.score:
            best = FaqHit(True, q, faq.get("answer", ""), score, related)
    return best


def recommend_script(text: str, scripts: list[dict]) -> dict | None:
    """按标签/标题关键词重合度推荐最佳话术。scripts: [{title,content,tags,sends,wins}]。"""
    best: dict | None = None
    best_score = 0
    for sc in scripts or []:
        tags = sc.get("tags", []) or []
        score = _overlap(text, [sc.get("title", ""), *tags])
        if score > best_score:
            best_score = score
            best = sc
    return best if best_score > 0 else None


# SLA 响应时限（分钟），与后台 SLA 盯防阈值一致。
SLA_MINUTES = {"P0": 15.0, "P1": 60.0, "P2": 240.0}


@dataclass(frozen=True)
class SlaState:
    state: str               # closed | ok | near | breached
    age_minutes: float
    limit_minutes: float
    remaining_minutes: float | None
    label: str


def ticket_sla(priority: str, created_at, status: str, now) -> SlaState:
    """只读计算工单 SLA 状态：closed 已闭环；ok 余量充足；near 进入 30% 警戒；breached 超时。"""
    limit = SLA_MINUTES.get(priority or "P2", 240.0)
    if status == "closed" or created_at is None:
        return SlaState("closed", 0.0, limit, None, "已闭环")
    age = max(0.0, (now - created_at).total_seconds() / 60.0)
    remaining = limit - age
    if age >= limit:
        return SlaState("breached", round(age, 1), limit, round(remaining, 1),
                        f"已超时 {round(age - limit, 1)} 分钟")
    if remaining <= limit * 0.3:
        return SlaState("near", round(age, 1), limit, round(remaining, 1),
                        f"剩余 {round(remaining, 1)} 分钟（警戒）")
    return SlaState("ok", round(age, 1), limit, round(remaining, 1),
                    f"剩余 {round(remaining, 1)} 分钟")
