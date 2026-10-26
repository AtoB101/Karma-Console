"""Threshold alert rules evaluated over the in-process metrics snapshot.

Single-node prototype: rules compare cumulative counters/gauges against
configurable thresholds. A triggered alert is delivered through the existing
owner-notification channel (stub when SMTP is unconfigured). This module keeps
``evaluate`` pure so thresholds can be unit-tested without a live server.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_THRESHOLDS = {
    # tool_executions: minimum total before error-rate judgement applies
    "tool_error_min_total": 10,
    "tool_error_rate": 0.25,
    # http_5xx: minimum cumulative 5xx before alerting
    "http_5xx_min_total": 5,
    # absolute errors counter before a burst alert
    "error_burst_count": 8,
    # approval_pending gauge > 0 considered overdue and reported
    "approval_pending_warn": 0,
}


@dataclass
class Alert:
    rule: str
    severity: str
    message: str
    value: float
    threshold: float
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "value": round(self.value, 4),
            "threshold": self.threshold,
            "detail": self.detail,
        }


def _labels(metric: dict) -> dict:
    return (metric or {}).get("labels", {}) or {}


def evaluate(metrics: dict, thresholds: dict | None = None) -> list[Alert]:
    """Evaluate rules against ``registry.snapshot()["metrics"]``."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alerts: list[Alert] = []

    tool = metrics.get("tool_executions") or {}
    tool_total = tool.get("value", 0) or 0
    tool_err = _labels(tool).get("status:error", 0) or 0
    if tool_total >= t["tool_error_min_total"]:
        rate = tool_err / tool_total
        if rate >= t["tool_error_rate"]:
            alerts.append(Alert(
                rule="tool_error_rate",
                severity="warning",
                message=f"工具错误率 {rate:.0%} 超过阈值 {t['tool_error_rate']:.0%}",
                value=rate,
                threshold=t["tool_error_rate"],
                detail={"errors": tool_err, "total": tool_total},
            ))

    http5 = metrics.get("http_5xx") or {}
    http5_total = http5.get("value", 0) or 0
    if http5_total >= t["http_5xx_min_total"]:
        alerts.append(Alert(
            rule="http_5xx",
            severity="critical",
            message=f"累计 {http5_total} 个 5xx 响应，超过阈值 {t['http_5xx_min_total']}",
            value=http5_total,
            threshold=t["http_5xx_min_total"],
            detail={"paths": _labels(http5)},
        ))

    errors = metrics.get("errors") or {}
    error_total = errors.get("value", 0) or 0
    if error_total >= t["error_burst_count"]:
        alerts.append(Alert(
            rule="error_burst",
            severity="critical",
            message=f"错误计数 {error_total} 达到突发阈值 {t['error_burst_count']}",
            value=error_total,
            threshold=t["error_burst_count"],
            detail={"scopes": _labels(errors)},
        ))

    pending = metrics.get("approval_pending") or {}
    pending_value = pending.get("value", 0) or 0
    if pending_value > t["approval_pending_warn"]:
        alerts.append(Alert(
            rule="approval_pending",
            severity="warning",
            message="存在待处理审批，请及时确认或驳回",
            value=pending_value,
            threshold=t["approval_pending_warn"],
        ))

    return alerts


def dispatch_alert(db, workspace_id: str, alert: Alert) -> dict:
    """Send one alert through the owner channel; best-effort, never raises."""
    from .notify import notify_owner_for_workspace

    subject = f"[Krama 告警] {alert.rule} ({alert.severity})"
    body = f"{alert.message}\n当前值: {alert.value}\n阈值: {alert.threshold}\n详情: {alert.detail}"
    try:
        return notify_owner_for_workspace(db, workspace_id, subject, body)
    except Exception as exc:  # noqa: BLE001 - alerting is best-effort
        return {"sent": False, "provider": "error", "error": str(exc)}
