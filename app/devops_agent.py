"""受控系统维护/开发 Agent（DevOps Agent）安全内核。

定位：一个工具集受限的"系统工程师"经理人。它只能：读懂需求、拆变更单、
生成草案说明、列测试要点与回滚、跑回归门禁清单；**绝不**自动写生产文件、
装依赖、重启服务、联网拉代码/包。任何落地都必须老板审批后由受控通道完成。

本模块全为确定性纯函数，不联网、不写库、不执行系统命令。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 变更单状态机：draft -> proposed -> approved/rejected -> applied（人工落盘后确认）
STATUS_DRAFT = "draft"
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"

VALID_TRANSITIONS: dict[str, set[str]] = {
    STATUS_DRAFT: {STATUS_PROPOSED, STATUS_REJECTED},
    STATUS_PROPOSED: {STATUS_APPROVED, STATUS_REJECTED},
    STATUS_APPROVED: {STATUS_APPLIED, STATUS_REJECTED},
    STATUS_APPLIED: set(),
    STATUS_REJECTED: set(),
}

# 回归门禁状态
REG_PENDING = "pending"
REG_PASS = "pass"
REG_FAIL = "fail"

# Agent 被允许的能力（白名单）
CAPABILITIES: list[dict[str, str]] = [
    {"name": "read_only", "desc": "只读查看现有面板/端点/代码结构与日志"},
    {"name": "draft_proposal", "desc": "生成变更草案说明、影响面、测试要点、回滚要点（不写盘）"},
    {"name": "regression_checklist", "desc": "整理回归门禁清单并记录回归结果"},
    {"name": "file_ticket", "desc": "提交变更审批单，进入老板拍板队列"},
]

# Agent 被禁止的动作（红线，任何情况下不得自动执行）
DENIED_ACTIONS: list[str] = [
    "直接写入/覆盖 app/ 生产文件",
    "pip/npm 安装或升级依赖",
    "重启服务/进程/部署",
    "联网 git clone / curl 拉取未知代码或包",
    "删除或回滚现有功能、改动权限矩阵与支付审批红线",
    "跳过回归直接上线",
]

# 草案/需求文本里出现即判为"试图自动执行"的危险意图（仅用于标记，不执行）
_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pip 安装依赖", re.compile(r"pip\s+(install|freeze)|npm\s+(install|i)\b", re.I)),
    ("联网拉取代码", re.compile(r"git\s+clone|curl\s+\S+\s*\|\s*(bash|sh)|wget\s+https?://", re.I)),
    ("删除/清空", re.compile(r"rm\s+-rf|rmdir|del\s+/|Remove-Item\s+-Recurse|清空目录", re.I)),
    ("重启/关机", re.compile(r"systemctl\s+restart|reboot|shutdown|restart\s+(the\s+)?service|重启服务", re.I)),
    ("动态执行", re.compile(r"\b(eval|exec)\s*\(|os\.system|subprocess\.(run|call|Popen)", re.I)),
    ("自动写盘", re.compile(r"直接(写|覆盖|修改)生产文件|自动落地|自动上线|auto-deploy", re.I)),
]

# 关键词粗分类，用于确定性拆单（不依赖模型）
_DOMAIN_KEYWORDS: list[tuple[str, list[str]]] = [
    ("面板/前端", ["面板", "按钮", "页面", "前端", "显示", "UI", "图标", "卡片", "弹窗"]),
    ("数据/报表", ["报表", "统计", "图表", "趋势", "看板", "指标", "分析"]),
    ("客户/获客", ["客户", "线索", "获客", "跟进", "SOP", "分级", "话术"]),
    ("客服/工单", ["客服", "工单", "转人工", "FAQ", "售后", "SLA", "投诉"]),
    ("商品/库存", ["商品", "库存", "补货", "上下架", "SKU", "出入库"]),
    ("订单/资金", ["订单", "支付", "退款", "结算", "物流", "发货", "资金"]),
    ("安全/权限", ["权限", "加密", "审计", "设备", "备份", "安全", "密钥"]),
    ("工作流/调度", ["工作流", "DAG", "自动化", "定时", "触发", "调度", "Agent"]),
]


@dataclass
class SafetyAssessment:
    forbidden_hits: list[str] = field(default_factory=list)
    risk_level: str = "R1"
    requires_manual: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "forbidden_hits": self.forbidden_hits,
            "risk_level": self.risk_level,
            "requires_manual": self.requires_manual,
            "note": self.note,
        }


def assess_safety(requirement: str, draft: str = "") -> SafetyAssessment:
    """扫描需求与草案中的危险自动执行意图；命中即要求人工，绝不代为执行。"""
    blob = f"{requirement or ''}\n{draft or ''}"
    hits = []
    for label, pat in _FORBIDDEN_PATTERNS:
        if pat.search(blob):
            hits.append(label)
    level = "R1"
    if hits:
        level = "R3"
    return SafetyAssessment(
        forbidden_hits=hits,
        risk_level=level,
        requires_manual=True,
        note=("命中自动执行意图，必须人工落地并走审批" if hits else "仅生成草案，不自动写盘"),
    )


def decompose_requirement(text: str) -> dict[str, Any]:
    """确定性把一句话需求拆成变更单骨架（变更点/影响面/测试要点/回滚）。

    不调用模型、不臆造细节；缺失项留空待补。
    """
    t = text or ""
    domains = [name for name, kws in _DOMAIN_KEYWORDS if any(k in t for k in kws)]
    if not domains:
        domains = ["未分类（需人工补充范围）"]
    return {
        "domains": domains,
        "change_points": [f"围绕「{t.strip()[:80]}」补充对应能力（具体改动点待草案细化）"],
        "impact": [f"影响域：{ '、'.join(domains) }；需回归既有同类面板不被破坏"],
        "test_plan": [
            "回归脚本 regression.py 全绿",
            "新增能力对应纯函数/端点断言",
            "前端面板语法检查与只读连通",
        ],
        "rollback": "保留变更前版本快照；审批拒绝或回归失败则不落地，维持现状",
    }


def can_transition(current: str, target: str) -> bool:
    return target in VALID_TRANSITIONS.get(current, set())


def can_approve(regression_status: str, waive: bool = False, waive_note: str = "") -> tuple[bool, str]:
    """审批门禁：回归 pass 可批；fail 一律不可批；pending 需老板显式豁免并注明。"""
    if regression_status == REG_PASS:
        return True, "回归通过，可提交老板审批"
    if regression_status == REG_FAIL:
        return False, "回归失败，禁止审批，需先修复"
    # pending
    if waive and waive_note.strip():
        return True, "老板显式豁免未完成回归，需在落盘后补跑"
    return False, "回归未完成，需先跑回归或由老板显式豁免"


def capabilities_catalog() -> dict[str, Any]:
    return {"allowed": CAPABILITIES, "denied": DENIED_ACTIONS}


def regression_gate_summary(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    """对一批变更单做回归门槛汇总（纯函数，不读库、不联网）。

    每单按 regression_status（pass/fail/pending）归类，复用 can_approve 门禁：
    - fail 一律阻断批准（blocked）；
    - pass 可批（ready）；
    - pending（或未豁免）归入待补（pending）。
    只要存在 fail，整体门槛 gate_ok=False。
    """
    counts = {REG_PASS: 0, REG_FAIL: 0, REG_PENDING: 0}
    blocked: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for t in tickets:
        rs = t.get("regression_status") or REG_PENDING
        counts[rs] = counts.get(rs, 0) + 1
        ok, reason = can_approve(rs, bool(t.get("waive")), t.get("waive_note") or "")
        item = {
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "regression_status": rs,
            "approveable": ok,
            "reason": reason,
        }
        if rs == REG_FAIL:
            blocked.append(item)
        elif ok:
            ready.append(item)
        else:
            pending.append(item)
    return {
        "total": len(tickets),
        "counts": counts,
        "blocked": blocked,
        "ready": ready,
        "pending": pending,
        "gate_ok": len(blocked) == 0,
    }


# 待批滞留阈值（小时）：proposed 超过该时长未批即标 stale，提醒老板别压着
DEFAULT_STALE_HOURS = 24.0


def stale_changes(tickets: list[dict[str, Any]], now: Any,
                  threshold_hours: float = DEFAULT_STALE_HOURS) -> dict[str, Any]:
    """proposed 滞留超时检测（纯函数，不读库、不联网）。

    只统计仍处于 proposed 的变更单；以 submitted_at（缺失则 created_at 兜底）
    为起点，与 naive UTC ``now`` 比较，超过 threshold_hours 标 stale。
    """
    stale: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for t in tickets:
        if t.get("status") != STATUS_PROPOSED:
            continue
        anchor = t.get("submitted_at") or t.get("created_at")
        if anchor is None:
            continue
        hours = (now - anchor).total_seconds() / 3600.0
        item = {
            "id": t.get("id"),
            "title": t.get("title"),
            "regression_status": t.get("regression_status"),
            "waiting_hours": round(hours, 1),
            "stale": hours >= threshold_hours,
        }
        (stale if item["stale"] else waiting).append(item)
    return {
        "threshold_hours": threshold_hours,
        "stale_count": len(stale),
        "stale": stale,
        "waiting": waiting,
    }
