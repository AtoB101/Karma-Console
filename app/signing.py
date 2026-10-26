"""Request-level signing helpers for Taobao TOP, Douyin and SF Express.

These implement the per-request signature algorithms from the public platform
docs and are called by ``SignedApiConnector``. They are pure functions: they
return the signature string and never mutate the caller's params, so callers
can attach ``sign``/``msgDigest`` afterwards.

Before production, cross-check each algorithm once against the platform's own
signing debug tool (see docs/receipt-integration-checklist.md).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta

_BEIJING = timezone(timedelta(hours=8))


def _concat_sorted(params: dict) -> str:
    cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
    return "".join(f"{k}{cleaned[k]}" for k in sorted(cleaned))


def sign_taobao(params: dict, app_secret: str) -> str:
    """Taobao TOP MD5 signature (uppercase, 32 hex)."""
    return hashlib.md5((_concat_sorted(params) + str(app_secret)).encode("utf-8")).hexdigest().upper()


def sign_douyin(params: dict, app_secret: str, sign_method: str = "hmac-sha256") -> str:
    """Douyin signature; ``sign_method`` accepts ``md5`` or ``hmac-sha256``."""
    method = str(sign_method or "md5").replace("_", "-").lower()
    base = (_concat_sorted(params) + str(app_secret)).encode("utf-8")
    if method == "hmac-sha256":
        return hmac.new(str(app_secret).encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hashlib.md5(base).hexdigest()


def sign_sf(msg_data: str, timestamp: str, checkword: str) -> str:
    """SF Fengqiao msgDigest (MD5 over msgData + timestamp + checkword)."""
    raw = f"{msg_data}{timestamp}{checkword}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def beijing_now_iso() -> str:
    """Beijing-time timestamp in Taobao's ``yyyy-MM-dd HH:mm:ss`` format."""
    return datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def unix_timestamp_seconds() -> str:
    return str(int(time.time()))


def unix_timestamp_millis() -> str:
    return str(int(time.time() * 1000))
