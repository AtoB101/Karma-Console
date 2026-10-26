"""Configuration rule packages for the Karma Console.

Drop a YAML file into app/rules/ and it becomes live (no kernel change).
Each package carries id/name/description/active/rules[]; the active rules
are merged into the planner prompt as deterministic constraints.
"""
from __future__ import annotations

import pathlib
import re
import time

import yaml

RULES_DIR = pathlib.Path(__file__).parent / "rules"

_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,48}$")
_CACHE: dict[str, list[dict]] = {}
_CACHE_TS = 0.0


def reload() -> int:
    """Rescan the rules directory; returns the number of valid packages."""
    global _CACHE, _CACHE_TS
    packs: list[dict] = []
    if not RULES_DIR.exists():
        RULES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(RULES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text("utf-8")) or {}
            if not isinstance(data, dict) or not data.get("id"):
                continue
            packs.append({
                "id": str(data["id"]),
                "name": str(data.get("name") or data["id"]),
                "description": str(data.get("description") or ""),
                "active": bool(data.get("active", True)),
                "builtin": bool(data.get("builtin", False)),
                "source": f.name,
                "rules": list(data.get("rules") or []),
                "applies_to": list(data.get("applies_to") or []),
            })
        except Exception:
            continue  # skip broken files instead of taking the system down
    _CACHE = packs
    _CACHE_TS = time.time()
    return len(packs)


def list_packs(refresh: bool = False) -> list[dict]:
    if refresh or not _CACHE or time.time() - _CACHE_TS > 30:
        reload()
    return [dict(p) for p in _CACHE]


def active_rules() -> list[str]:
    """Merged deterministic rule lines from every active package."""
    out: list[str] = []
    for p in list_packs():
        if not p["active"]:
            continue
        for r in p["rules"]:
            if isinstance(r, str) and r.strip():
                out.append(r.strip())
    return out


def brief() -> str:
    """Block injected into the planner prompt with active package rules."""
    rules = active_rules()
    if not rules:
        return ""
    lines = ["【本店规则包 · 必须遵守】以下规则来自已启用的规则包："]
    for r in rules:
        lines.append(f"- {r}")
    return "\n".join(lines)


def save_pack(payload: dict) -> dict:
    """Create a user rule package (id must start with 'user_')."""
    pack_id = str(payload.get("id") or "").strip()
    if not pack_id.startswith("user_"):
        raise ValueError("自定义规则包 id 必须以 user_ 开头，防止覆盖内置包")
    if not _ID_RE.match(pack_id):
        raise ValueError("规则包 id 仅允许字母/数字/下划线/中划线，长度 2-48")
    name = str(payload.get("name") or pack_id).strip()[:60]
    description = str(payload.get("description") or "").strip()[:200]
    rules = [str(r).strip() for r in (payload.get("rules") or []) if str(r).strip()]
    if not rules:
        raise ValueError("规则包至少需要 1 条规则")
    applies_to = [str(x).strip() for x in (payload.get("applies_to") or []) if str(x).strip()]
    doc = {
        "id": pack_id,
        "name": name,
        "description": description,
        "active": True,
        "builtin": False,
        "rules": rules,
        "applies_to": applies_to,
    }
    if not RULES_DIR.exists():
        RULES_DIR.mkdir(parents=True, exist_ok=True)
    path = RULES_DIR / f"{pack_id}.yaml"
    # atomic-ish write: only inside RULES_DIR, filename derived from validated id
    tmp = RULES_DIR / f".{pack_id}.{int(time.time() * 1000)}.tmp"
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    tmp.replace(path)
    reload()
    return {"id": pack_id, "name": name, "path": str(path), "rules": len(rules)}


def set_active(pack_id: str, active: bool) -> dict:
    """Flip the active flag of any package (builtin or user)."""
    path = RULES_DIR / f"{pack_id}.yaml"
    if not path.exists():
        raise FileNotFoundError("规则包不存在")
    doc = yaml.safe_load(path.read_text("utf-8")) or {}
    doc["active"] = bool(active)
    tmp = RULES_DIR / f".{pack_id}.{int(time.time() * 1000)}.tmp"
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    tmp.replace(path)
    reload()
    return {"id": pack_id, "active": bool(active)}


def delete_pack(pack_id: str) -> dict:
    """Delete a user rule package only; builtin packages are protected."""
    if pack_id.startswith("user_") is False:
        raise PermissionError("内置规则包不可删除，只可停用")
    path = RULES_DIR / f"{pack_id}.yaml"
    if not path.exists():
        raise FileNotFoundError("规则包不存在")
    path.unlink()
    reload()
    return {"id": pack_id, "deleted": True}
