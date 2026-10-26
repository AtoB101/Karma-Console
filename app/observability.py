"""Observability: structured JSON logs and an in-process metrics registry.

Deliberately dependency-free: stdlib logging plus a lock-guarded metrics
registry. Metrics are process-local counters/gauges suitable for a single
node; a multi-node deployment should export them to Prometheus/OTel, which
is a drop-in extension of snapshot().
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        try:
            from .request_id import get_request_id

            rid = get_request_id()
            if rid:
                payload["request_id"] = rid
        except Exception:  # pragma: no cover - correlation id is optional
            pass
        for attr in ("event", "workspace_id", "tool", "channel", "provider",
                     "status", "duration_ms", "error"):
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the JSON formatter to the application root logger once."""
    root = logging.getLogger("krama")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False


# --- Metrics registry -------------------------------------------------------


@dataclass
class _Metric:
    kind: str  # counter | gauge
    value: float = 0.0
    labels: dict[str, float] = field(default_factory=dict)


class MetricsRegistry:
    """Thread-safe in-process metric store (counters and gauges)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}
        self._started_at = time.time()

    def counter(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            metric = self._metrics.setdefault(name, _Metric(kind="counter"))
            metric.value += value
            if labels:
                for key, label_value in labels.items():
                    metric.labels[f"{key}:{label_value}"] = metric.labels.get(f"{key}:{label_value}", 0.0) + value

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            metric = self._metrics.setdefault(name, _Metric(kind="gauge"))
            metric.value = value
            if labels:
                for key, label_value in labels.items():
                    metric.labels[f"{key}:{label_value}"] = value

    def snapshot(self) -> dict:
        with self._lock:
            metrics = {
                name: {"kind": m.kind, "value": m.value, "labels": dict(m.labels)}
                for name, m in sorted(self._metrics.items())
            }
        return {
            "uptime_seconds": round(time.time() - self._started_at, 3),
            "metrics": metrics,
        }


registry = MetricsRegistry()
logger = logging.getLogger("krama")


# --- Convenience recorders --------------------------------------------------


def record_chat(workspace_id: str, status: str = "ok", duration_ms: float = 0.0) -> None:
    registry.counter("chat_requests", labels={"status": status})
    if duration_ms:
        registry.gauge("chat_last_duration_ms", duration_ms)
    logger.info("chat handled", extra={"event": "chat", "workspace_id": workspace_id,
                                       "status": status, "duration_ms": round(duration_ms, 3)})


def record_tool(tool: str, workspace_id: str, status: str = "ok", duration_ms: float = 0.0) -> None:
    registry.counter("tool_executions", labels={"tool": tool, "status": status})
    if status == "error":
        registry.counter("tool_errors", labels={"tool": tool})
    if duration_ms:
        registry.gauge("tool_last_duration_ms", duration_ms, labels={"tool": tool})
    logger.info("tool executed", extra={"event": "tool", "workspace_id": workspace_id,
                                        "tool": tool, "status": status,
                                        "duration_ms": round(duration_ms, 3)})


def record_webhook(provider: str, workspace_id: str, status: str = "ok", duration_ms: float = 0.0) -> None:
    registry.counter("webhook_requests", labels={"provider": provider, "status": status})
    if status == "error":
        registry.counter("webhook_errors", labels={"provider": provider})
    logger.info("webhook handled", extra={"event": "webhook", "workspace_id": workspace_id,
                                          "provider": provider, "status": status,
                                          "duration_ms": round(duration_ms, 3)})


def record_request(method: str, path: str, status: int, duration_ms: float) -> None:
    route = path.split("?")[0].split("/")[:3] if path else []
    normalized = "/" + "/".join(part for part in route if part) if route else path
    registry.counter("http_requests", labels={"method": method, "status": str(status), "path": normalized})
    if duration_ms:
        registry.gauge("http_last_duration_ms", duration_ms, labels={"path": normalized})
    if status >= 500:
        registry.counter("http_5xx", labels={"path": normalized})


def record_approval(workspace_id: str, state: str) -> None:
    registry.counter("approval_requests", labels={"state": state})
    if state == "pending":
        registry.gauge("approval_pending", 1)
    elif state in ("approved", "denied", "expired"):
        registry.gauge("approval_pending", 0)


def record_error(scope: str, error: str = "") -> None:
    registry.counter("errors", labels={"scope": scope})
    logger.error("error recorded", extra={"event": "error", "scope": scope, "error": error})
