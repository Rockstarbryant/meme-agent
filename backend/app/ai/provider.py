from __future__ import annotations

from abc import ABC, abstractmethod

import httpx


class AIProviderError(Exception):
    pass


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleProvider(LLMProvider):
    """Works for OpenRouter and any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.name, self.model = name, model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def complete(self, system: str, user: str) -> str:
        try:
            r = await self._client.post(self._url, headers={"Authorization": f"Bearer {self._key}"},
                                        json={"model": self.model, "temperature": 0,
                                              "messages": [{"role": "system", "content": system},
                                                           {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise AIProviderError(f"{self.name} request failed: {type(exc).__name__}") from exc


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.model, self._key = model, api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def complete(self, system: str, user: str) -> str:
        try:
            r = await self._client.post("https://api.anthropic.com/v1/messages",
                                        headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
                                        json={"model": self.model, "max_tokens": 1024, "system": system,
                                              "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise AIProviderError(f"anthropic request failed: {type(exc).__name__}") from exc


def build_provider(settings) -> LLMProvider | None:
    """Return None when no provider/key is configured (AI disabled).

    OpenRouter: if AI_MODEL is empty, default to ``openrouter/free`` (OpenRouter's
    Free Models Router — picks an available $0 model). Explicit AI_MODEL is kept as-is
    for all providers (e.g. a specific ``*:free`` id or a paid model).
    """
    p = getattr(settings, "llm_provider", None) or "none"
    if p in ("", "none"):
        return None

    if p == "openrouter" and getattr(settings, "openrouter_api_key", None):
        model = (settings.ai_model or "").strip() or "openrouter/free"
        return OpenAICompatibleProvider(
            "openrouter",
            "https://openrouter.ai/api/v1",
            settings.openrouter_api_key.get_secret_value(),
            model,
        )
    if p == "openai" and getattr(settings, "openai_api_key", None):
        if not (settings.ai_model or "").strip():
            return None
        return OpenAICompatibleProvider(
            "openai",
            getattr(settings, "openai_base_url", "https://api.openai.com/v1"),
            settings.openai_api_key.get_secret_value(),
            settings.ai_model.strip(),
        )
    if p == "anthropic" and getattr(settings, "anthropic_api_key", None):
        if not (settings.ai_model or "").strip():
            return None
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), settings.ai_model.strip())
    return None