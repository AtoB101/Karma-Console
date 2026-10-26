"""Data classification, redaction and per-workspace data-map reporting.

Implements the platform requirement that every stored field carries an
explicit sensitivity tier (PUBLIC / INTERNAL / SENSITIVE / CRITICAL) so
the agent, audit trail and APIs can decide what may be logged, shown or
exported. Redaction rules are applied before sensitive values ever reach
logs, metrics or end-user responses.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any


class DataTier(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"
    CRITICAL = "CRITICAL"


# --- Key-level sensitivity detection ---------------------------------------

_CRITICAL_RE = re.compile(
    r"(karma[_-]?(api[_-]?key|key|secret|token)|payment[_-]?(key|secret)|"
    r"wallet[_-]?(key|secret|address|mnemonic)|private[_-]?key|"
    r"seed[_-]?phrase|mnemonic)",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|"
    r"credential|client[_-]?secret|confirm[_-]?code|verification[_-]?code|"
    r"one[_-]?time[_-]?code|otp|pin)",
    re.IGNORECASE,
)
_PII_RE = re.compile(
    r"(email|phone|mobile|id[_-]?card|id[_-]?number|identity|real[_-]?name|"
    r"address|location|wechat|weixin|telegram|discord|contact|name)",
    re.IGNORECASE,
)

# --- Table-level sensitivity ------------------------------------------------

# Default tier per table name. Anything not listed is INTERNAL.
TABLE_TIERS: dict[str, DataTier] = {
    "products": DataTier.INTERNAL,
    "orders": DataTier.SENSITIVE,
    "stock_movements": DataTier.INTERNAL,
    "shipments": DataTier.SENSITIVE,
    "customers": DataTier.SENSITIVE,
    "conversations": DataTier.SENSITIVE,
    "messages": DataTier.SENSITIVE,
    "scripts": DataTier.INTERNAL,
    "workspace_functions": DataTier.INTERNAL,
    "modules": DataTier.INTERNAL,
    "connector_instances": DataTier.CRITICAL,
    "layout_areas": DataTier.INTERNAL,
    "intents": DataTier.INTERNAL,
    "tasks": DataTier.INTERNAL,
    "approval_requests": DataTier.CRITICAL,
    "executions": DataTier.SENSITIVE,
    "audit_logs": DataTier.SENSITIVE,
    "todos": DataTier.SENSITIVE,
    "goals": DataTier.INTERNAL,
    "agent_memories": DataTier.SENSITIVE,
    "commerce_policies": DataTier.CRITICAL,
    "workspaces": DataTier.CRITICAL,
    "merchant_profiles": DataTier.INTERNAL,
    "api_keys": DataTier.CRITICAL,
    "users": DataTier.SENSITIVE,
    "memberships": DataTier.INTERNAL,
    "region_policy_rules": DataTier.PUBLIC,
}

# Explicit field-level overrides: (table, field) -> tier.
FIELD_TIERS: dict[tuple[str, str], DataTier] = {
    ("workspaces", "settings"): DataTier.CRITICAL,
    ("workspaces", "name"): DataTier.INTERNAL,
    ("workspaces", "region"): DataTier.PUBLIC,
    ("workspaces", "business_category"): DataTier.PUBLIC,
    ("customers", "profile"): DataTier.SENSITIVE,
    ("customers", "external_id"): DataTier.SENSITIVE,
    ("orders", "customer"): DataTier.SENSITIVE,
    ("orders", "items"): DataTier.INTERNAL,
    ("messages", "content"): DataTier.SENSITIVE,
    ("agent_memories", "value"): DataTier.SENSITIVE,
    ("audit_logs", "evidence"): DataTier.SENSITIVE,
    ("approval_requests", "payload"): DataTier.SENSITIVE,
    ("executions", "input"): DataTier.SENSITIVE,
    ("executions", "output"): DataTier.SENSITIVE,
    ("connector_instances", "config"): DataTier.CRITICAL,
    ("products", "qualifications"): DataTier.SENSITIVE,
    ("products", "price"): DataTier.INTERNAL,
}


def classify_key(key: str) -> DataTier:
    """Return the sensitivity tier for a single dict/JSON key name."""
    if not key:
        return DataTier.INTERNAL
    if _CRITICAL_RE.search(key):
        return DataTier.CRITICAL
    if _SENSITIVE_RE.search(key):
        return DataTier.SENSITIVE
    if _PII_RE.search(key):
        return DataTier.SENSITIVE
    return DataTier.INTERNAL


def classify_field(table: str, field: str) -> DataTier:
    """Return the effective tier for a (table, field) pair."""
    if (table, field) in FIELD_TIERS:
        return FIELD_TIERS[(table, field)]
    return classify_key(field)


def table_tier(table: str) -> DataTier:
    """Return the default tier for a whole table."""
    return TABLE_TIERS.get(table, DataTier.INTERNAL)


def redact_scalar(value: Any) -> Any:
    """Redact a scalar sensitive value to a fixed, non-reversible mask."""
    return "****"


def redact_mapping(data: Any) -> Any:
    """Recursively redact sensitive keys inside dicts/lists.

    Key names matching secret/token/PII patterns are replaced with a masked
    value. Non-sensitive keys are copied through, recursing into containers.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            tier = classify_key(str(key))
            if tier in (DataTier.CRITICAL, DataTier.SENSITIVE):
                out[str(key)] = redact_scalar(value)
            else:
                out[str(key)] = redact_mapping(value)
        return out
    if isinstance(data, list):
        return [redact_mapping(item) for item in data]
    if isinstance(data, tuple):
        return [redact_mapping(item) for item in data]
    return data


def redact_text(text: str) -> str:
    """Mask obvious secrets embedded in free-form text (URLs, key=value)."""
    if not text:
        return text
    masked = re.sub(r"(https?://)([^/@\s]+)@", r"\1****@", text)
    masked = re.sub(r"(?i)((?:api[_-]?key|secret|token|password|authorization|confirm[_-]?code)\s*[=:]\s*)([^\s,;]+)", r"\1****", masked)
    return masked


def build_data_map(db, workspace_id: str, region: str = "UNKNOWN") -> dict:
    """Produce a data-map summary for one workspace.

    Returns table row counts (tenant-scoped), tier assignment and the
    explicit sensitive-field inventory. No field values are exported; this
    is metadata about how data is classified, never the data itself.
    """
    from sqlalchemy import text as sql_text

    table_names = sorted(TABLE_TIERS.keys())
    counts: dict[str, int] = {}
    for table in table_names:
        try:
            row = db.execute(
                sql_text(f"SELECT COUNT(*) AS c FROM {table} WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).first()
            counts[table] = int(row.c) if row is not None else 0
        except Exception:  # noqa: BLE001 - table may not exist on fresh DB
            counts[table] = 0

    tiers: dict[str, list[str]] = {tier.value: [] for tier in DataTier}
    for table in table_names:
        tier = table_tier(table).value
        if table not in tiers[tier]:
            tiers[tier].append(table)

    sensitive_fields: list[dict] = []
    for (table, field), tier in sorted(FIELD_TIERS.items()):
        if tier in (DataTier.SENSITIVE, DataTier.CRITICAL):
            sensitive_fields.append({"table": table, "field": field, "tier": tier.value})

    return {
        "workspace_id": workspace_id,
        "region": region,
        "tiers": tiers,
        "counts": counts,
        "sensitive_fields": sensitive_fields,
        "redaction_enabled": True,
    }
