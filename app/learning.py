"""Trace-based self-learning for the Karma Console.

Scans the workspace's execution history, aggregates success/failure per
(tool, status) pattern, and distills deterministic lessons. High-confidence,
owner-applied lessons are injected into the planner prompt so the system
gets smarter the more it works — without any manual rule editing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Execution, Lesson


def _pattern_key(tool_name: str, status: str) -> str:
    return f"{tool_name}:{status or 'UNKNOWN'}"


def _lesson_text(tool_name: str, status: str, fail_samples: list[dict]) -> str:
    """Deterministic lesson wording derived from real execution evidence."""
    if status == "SUCCEEDED":
        return (
            f"工具 {tool_name} 在本店历史执行均成功：需要完成同类任务时直接走该工具，"
            "无需升级老板；如输入信息缺失，先用 clarify 补齐再执行。"
        )
    reasons = set()
    for out in fail_samples:
        if not isinstance(out, dict):
            continue
        if out.get("error"):
            reasons.add(str(out["error"])[:120])
        elif out.get("detail"):
            reasons.add(str(out["detail"])[:120])
    reason_note = "；".join(list(reasons)[:2]) if reasons else "执行被网关拒绝或输入不合法"
    return (
        f"工具 {tool_name} 在本店历史出现过失败（{reason_note}）：再次遇到同类请求时，"
        "先核对权限/输入/依赖是否齐备，涉及资金或不可逆动作一律走 must_confirm 待办，"
        "不要直接重试同一参数。"
    )


def learn(db: Session, workspace_id: str, limit: int = 200) -> dict:
    """Recompute lessons from the most recent execution records.

    Aggregates per (tool, status) pattern; upserts the lesson row with
    smoothed confidence = (success+1)/(success+fail+2). Idempotent and cheap.
    """
    rows = db.execute(
        select(Execution)
        .where(Execution.workspace_id == workspace_id)
        .order_by(Execution.created_at.desc())
        .limit(limit)
    ).scalars().all()

    agg: dict[str, dict] = {}
    for r in rows:
        key = _pattern_key(r.tool_name or "?", r.status or "UNKNOWN")
        a = agg.setdefault(key, {"tool": r.tool_name or "?", "status": r.status or "UNKNOWN",
                                 "ok": 0, "fail": 0, "fails": []})
        if r.status == "SUCCEEDED":
            a["ok"] += 1
        elif r.status == "FAILED":
            a["fail"] += 1
            a["fails"].append(dict(r.output or {}))
        else:
            continue  # PENDING/RUNNING/UNKNOWN carry no lesson yet

    learned, updated = 0, 0
    for key, a in agg.items():
        if a["ok"] + a["fail"] == 0:
            continue
        status = "SUCCEEDED" if a["fail"] == 0 else ("FAILED" if a["ok"] == 0 else "MIXED")
        conf = (a["ok"] + 1) / (a["ok"] + a["fail"] + 2)
        lesson = _lesson_text(a["tool"], status, a["fails"])
        existing = db.execute(
            select(Lesson).where(
                Lesson.workspace_id == workspace_id,
                Lesson.pattern_key == key,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(Lesson(
                workspace_id=workspace_id,
                pattern_key=key,
                tool_name=a["tool"],
                status=status,
                lesson=lesson,
                success_count=a["ok"],
                fail_count=a["fail"],
                confidence=round(conf, 3),
            ))
            learned += 1
        else:
            existing.success_count = a["ok"]
            existing.fail_count = a["fail"]
            existing.confidence = round(conf, 3)
            existing.status = status
            existing.lesson = lesson
            updated += 1
    db.commit()
    return {"learned": learned, "updated": updated, "scanned": len(rows), "patterns": len(agg)}


def list_lessons(db: Session, workspace_id: str) -> list[dict]:
    rows = db.execute(
        select(Lesson)
        .where(Lesson.workspace_id == workspace_id)
        .order_by(Lesson.confidence.desc(), Lesson.updated_at.desc())
    ).scalars().all()
    return [
        {
            "id": r.id,
            "pattern_key": r.pattern_key,
            "tool_name": r.tool_name,
            "status": r.status,
            "lesson": r.lesson,
            "success_count": r.success_count,
            "fail_count": r.fail_count,
            "confidence": r.confidence,
            "applied": r.applied,
            "last_used_at": r.last_used_at.isoformat(timespec="seconds") if r.last_used_at else None,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        }
        for r in rows
    ]


def brief(db: Session, workspace_id: str, max_items: int = 4) -> str:
    """Deterministic block injected into the planner prompt.

    Includes only owner-applied lessons with confidence >= 0.5, so the
    system only 'remembers' what has been reviewed and proven.
    """
    rows = db.execute(
        select(Lesson)
        .where(Lesson.workspace_id == workspace_id, Lesson.applied.is_(True))
        .order_by(Lesson.confidence.desc(), Lesson.updated_at.desc())
        .limit(max_items)
    ).scalars().all()
    if not rows:
        return ""
    lines = ["【本店执行经验 · 已由老板确认采纳】遇到同类任务时优先参考："]
    for r in rows:
        lines.append(f"- {r.lesson}（来源：{r.pattern_key}，成功率 {round(r.confidence * 100)}%）")
    for r in rows:
        r.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return "\n".join(lines)
