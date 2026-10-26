"""Encryption helpers for workspace secrets (model keys, chat tokens).

Fernet (AES-128-CBC + HMAC, from ``cryptography``) encrypts secrets at rest.
The key comes from ``KRAMA_SECRET_KEY`` (base64) or is generated once and
persisted to ``KRAMA_SECRET_KEY_FILE`` (default ``./.secret.key``) for local
development. Production should always set ``KRAMA_SECRET_KEY`` and rotate it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import pathlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_key: bytes | None = None


def _load_key() -> bytes:
    global _key
    if _key is not None:
        return _key
    if settings.secret_key:
        try:
            _key = base64.urlsafe_b64decode(settings.secret_key.encode())
        except Exception:  # noqa: BLE001 - allow raw utf-8 keys
            _key = settings.secret_key.encode()
        return _key
    key_file = pathlib.Path(settings.secret_key_file)
    if key_file.exists():
        _key = key_file.read_bytes().strip()
    else:
        _key = Fernet.generate_key()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(_key)
    return _key


def encrypt(value: str) -> str:
    if not value:
        return ""
    return Fernet(_load_key()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return Fernet(_load_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compare_hashes(computed: str, stored: str) -> bool:
    return hmac.compare_digest(computed, stored)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"