"""Risk classification helpers (R0-R4)."""
from __future__ import annotations

from .models import RiskLevel


def requires_approval(level: str | RiskLevel, threshold: int = 3) -> bool:
    level = RiskLevel(level)
    rank = int(level.value[1])
    return rank >= threshold


def rank(level: str | RiskLevel) -> int:
    return int(RiskLevel(level).value[1])


def is_read_only(level: str | RiskLevel) -> bool:
    return RiskLevel(level) is RiskLevel.R0
