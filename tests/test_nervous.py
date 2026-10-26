"""Neural dispatch spine unit tests (pure functions, no DB/LLM)."""
from app import nervous


def test_assigned_card_has_all_required_fields():
    card = nervous.assign("fulfill_order")
    for field in ("domain", "role", "owner_name", "work", "autonomy", "escalate",
                  "boundary", "forbidden", "priority", "plan", "plan_b",
                  "cost", "worst_case", "receipt", "tool"):
        assert field in card, f"missing {field}"
    assert card["tool"] == "fulfill_order"
    assert isinstance(card["forbidden"], list) and card["forbidden"]


def test_money_actions_always_escalate():
    for tool in ("make_payment", "make_refund", "dispute_payment",
                 "bulk_update_prices", "set_commerce_policy"):
        card = nervous.assign(tool)
        assert card["escalate"] is True, tool
        assert card["autonomy"] == nervous.ESCALATE, tool


def test_routine_readonly_does_not_escalate():
    card = nervous.assign("list_products")
    assert card["escalate"] is False
    assert card["autonomy"] in (nervous.AUTO, nervous.POLICY)


def test_unknown_tool_safe_failure_escalates():
    card = nervous.assign("definitely_unknown_tool_xyz")
    assert card["escalate"] is True
    assert card["autonomy"] == nervous.ESCALATE
    assert card["role"] == "ceo"


def test_empty_intent_escalates():
    card = nervous.assign("")
    assert card["escalate"] is True


def test_panel_owner_known_and_fallback():
    own = nervous.panel_owner("order")
    assert own["role"] == "fulfillment"
    fb = nervous.panel_owner("does_not_exist_panel")
    assert fb["role"] == "ceo"


def test_tentacles_index_complete_and_self_consistent():
    idx = nervous.tentacles_index()
    assert len(idx) >= 6
    seen_tools: set[str] = set()
    for entry in idx:
        for field in ("id", "name", "mission", "work", "autonomy", "priority",
                      "tools", "panels", "boundary", "forbidden",
                      "plan", "plan_b", "cost", "worst_case", "receipt"):
            assert field in entry
        # a tool must not be claimed by two different domains
        for t in entry["tools"]:
            assert t not in seen_tools, f"tool {t} claimed twice"
            seen_tools.add(t)


def test_every_domain_has_plan_b_and_worst_case():
    for d in nervous.DOMAINS:
        assert d["plan_b"], d["id"]
        assert d["worst_case"], d["id"]
        assert d["cost"], d["id"]
        assert d["receipt"], d["id"]


def test_overview_ledger_rollups():
    ov = nervous.overview()
    assert ov["domains_total"] >= 6
    assert ov["tools_registered"] >= 20
    # every force-escalation tool is listed with an owner
    force_tools = {item["tool"] for item in ov["force_escalate"]}
    for t in nervous.FORCE_ESCALATE:
        assert t in force_tools
    # priority buckets exist for P0..P3
    for p in ("P0", "P1", "P2", "P3"):
        assert p in ov["by_priority"]
    # autonomy distribution sums to domain count
    assert sum(ov["autonomy_distribution"].values()) == ov["domains_total"]
    # each domain row carries boundary and forbidden count
    for row in ov["domains"]:
        assert row["boundary"]
        assert isinstance(row["forbidden_count"], int)


def test_dispatch_meta_carries_owner_and_fallback():
    meta = nervous.dispatch_meta("make_payment")
    assert meta["dispatch_escalate"] is True
    assert meta["dispatch_autonomy"] == "ESCALATE"
    assert meta["dispatch_owner"]
    assert meta["dispatch_plan_b"]

    ok = nervous.dispatch_meta("list_products")
    assert ok["dispatch_escalate"] is False
    assert ok["dispatch_owner"]


def test_escalation_brief_lists_every_force_tool_and_warns():
    brief = nervous.escalation_brief()
    for tool in nervous.FORCE_ESCALATE:
        assert tool in brief
    assert "必须主人确认" in brief or "主人确认" in brief
    assert "must_confirm" in brief

