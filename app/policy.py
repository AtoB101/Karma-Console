"""Region-aware policy engine.

Region and geo-legal constraints are enforced here so they cannot be
overridden by a model, an agent, or an installed module. The concrete
rule values are PLACEHOLDERS and must be replaced after legal review
of each target market (the blueprint defers legal research to a later
phase).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RegionPolicyRule


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    require_approval: bool = False
    reasons: tuple[str, ...] = ()


@dataclass
class PolicyEngine:
    """Evaluates region rules plus hardcoded guardrails."""

    # Hard guardrails that no region may turn off.
    HARD_REASONS: dict[str, str] = field(
        default_factory=lambda: {
            "payment": "Payment actions always require explicit approval (R3).",
        }
    )

    def evaluate(self, db: Session, region: str, action: str) -> PolicyDecision:
        reasons: list[str] = []
        require_approval = False

        hard = self.HARD_REASONS.get(action)
        if hard:
            require_approval = True
            reasons.append(hard)

        rows = db.execute(
            select(RegionPolicyRule).where(
                RegionPolicyRule.region == region,
                RegionPolicyRule.enabled.is_(True),
            )
        ).scalars().all()

        for rule in rows:
            if rule.rule_type == "require_approval" and action in (rule.config.get("actions") or []):
                require_approval = True
                reasons.append(rule.note or f"region {region} requires approval for {action}")
            if rule.rule_type == "block" and action in (rule.config.get("actions") or []):
                return PolicyDecision(False, True, (rule.note or f"action {action} is blocked in {region}",))

        return PolicyDecision(True, require_approval, tuple(reasons))
