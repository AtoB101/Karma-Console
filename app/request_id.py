"""Per-request correlation ID propagated through logs, audit and metrics.

Uses a context var so the ID survives async task boundaries within one request.
The HTTP middleware sets it from the ``X-Request-ID`` header (or generates one),
echoes it back on the response, and clears it after the response.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id or "")


def get_request_id() -> str:
    return _request_id.get()


def clear_request_id() -> None:
    _request_id.set("")
