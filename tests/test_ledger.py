"""Task board ledger classification unit tests (pure functions, no DB)."""
from types import SimpleNamespace

from app.ledger import build_board, COLUMNS


def _ex(tool, status, eid="e1"):
    return SimpleNamespace(id=eid, tool_name=tool, status=status, created_at=None)


def _ap(action, eid="a1"):
    return SimpleNamespace(id=eid, action=action, status="PENDING",
                           created_at=None, risk_level="R3")


def _todo(kind, status="pending", eid="t1"):
    return SimpleNamespace(id=eid, kind=kind, title="待办", detail="", status=status,
                           created_at=None, ref_type="", ref_id="")


def test_executions_classify_into_lanes():
    board = build_board(
        executions=[_ex("list_products", "SUCCEEDED", "x1"),
                    _ex("fulfill_order", "RUNNING", "x2"),
                    _ex("add_stock", "FAILED", "x3"),
                    _ex("list_orders", "PENDING", "x4")],
    )
    assert board["lanes"]["completed"][0]["title"] == "list_products"
    assert board["lanes"]["executing"][0]["title"] == "fulfill_order"
    assert board["lanes"]["failed"][0]["title"] == "add_stock"
    assert board["lanes"]["queued"][0]["title"] == "list_orders"


def test_force_escalation_approval_lands_in_escalated_lane():
    board = build_board(
        approvals=[_ap("make_payment", "p1"), _ap("list_products", "p2")],
    )
    escalated = [i["title"] for i in board["lanes"]["escalated"]]
    pending = [i["title"] for i in board["lanes"]["pending_approval"]]
    assert "make_payment" in escalated
    assert "list_products" in pending
    assert "make_payment" not in pending


def test_todos_classify_must_confirm_vs_queued():
    board = build_board(
        todos=[_todo("must_confirm", eid="t1"), _todo("needs_reply", eid="t2"),
               _todo("info", eid="t3")],
    )
    p = [i["id"] for i in board["lanes"]["pending_approval"]]
    q = [i["id"] for i in board["lanes"]["queued"]]
    assert "t1" in p
    assert {"t2", "t3"} <= set(q)


def test_counts_match_lanes_and_columns_complete():
    board = build_board(
        executions=[_ex("a", "SUCCEEDED")],
        approvals=[_ap("make_refund")],
        todos=[_todo("must_confirm")],
    )
    assert set(board["columns"]) == set(COLUMNS)
    for c in COLUMNS:
        assert board["counts"][c] == len(board["lanes"][c])
    assert board["counts"]["escalated"] == 1
