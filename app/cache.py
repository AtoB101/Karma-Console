"""轻量进程内 TTL 只读缓存（线程安全，仅用于只读看板聚合，不缓存写路径）。

设计原则：
- 短 TTL（默认 10s）自然过期，不做复杂写失效，避免脏数据长期驻留；
- 每条记录带独立过期时间，get 时惰性剔除；
- key 必须自带 workspace_id，杜绝跨租户串读；
- 不缓存任何凭证/密文/PII 明文。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_STORE: dict[str, tuple[float, object]] = {}
_DEFAULT_TTL = 10.0


def get(key: str):
    now = time.monotonic()
    with _LOCK:
        item = _STORE.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= now:
            _STORE.pop(key, None)
            return None
        return value


def set(key: str, value, ttl: float = _DEFAULT_TTL) -> None:
    with _LOCK:
        _STORE[key] = (time.monotonic() + ttl, value)


def invalidate(prefix: str) -> int:
    """按前缀主动失效（写操作后调用），返回删除条数。"""
    n = 0
    with _LOCK:
        for k in list(_STORE):
            if k.startswith(prefix):
                _STORE.pop(k, None)
                n += 1
    return n


def clear() -> None:
    with _LOCK:
        _STORE.clear()
