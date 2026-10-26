"""Local ops readiness checks: structure and offline behavior."""
from app.ops_check import run_checks


def test_run_checks_shape_and_no_fail_on_ready_host():
    report = run_checks()
    assert "summary" in report and "checks" in report
    s = report["summary"]
    for key in ("pass", "fail", "warn", "healthy"):
        assert key in s
    assert isinstance(report["checks"], list) and report["checks"]
    for c in report["checks"]:
        assert {"level", "name", "ok", "detail"} <= set(c)
        assert c["level"] in ("PASS", "WARN", "FAIL")
    # This host has deps, DB, writable dirs and a secret key -> no FAIL.
    assert s["fail"] == 0
    assert s["healthy"] is True


def test_optional_credentials_never_fail():
    report = run_checks()
    ext = next(c for c in report["checks"] if c["name"].startswith("外部渠道凭证"))
    assert ext["level"] in ("PASS", "WARN")
    assert ext["ok"] is True


def test_checks_cover_core_areas():
    names = {c["name"] for c in run_checks()["checks"]}
    assert any("Python" in n for n in names)
    assert any("依赖" in n for n in names)
    assert any("数据库" in n for n in names)
    assert any("备份" in n for n in names)
    assert any("密钥" in n for n in names)
