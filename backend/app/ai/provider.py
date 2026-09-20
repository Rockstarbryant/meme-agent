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
    """Return None when no provider/key/model is configured (AI disabled)."""
    if not settings.ai_model:
        return None
    p = settings.llm_provider
    if p == "openrouter" and settings.openrouter_api_key:
        return OpenAICompatibleProvider("openrouter", "https://openrouter.ai/api/v1",
                                        settings.openrouter_api_key.get_secret_value(), settings.ai_model)
    if p == "openai" and settings.openai_api_key:
        return OpenAICompatibleProvider("openai", settings.openai_base_url,
                                        settings.openai_api_key.get_secret_value(), settings.ai_model)
    if p == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), settings.ai_model)
    return None
