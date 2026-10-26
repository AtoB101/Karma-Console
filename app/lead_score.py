"""P0 线索规则评分引擎（纯本地规则，不接外部模型，不伪造联调）。

依据 docs/LEAD_CUSTOMER_BLUEPRINT.md 第 6 节权重表，由客户画像与会话消息
本地计算 0-100 分并分级 H/A/B/C。纯函数，便于单测与复用。
"""
from __future__ import annotations

from dataclasses import dataclass


# 信号关键词（小写匹配，中文为主）与对应权重
_QUOTATION = ["报价", "多少钱", "价格", "底价", "优惠", "发个价", "报价单", "怎么卖"]
_SPEC_STOCK = ["规格", "库存", "有没有货", "有货", "能不能发", "发货", "几天到", "怎么寄", "运费", "型号"]
_APPOINT = ["预约", "到店", "留个", "加我", "电话", "微信", "联系方式", "看看现场", "约一下", "上门"]
_AFTERSALE = ["售后", "质保", "保修", "退换", "退货", "换货", "质量问题", "包修"]
_REFUSE = ["不需要", "别联系", "别发了", "已经买了", "拉黑", "拒绝", "不感兴趣", "别打扰"]

# 分级阈值
GRADE_THRESHOLDS = [
    ("H", 80, "热线索 · 当天派单，15-30分钟首触"),
    ("A", 60, "温线索 · 当天派单，尽快首触"),
    ("B", 40, "淡线索 · 排入4小时内触达队列"),
    ("C", 0, "冷线索 · 培育/公海，定期复盘"),
]


@dataclass(frozen=True)
class LeadScore:
    score: int
    grade: str
    label: str
    signals: list[str]


def _hit(text: str, keywords: list[str]) -> list[str]:
    t = text.lower()
    return [k for k in keywords if k in t]


def score_customer(customer, messages: list[dict] | None = None) -> LeadScore:
    """根据客户画像与会话消息计算评分。

    customer 只需具备 name/source/profile 属性；messages 为 [{content, direction}]。
    """
    profile = getattr(customer, "profile", None) or {}
    name = getattr(customer, "name", "") or ""
    source = getattr(customer, "source", "") or ""
    notes = profile.get("note", "") if isinstance(profile, dict) else ""

    score = 0
    signals: list[str] = []
    blob = " ".join((m.get("content") or "") for m in (messages or []))

    # 明确拒绝/失联 → 降级
    if _hit(blob, _REFUSE):
        score -= 25
        signals.append("明确拒绝/失联")
    # 问价/索取报价单
    if _hit(blob, _QUOTATION):
        score += 30
        signals.append("主动问价")
    # 咨询规格/库存/能否发货
    if _hit(blob, _SPEC_STOCK):
        score += 22
        signals.append("咨询规格/库存/发货")
    # 预约/留联系方式
    if _hit(blob, _APPOINT):
        score += 25
        signals.append("预约/留资")
    # 问售后/质保/退换
    if _hit(blob, _AFTERSALE):
        score += 12
        signals.append("咨询售后政策")
    # 多次回访（去重客户数 ≥2 由调用方统计后传入，这里按消息条数兜底）
    if messages and len(messages) >= 2:
        score += 10
        signals.append("多次互动")
    # 资料完整：姓名 + 来源 + 备注齐全
    if name and source and notes:
        score += 8
        signals.append("资料完整")

    # 收敛到 0-100
    score = max(0, min(100, score))
    grade, label = _grade(score)
    return LeadScore(score=score, grade=grade, label=label, signals=signals)


def _grade(score: int) -> tuple[str, str]:
    for grade, threshold, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade, label
    return "C", GRADE_THRESHOLDS[-1][2]


def grade_badge(grade: str) -> dict:
    """供前端展示：颜色与中文标签。"""
    table = {
        "H": {"color": "#ff6b7d", "text": "H · 热"},
        "A": {"color": "#ffc86b", "text": "A · 温"},
        "B": {"color": "#8fd8f7", "text": "B · 淡"},
        "C": {"color": "#48e6b3", "text": "C · 冷"},
    }
    return table.get(grade, {"color": "#8fd8f7", "text": grade or "—"})
