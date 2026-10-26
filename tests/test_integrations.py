"""Integration registry unit tests: local-only, never contacts the network."""
from app import integrations


def test_all_unavailable_without_credentials():
    summary = integrations.describe_all(env_overrides={})
    assert summary["total"] >= 6
    assert summary["ready_to_probe"] == 0
    assert summary["unavailable"] == summary["total"]
    for row in summary["adapters"]:
        assert row["configured"] is False
        assert row["status"] == "unavailable"
        assert row["missing"], "every unconfigured adapter must list what is missing"


def test_karma_ready_to_probe_only_when_required_filled():
    spec = next(s for s in integrations.REGISTRY if s["key"] == "karma_payment")
    partial = integrations.probe(spec, {"KRAMA_API_KEY": "k"})
    assert partial["configured"] is False
    assert partial["status"] == "unavailable"
    filled = integrations.probe(
        spec,
        {"KRAMA_API_KEY": "k", "KRAMA_AGENT_ID": "a", "KRAMA_IDENTITY_ID": "i"},
    )
    assert filled["configured"] is True
    assert filled["status"] == "ready_to_probe"
    assert filled["missing"] == []
    # ready_to_probe must never claim it is connected/successful
    assert "连接成功" not in filled["note"]
    assert "已连接" not in filled["note"]


def test_each_adapter_declares_real_methods():
    for spec in integrations.REGISTRY:
        assert spec["methods"], spec["key"]
        assert spec["required"], spec["key"]


def test_describe_counts_match_rows():
    summary = integrations.describe_all(env_overrides={})
    ready = sum(1 for r in summary["adapters"] if r["configured"])
    assert ready == summary["ready_to_probe"]
    assert summary["unavailable"] == summary["total"] - ready
