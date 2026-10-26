"""内容流水线纯逻辑：文案/视频条目状态机与 CEO 拍板闸门（纯函数，不读库、不联网）。

链路：draft（Agent 起草）-> review（提交拍板）-> approved/rejected（CEO/老板）
-> ready_to_publish（交社交运营）-> published（渠道发布后回写，需真实凭证）。

安全核心：只有 approved 之后才能进入发布队列；未拍板的 draft/review/rejected
一律不得对外发布；发布与对外回复均需老板/CEO 确认，Agent 不得擅自外发。
"""
from __future__ import annotations

# 状态
DRAFT = "draft"
REVIEW = "review"
APPROVED = "approved"
REJECTED = "rejected"
READY = "ready_to_publish"
PUBLISHED = "published"

# 内容类型
KIND_COPY = "copy"
KIND_VIDEO = "video"
KINDS = {KIND_COPY, KIND_VIDEO}

_TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {REVIEW},
    REVIEW: {APPROVED, REJECTED},
    REJECTED: {DRAFT, REVIEW},
    APPROVED: {READY},
    READY: {PUBLISHED},
    PUBLISHED: set(),
}

# 可对外的状态：仅 ready_to_publish 允许进入发布队列
PUBLISHABLE = {READY}
# 终态
FINAL = {PUBLISHED}


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, set())


def publishable(status: str) -> bool:
    """是否可进入发布队列（仅 ready_to_publish）。"""
    return status in PUBLISHABLE


def needs_ceo_approval(status: str) -> bool:
    """是否处于等待 CEO/老板拍板。"""
    return status == REVIEW


def valid_kind(kind: str) -> bool:
    return kind in KINDS


def stage_label(status: str) -> str:
    return {
        DRAFT: "草稿（Agent 起草中）",
        REVIEW: "待 CEO 拍板",
        APPROVED: "已批准",
        REJECTED: "已驳回",
        READY: "待发布",
        PUBLISHED: "已发布",
    }.get(status, status)


def gate_publish(status: str) -> tuple[bool, str]:
    """发布闸门：返回 (允许, 原因)。未批准一律拒绝，绝不伪造发布成功。"""
    if status in PUBLISHABLE:
        return True, "approved 内容，可进入发布队列"
    if status == APPROVED:
        return False, "已批准但未交接为待发布（ready_to_publish）"
    if status == REVIEW:
        return False, "仍在等待 CEO 拍板，禁止发布"
    if status == REJECTED:
        return False, "已被驳回，禁止发布"
    if status == DRAFT:
        return False, "仍是草稿，禁止发布"
    return False, f"状态 {status} 不可发布"


def list_publishable(items: list[dict]) -> list[dict]:
    """从条目列表筛出可发布项（只读、纯函数）。"""
    return [it for it in items if publishable(it.get("status", ""))]
