"""Read-only recent-log viewer for the system panel.

Pure functions: locate the newest ``*.log`` under a directory, keep the tail,
and redact obvious secrets before they reach the API response. Never writes,
never follows symlinks outside the target dir, and returns an empty result when
the directory or file is missing (no fake "all clear").
"""
from __future__ import annotations

import pathlib
import re

# Sensitive key names followed by ``=`` or ``:`` and a value.
_SECRET_KV = re.compile(
    r'(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|'
    r'secret|secret[_-]?key|password|passwd|authorization|bearer|cookie|'
    r'session[_-]?id|private[_-]?key)\s*[:=]\s*["\']?[^\s"\',;}\]]+'
)
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')


def redact_line(line: str) -> str:
    """Mask obvious secret values on a single log line."""
    line = _SECRET_KV.sub(lambda m: f"{m.group(1)}=****", line)
    line = _BEARER.sub("Bearer ****", line)
    return line


def newest_log(log_dir: pathlib.Path | str) -> pathlib.Path | None:
    directory = pathlib.Path(log_dir)
    if not directory.is_dir():
        return None
    candidates = [p for p in directory.glob("*.log") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_recent_logs(log_dir: pathlib.Path | str, max_lines: int = 80) -> dict:
    """Return ``{file, lines, count, empty}`` for the newest log's tail.

    ``lines`` are redacted; missing dir/file yields ``empty=True`` and no file.
    """
    max_lines = max(1, min(int(max_lines), 500))
    target = newest_log(log_dir)
    if target is None:
        return {"file": None, "lines": [], "count": 0, "empty": True}
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"file": None, "lines": [], "count": 0, "empty": True}
    lines = [redact_line(ln.rstrip("\n")) for ln in raw.splitlines()]
    tail = lines[-max_lines:]
    return {
        "file": target.name,
        "lines": tail,
        "count": len(tail),
        "empty": False,
    }
