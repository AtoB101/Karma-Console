"""Local-only business briefing aggregation: window math and highlights."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.briefing import summarize


def _order(status, amount, created):
    return SimpleNamespace(status=status, total_amount=amount, created_at=created)


def _cust(stage, created):
    return SimpleNamespace(stage=stage, created_at=created)


def test_daily_counts_paid_in_window_and_ignores_out_of_window():
    now = datetime(2026, 10, 21, 12, 0, 0)
    orders = [
        _order("PAID", 100.0, now - timedelta(hours=2)),       # in window, paid
        _order("SHIPPED", 50.0, now - timedelta(hours=5)),    # in window, paid
        _order("unpaid", 999.0, now - timedelta(hours=1)),     # unpaid, excluded from revenue
        _order("PAID", 300.0, now - timedelta(days=3)),       # out of today window
    ]
    res = summarize(orders, [], [], now=now, period="daily")
    assert res["orders_in_window"] == 3
    assert res["revenue_local"] == 150.0


def test_weekly_window_includes_last_seven_days():
    now = datetime(2026, 10, 21, 12, 0, 0)
    orders = [_order("DELIVERED", 20.0, now - timedelta(days=3))]
    res = summarize(orders, [], [], now=now, period="weekly")
    assert res["orders_in_window"] == 1
    assert res["revenue_local"] == 20.0
    assert res["label"] == "近 7 天"


def test_highlights_flag_approvals_pending_ship_and_low_stock():
    now = datetime(2026, 10, 21, 12, 0, 0)
    products = [SimpleNamespace(id="p1", name="帽", stock=2)]
    res = summarize(
        [], [], products,
        pending_todos=3, pending_approvals=2, goals=[], now=now, period="daily",
    )
    txt = "｜".join(res["highlights"])
    assert "待审批" in txt
    assert "低库存" in txt
    assert res["pending_approvals"] == 2


def test_empty_business_gives_calm_message_and_caveat():
    res = summarize([], [], [], now=datetime.utcnow(), period="daily")
    assert any("平稳待命" in h for h in res["highlights"])
    assert "非平台对账" in res["caveat"]
