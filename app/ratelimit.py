"""In-memory sliding-window rate limiting.

Single-process scope; suitable for the SQLite monolith. Multi-replica
deployments need a shared store (e.g. Redis) — documented in THREAT_MODEL.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from .config import settings


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window_seconds)
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        dq = self._hits[key]
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True


limiter_inbound = SlidingWindowLimiter(settings.chat_inbound_rate_limit, settings.chat_inbound_rate_window)
limiter_execute = SlidingWindowLimiter(settings.execute_rate_limit, settings.execute_rate_window)