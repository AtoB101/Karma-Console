"""多模型注册表纯逻辑（纯函数，不读库、不联网、不打印明文密钥）。

总控制台可登记多个模型（文本/视频/图像能力标签），密钥以 Fernet 密文存于
settings["models"]；每个内容 Agent 在自己的操作页通过 ``model_id`` 选择适配
模型，密钥只在服务端按需解密供后端调用，前端始终只收到 mask 后的脱敏值。

兼容旧版单份 ``settings["model"]``：首次读取可平滑迁移为一条，不改写旧字段。
"""
from __future__ import annotations

import uuid

from . import crypto

TEXT = "text"
VIDEO = "video"
IMAGE = "image"
ALL_CAPS = {TEXT, VIDEO, IMAGE}


def _gen_id() -> str:
    return "m_" + uuid.uuid4().hex[:12]


def normalize_entry(raw: dict, existing: dict | None = None) -> dict:
    """归一化一条模型登记；新 api_key 明文优先，否则保留已有密文。"""
    existing = existing or {}
    enc = existing.get("api_key_enc") or ""
    if raw.get("api_key"):
        enc = crypto.encrypt(raw["api_key"])
    return {
        "id": existing.get("id") or raw.get("id") or _gen_id(),
        "label": (raw.get("label") or existing.get("label") or raw.get("provider") or "模型").strip()[:80],
        "provider": (raw.get("provider") or existing.get("provider") or "").strip()[:40],
        "model": (raw.get("model") or existing.get("model") or "").strip()[:120],
        "base_url": (raw.get("base_url") or existing.get("base_url") or "").strip()[:500],
        "capabilities": [c for c in (raw.get("capabilities") or existing.get("capabilities") or []) if c in ALL_CAPS],
        "api_key_enc": enc,
    }


def view(entry: dict) -> dict:
    """脱敏视图：绝不回传 api_key_enc，密钥只给 mask。"""
    enc = entry.get("api_key_enc") or ""
    return {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "provider": entry.get("provider"),
        "model": entry.get("model"),
        "base_url": entry.get("base_url"),
        "capabilities": entry.get("capabilities") or [],
        "configured": bool(enc),
        "api_key": crypto.mask(crypto.decrypt(enc)) if enc else "",
    }


def list_views(entries: list[dict]) -> list[dict]:
    return [view(e) for e in entries]


def _callable(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "provider": entry.get("provider"),
        "model": entry.get("model"),
        "base_url": entry.get("base_url"),
        "api_key": crypto.decrypt(entry.get("api_key_enc") or ""),
        "capabilities": entry.get("capabilities") or [],
    }


def resolve(entries: list[dict], model_id: str | None) -> dict | None:
    """按 id 解析出可调用配置（服务端内部用）；无 id 回退到首个已配置条目。

    返回 None 表示未配置可用密钥——调用方必须走"未配置/凭证位"分支，禁止伪造成功。
    """
    if model_id:
        for e in entries:
            if e.get("id") == model_id:
                return _callable(e) if e.get("api_key_enc") else None
    for e in entries:
        if e.get("api_key_enc"):
            return _callable(e)
    return None


def has_capability(entry: dict, cap: str) -> bool:
    return cap in (entry.get("capabilities") or [])


def migrate_legacy(legacy: dict | None) -> list[dict]:
    """旧 settings["model"] 单份迁移为注册表一条（密钥保持密文）。"""
    legacy = legacy or {}
    enc = legacy.get("api_key_enc") or (crypto.encrypt(legacy.get("api_key")) if legacy.get("api_key") else "")
    if not enc:
        return []
    return [{
        "id": "legacy-default",
        "label": "默认模型（旧）",
        "provider": legacy.get("provider", ""),
        "model": legacy.get("model", ""),
        "base_url": legacy.get("base_url", ""),
        "api_key_enc": enc,
        "capabilities": [TEXT],
    }]
