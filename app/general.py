"""Generalist capability layer: safe compute sandbox and workspace data files.

run_code gives the agent a bounded, network-free, filesystem-free compute
primitive (pure Python with a curated stdlib pre-import) so it can handle
arbitrary data tasks - aggregation, reconciliation, dedupe, formatting,
reporting - without hardcoding every business case. read_data / write_report
give the agent structured data in/out, capped and constrained to the
workspace's own file directory.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

FILES_ROOT = pathlib.Path(__file__).resolve().parent.parent / "data" / "files"
MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 200
MAX_STDOUT = 20000
CODE_TIMEOUT_SECONDS = 5.0

_SANDBOX_PREAMBLE = (
    "import json, math, datetime, statistics, collections, itertools, functools, re, decimal, fractions, random\n"
    "import sys as _sys\n"
    "__input__ = json.loads(_sys.stdin.read() or 'null')\n"
    "data = __input__\n"
    "inputs = __input__\n"
    "import builtins as _b\n"
    "_orig_import = _b.__import__\n"
    "_allowed = {'json','math','datetime','statistics','collections','itertools','functools','re','decimal','fractions','random','string','operator','typing','dataclasses','enum','copy','uuid','hashlib','base64','time','csv','heapq','bisect','textwrap','numbers','calendar'}\n"
    "def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
    "    if name.split('.')[0] not in _allowed:\n"
    "        raise ImportError(f\"import of {name!r} is not allowed in sandbox\")\n"
    "    return _orig_import(name, globals, locals, fromlist, level)\n"
    "_b.__import__ = _safe_import\n"
    "for _n in ('open','eval','exec','compile','input','breakpoint','exit','quit','memoryview'):\n"
    "    if hasattr(_b, _n): delattr(_b, _n)\n"
    "del _sys\n"
)


def workspace_dir(workspace_id: str) -> pathlib.Path:
    return FILES_ROOT / workspace_id


def ensure_workspace_dir(workspace_id: str) -> pathlib.Path:
    directory = workspace_dir(workspace_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_name(name: str) -> str:
    name = pathlib.Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fa5]", "_", name)
    return name or "file"


def save_upload(workspace_id: str, name: str, content_base64: str) -> dict:
    try:
        raw = base64.b64decode(content_base64 or "")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("content_base64 is not valid base64") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("file exceeds 2MB limit")
    file_name = safe_name(name)
    ensure_workspace_dir(workspace_id)
    (workspace_dir(workspace_id) / file_name).write_bytes(raw)
    return {"file_name": file_name, "size": len(raw), "workspace_id": workspace_id}


def list_data(workspace_id: str) -> dict:
    """List files directly inside this workspace's data dir (names only, no content)."""
    root = workspace_dir(workspace_id)
    items = []
    if root.exists():
        for p in root.iterdir():
            if not p.is_file():
                continue
            st = p.stat()
            items.append({"file_name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": items, "workspace_id": workspace_id}


def _read_text(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def read_data(workspace_id: str, file_name: str) -> dict:
    name = safe_name(file_name)
    path = workspace_dir(workspace_id) / name
    if not path.exists():
        return {"error": f"file not found: {name}"}
    text = _read_text(path)
    lower = name.lower()
    if lower.endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
        columns = list(rows[0].keys()) if rows else []
        return {"file_name": name, "kind": "csv", "columns": columns, "row_count": len(rows), "rows": rows[:MAX_ROWS]}
    if lower.endswith(".json"):
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"invalid json: {exc}", "file_name": name}
        return {"file_name": name, "kind": "json", "data": data}
    return {"file_name": name, "kind": "text", "content": text[:4000]}


def write_report(workspace_id: str, file_name: str, content) -> dict:
    name = safe_name(file_name)
    ensure_workspace_dir(workspace_id)
    path = workspace_dir(workspace_id) / name
    if isinstance(content, (dict, list)):
        text = json.dumps(content, ensure_ascii=False, indent=2)
    else:
        text = str(content)
        stripped = text.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                try:
                    import ast

                    parsed = ast.literal_eval(stripped)
                    text = json.dumps(parsed, ensure_ascii=False, indent=2)
                except Exception:  # noqa: BLE001
                    pass
    path.write_text(text, encoding="utf-8")
    return {"file_name": name, "path": str(path), "size": len(text.encode("utf-8"))}


def summarize_data(workspace_id: str, file_name: str, group_by: str, value_col: str, agg: str = "sum", output_file: str | None = None) -> dict:
    data = read_data(workspace_id, file_name)
    if data.get("kind") != "csv":
        return {"error": "summarize_data only supports CSV files", "file_name": file_name}
    rows = data.get("rows") or []
    groups: dict[str, list[float]] = {}
    for row in rows:
        key = row.get(group_by)
        if key is None:
            continue
        try:
            value = float(row.get(value_col) or 0)
        except (TypeError, ValueError):
            value = 0.0
        groups.setdefault(str(key), []).append(value)
    if agg == "sum":
        by_group = {k: sum(v) for k, v in groups.items()}
        total = sum(sum(v) for v in groups.values())
    elif agg == "avg":
        by_group = {k: statistics.mean(v) for k, v in groups.items()}
        total = statistics.mean([x for v in groups.values() for x in v]) if groups else 0.0
    elif agg == "count":
        by_group = {k: len(v) for k, v in groups.items()}
        total = sum(len(v) for v in groups.values())
    elif agg == "max":
        by_group = {k: max(v) for k, v in groups.items()}
        total = max([x for v in groups.values() for x in v]) if groups else 0.0
    elif agg == "min":
        by_group = {k: min(v) for k, v in groups.items()}
        total = min([x for v in groups.values() for x in v]) if groups else 0.0
    else:
        return {"error": f"unsupported agg: {agg}", "supported": ["sum", "avg", "count", "max", "min"]}
    out = {"file_name": file_name, "group_by": group_by, "value_col": value_col, "agg": agg, "by_group": by_group, "total": total}
    if output_file:
        out["written"] = write_report(workspace_id, output_file, out)
    return out


def run_code(code: str, inputs: dict | None = None, timeout: float = CODE_TIMEOUT_SECONDS) -> dict:
    code = str(code or "")
    if not code.strip():
        return {"error": "empty code"}
    if len(code) > 8000:
        return {"error": "code exceeds 8000 chars"}
    program = _SANDBOX_PREAMBLE + "\n" + code + "\n"
    payload = json.dumps(inputs or {}, ensure_ascii=False)
    proc = None
    spawn_error: PermissionError | None = None
    for attempt in range(5):
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", program],
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            break
        except subprocess.TimeoutExpired:
            return {"error": f"code timed out after {timeout}s"}
        except PermissionError as exc:
            # Windows can transiently deny the first child spawn right after a
            # database file swap (antivirus/timing). Retry before surfacing.
            if getattr(exc, "winerror", None) != 5:
                return {"error": f"sandbox spawn denied: {exc}"}
            spawn_error = exc
            time.sleep(0.1 * (2 ** attempt))
    if proc is None:
        return {"error": f"sandbox spawn failed: {spawn_error}"}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    result = {"exit_code": proc.returncode, "stdout": stdout[:MAX_STDOUT], "stderr": stderr[:4000]}
    if stdout:
        try:
            result["json"] = json.loads(stdout)
        except Exception:  # noqa: BLE001
            pass
    return result
