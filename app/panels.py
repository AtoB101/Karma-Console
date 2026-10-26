"""Dynamic panel contract: approved component registry + config validation.

Phase-1 UI-001. Panels are created from constrained JSON configs whose
``type`` must be registered here; unknown components are rejected, and
invalid configs must never be enabled. This is the schema the frontend will
render against instead of free-form AI output.
"""
from __future__ import annotations

# component type -> {required, data_sources, allows_write}
# data_sources map to existing read endpoints under /workspaces/{id}/<key>.
# allows_write is advisory metadata for the frontend; server-side writes are
# still only possible through the gateway and never from a panel config.
DATA_SOURCES = ["customers", "products", "inventory", "orders", "shipments", "layout", "todos", "approvals", "events", "agents"]
COMPONENT_REGISTRY: dict[str, dict] = {
    "metric_card": {"required": ["title", "data_source"], "data_sources": DATA_SOURCES, "allows_write": False},
    "data_table": {"required": ["title", "data_source"], "data_sources": DATA_SOURCES, "allows_write": False},
    "data_list": {"required": ["title", "data_source"], "data_sources": DATA_SOURCES, "allows_write": False},
    "trend_chart": {"required": ["title", "data_source"], "data_sources": ["orders", "products", "inventory", "events"], "allows_write": False},
    "pie_chart": {"required": ["title", "data_source"], "data_sources": ["orders", "products", "inventory"], "allows_write": False},
    "filter": {"required": ["title"], "data_sources": [], "allows_write": False},
    "search_box": {"required": ["title"], "data_sources": [], "allows_write": False},
    "form": {"required": ["title"], "data_sources": [], "allows_write": True},
    "task_list": {"required": ["title"], "data_sources": ["todos"], "allows_write": False},
    "calendar": {"required": ["title"], "data_sources": ["todos"], "allows_write": False},
    "report_reader": {"required": ["title"], "data_sources": ["events"], "allows_write": False},
    "manager_status": {"required": ["title"], "data_sources": ["agents"], "allows_write": False},
    "approval_card": {"required": ["title"], "data_sources": ["approvals"], "allows_write": True},
    "log_viewer": {"required": ["title"], "data_sources": ["events"], "allows_write": False},
    "notification_list": {"required": ["title"], "data_sources": ["todos", "events"], "allows_write": False},
    # backward-compatible legacy types retained alongside the 14 base components
    "overview": {"required": [], "data_sources": [], "allows_write": False},
    "chat": {"required": ["title"], "data_sources": [], "allows_write": False},
    "todos": {"required": ["title"], "data_sources": ["todos"], "allows_write": False},
    "approvals": {"required": ["title"], "data_sources": ["approvals"], "allows_write": True},
}

VALID_STATUS = {"draft", "live", "disabled"}


def registry() -> list[dict]:
    """Approved component types (for the frontend picker / renderer)."""
    return [
        {
            "type": name,
            "required_fields": spec["required"],
            "allowed_data_sources": spec["data_sources"],
            "allows_write": spec["allows_write"],
        }
        for name, spec in COMPONENT_REGISTRY.items()
    ]


def validate_panel_config(config: dict) -> tuple[bool, list[str], dict]:
    """Return (ok, errors, normalized). Never raises for malformed input."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return False, ["panel config must be an object"], {}

    panel_type = str(config.get("type") or "").strip()
    spec = COMPONENT_REGISTRY.get(panel_type)
    if spec is None:
        return False, [f"未注册的组件类型: {panel_type or '(空)'}"], {}

    for field in spec["required"]:
        if not str(config.get(field) or "").strip():
            errors.append(f"缺少必要字段: {field}")

    if spec["data_sources"]:
        source = str(config.get("data_source") or "")
        if source not in spec["data_sources"]:
            errors.append(f"未授权的数据源: {source}")

    status = str(config.get("status") or "draft")
    if status not in VALID_STATUS:
        errors.append(f"非法状态: {status}")

    version = config.get("version", 1)
    if not isinstance(version, int) or version < 1:
        errors.append("version 必须是正整数")

    normalized = {
        "type": panel_type,
        "title": str(config.get("title") or ""),
        "data_source": str(config.get("data_source") or "") if spec["data_sources"] else "",
        "status": status,
        "version": version,
    }
    return (not errors), errors, normalized