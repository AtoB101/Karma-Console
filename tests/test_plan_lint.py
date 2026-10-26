"""P15 static tool-name lint: hallucinated tools rejected before execution."""
from app.plan import lint_known_tools, lint_params


def test_unknown_tool_rejected():
    errs = lint_known_tools(
        [{"tool": "make_magic", "params": {}}], {"list_products"}
    )
    assert errs and "make_magic" in errs[0]


def test_known_tool_passes():
    assert lint_known_tools(
        [{"tool": "list_products", "params": {}}], {"list_products"}
    ) == []


def test_multiple_unknown_all_reported():
    errs = lint_known_tools(
        [{"tool": "a", "params": {}}, {"tool": "b", "params": {}}], set()
    )
    assert len(errs) == 2


def test_unknown_param_rejected():
    allowed = {"create_product_draft": {"name", "price"}}
    errs = lint_params(
        [{"tool": "create_product_draft", "params": {"name": "x", "bogus": 1}}],
        allowed,
    )
    assert errs and "bogus" in errs[0]


def test_known_param_passes():
    allowed = {"create_product_draft": {"name", "price"}}
    assert lint_params(
        [{"tool": "create_product_draft", "params": {"name": "x", "price": 9}}],
        allowed,
    ) == []


def test_unmapped_tool_skipped():
    assert lint_params([{"tool": "custom_fn", "params": {"anything": 1}}], {}) == []
