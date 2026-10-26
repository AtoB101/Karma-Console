"""Model routing: deterministic mock provider plus an OpenAI-compatible one.

The platform must not depend on a single model; callers only depend on
``generate`` (marketing copy) and ``chat`` (planner / coordinator with full
message lists). Provider selection comes from configuration.
"""
from __future__ import annotations

import contextvars
import time

import httpx

from .config import settings

MARKETING_SYSTEM = "You write concise, truthful merchant marketing copy in the language of the request."

# 请求级模型覆盖：每个 Agent 操作页选了 model_id 后，该次对话全程走这个模型，
# 并在请求结束时汇总 usage（token 计量）。未设置时回退到工作区/全局默认模型。
_request_model: contextvars.ContextVar[dict | None] = contextvars.ContextVar("krama_request_model", default=None)
_usage_collector: contextvars.ContextVar[list | None] = contextvars.ContextVar("krama_usage", default=None)


def set_request_model(model_config: dict | None) -> None:
    _request_model.set(model_config)
    _usage_collector.set([])


def pop_usage() -> dict | None:
    """取出本次请求累计的 token 用量并复位；无模型真实调用时返回 None。"""
    rows = _usage_collector.get()
    _usage_collector.set(None)
    if not rows:
        return None
    def _sum(key: str) -> int:
        return int(sum(int(r.get(key) or 0) for r in rows))
    return {
        "calls": len(rows),
        "prompt_tokens": _sum("prompt_tokens"),
        "completion_tokens": _sum("completion_tokens"),
        "total_tokens": _sum("total_tokens") or (_sum("prompt_tokens") + _sum("completion_tokens")),
    }


class MockProvider:
    name = "mock"

    def generate(self, prompt: str, context: str = "") -> str:
        return (
            f"[mock] Marketing draft. Prompt: {prompt[:80]!r}. "
            f"Context: {context[:80]!r}. Generated deterministically for tests."
        )

    def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None, json_mode: bool = False) -> str:
        last = messages[-1]["content"] if messages else ""
        if json_mode:
            return '{"intent":"chat","steps":[],"reply":"mock","done":true}'
        return f"[mock] {last[:160]!r}"


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None, json_mode: bool = False) -> str:
        if not self.api_key:
            raise RuntimeError("model api key is not configured")
        url = f"{self.base_url}/chat/completions"
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("choices") or [{}])[0].get("message") or {}
                usage = data.get("usage")
                collector = _usage_collector.get()
                if isinstance(usage, dict) and collector is not None:
                    collector.append(usage)
                return content.get("content") or ""
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        raise last_exc if last_exc is not None else RuntimeError("model request failed")

    def generate(self, prompt: str, context: str = "") -> str:
        messages = [
            {"role": "system", "content": MARKETING_SYSTEM},
            {"role": "user", "content": f"Context: {context}\n\nTask: {prompt}"},
        ]
        return self.chat(messages, temperature=0.7)


def _build_provider() -> MockProvider | OpenAICompatProvider:
    if settings.default_model_provider == "openai_compat" and settings.openai_api_key:
        return OpenAICompatProvider(settings.openai_base_url, settings.openai_api_key, settings.openai_model)
    return MockProvider()


_provider = _build_provider()


def workspace_model_config(db, workspace_id: str) -> dict | None:
    """Return the workspace-level model config when one is configured.

    The API key is decrypted here and only exists in memory as plaintext.
    """
    from . import crypto
    from .models import Workspace

    ws = db.get(Workspace, workspace_id)
    if ws is None:
        return None
    model = (ws.settings or {}).get("model") or {}
    api_key = ""
    if model.get("api_key_enc"):
        api_key = crypto.decrypt(model["api_key_enc"])
    elif model.get("api_key"):
        api_key = model["api_key"]  # legacy plaintext
    if not api_key:
        return None
    return {
        "provider": model.get("provider") or "openai_compat",
        "base_url": model.get("base_url") or settings.openai_base_url,
        "api_key": api_key,
        "model": model.get("model") or settings.openai_model,
    }


def _provider_for(model_config: dict | None):
    # 显式传入优先，其次请求级覆盖（Agent 页选的 model_id），最后全局默认。
    cfg = model_config or _request_model.get()
    if cfg and cfg.get("api_key"):
        return OpenAICompatProvider(
            base_url=cfg.get("base_url") or settings.openai_base_url,
            api_key=cfg["api_key"],
            model=cfg.get("model") or settings.openai_model,
        )
    return _provider


def generate(prompt: str, context: str = "", model_config: dict | None = None) -> str:
    return _provider_for(model_config).generate(prompt=prompt, context=context)


def chat(messages: list[dict], model_config: dict | None = None, temperature: float = 0.2, max_tokens: int | None = None, json_mode: bool = False) -> str:
    return _provider_for(model_config).chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)


def provider_name() -> str:
    return _provider.name
