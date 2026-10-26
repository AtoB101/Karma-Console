"""Read-only recent-log viewer: redaction, tail slicing, empty-dir fallback."""
import pathlib

from app.log_view import redact_line, read_recent_logs


def test_redact_common_secrets():
    out = redact_line('POST /x api_key=abc123SECRET token="tok-999" Bearer eyJhbGciOiJSUzI1NiJ9')
    assert "abc123SECRET" not in out
    assert "tok-999" not in out
    assert "eyJhbGciOiJSUzI1NiJ9" not in out
    assert "****" in out


def test_empty_dir_returns_empty_without_faking(tmp_path):
    res = read_recent_logs(tmp_path / "nope", max_lines=20)
    assert res["empty"] is True
    assert res["file"] is None
    assert res["lines"] == []


def test_tail_slice_and_redaction(tmp_path):
    log = tmp_path / "server_1.log"
    lines = [f"line {i} api_key=KEY{i}" for i in range(10)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = read_recent_logs(tmp_path, max_lines=3)
    assert res["empty"] is False
    assert res["file"] == "server_1.log"
    assert res["count"] == 3
    # tail keeps only the last 3
    assert all(ln.startswith("line 9") or ln.startswith("line 8") or ln.startswith("line 7") for ln in res["lines"])
    # secrets redacted even in the kept tail
    assert all("KEY" not in ln for ln in res["lines"])
