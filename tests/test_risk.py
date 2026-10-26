from app.models import RiskLevel
from app.risk import is_read_only, rank, requires_approval


def test_rank_and_approval():
    assert rank(RiskLevel.R0) == 0
    assert rank(RiskLevel.R4) == 4
    assert requires_approval(RiskLevel.R2) is False
    assert requires_approval(RiskLevel.R3) is True


def test_read_only():
    assert is_read_only(RiskLevel.R0) is True
    assert is_read_only(RiskLevel.R1) is False
