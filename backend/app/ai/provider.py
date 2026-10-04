from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)


class AIProviderError(Exception):
    pass


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(self, system: str, user: str) -> str: ...


class _RateGate:
    """Process-wide pacing for one provider. The free OpenRouter tier allows only ~20 requests/minute, and every
    tenant, token and retry shares it, so unpaced bursts were answered with HTTP 429 (shown as UNAVAILABLE)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self, min_interval_s: float) -> None:
        if min_interval_s <= 0:
            return
        async with self._lock:
            delay = min_interval_s - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


_GATES: dict[str, _RateGate] = {}


def _gate(name: str) -> _RateGate:
    return _GATES.setdefault(name, _RateGate())


def _error_text(r: httpx.Response) -> str:
    """Short, key-free description of an HTTP error (provider message when there is one)."""
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
        else:
            msg = err or ""
    except ValueError:
        msg = r.text
    return f"HTTP {r.status_code}" + (f": {str(msg)[:160]}" if msg else "")


class OpenAICompatibleProvider(LLMProvider):
    """Works for OpenRouter and any OpenAI-compatible /chat/completions endpoint.

    Failure handling (why AI used to show UNAVAILABLE): rate limits (429), upstream 5xx, timeouts and OpenRouter's
    "HTTP 200 with an error body" are retried with backoff (honouring Retry-After), then the next configured
    fallback model is tried; permanent errors (401/402/403) fail immediately with the provider's own message.
    """

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None, *, max_retries: int = 2,
                 retry_backoff_s: float = 1.5, min_interval_s: float = 0.0,
                 fallback_models: list[str] | None = None, json_mode: bool = True, total_budget_s: float = 120.0):
        self.name, self.model = name, model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        self.min_interval_s = max(0.0, min_interval_s)
        self.fallback_models = [m for m in (fallback_models or []) if m and m != model]
        self.json_mode = json_mode
        self.total_budget_s = total_budget_s

    def _payload(self, model: str, system: str, user: str, json_mode: bool) -> dict:
        body: dict = {"model": model, "temperature": 0, "max_tokens": 1200,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def complete(self, system: str, user: str) -> str:
        started = time.monotonic()
        last_err = "no attempt made"
        json_mode = self.json_mode
        for model in [self.model, *self.fallback_models]:
            attempt = 0
            while attempt <= self.max_retries:
                if time.monotonic() - started > self.total_budget_s:
                    raise AIProviderError(f"{self.name} gave up after {self.total_budget_s:.0f}s: {last_err}")
                await _gate(self.name).wait(self.min_interval_s)
                retry_after: float | None = None
                try:
                    r = await self._client.post(self._url, headers={"Authorization": f"Bearer {self._key}"},
                                                json=self._payload(model, system, user, json_mode))
                except httpx.TimeoutException:
                    last_err = f"{model}: timeout"
                except httpx.HTTPError as exc:
                    last_err = f"{model}: {type(exc).__name__}"
                else:
                    if r.status_code in (401, 402, 403):
                        raise AIProviderError(f"{self.name} rejected the request ({_error_text(r)})")
                    if r.status_code == 400 and json_mode:
                        json_mode = False        # model/provider does not support response_format: retry plain
                        last_err = f"{model}: {_error_text(r)}"
                        continue
                    if r.status_code in (408, 409, 425, 429) or r.status_code >= 500:
                        last_err = f"{model}: {_error_text(r)}"
                        try:
                            retry_after = float(r.headers.get("retry-after", ""))
                        except ValueError:
                            retry_after = None
                    elif r.status_code >= 400:
                        last_err = f"{model}: {_error_text(r)}"
                        break                     # not retryable for this model -> next fallback model
                    else:
                        try:
                            body = r.json()
                        except ValueError:
                            last_err = f"{model}: response was not JSON"
                        else:
                            choices = body.get("choices") if isinstance(body, dict) else None
                            if choices:
                                content = (choices[0].get("message") or {}).get("content")
                                if isinstance(content, str) and content.strip():
                                    return content
                                last_err = f"{model}: empty completion"
                            else:
                                err = body.get("error") if isinstance(body, dict) else None
                                msg = err.get("message") if isinstance(err, dict) else err
                                last_err = f"{model}: provider error: {str(msg or 'no choices')[:160]}"
                attempt += 1
                if attempt <= self.max_retries:
                    delay = retry_after if retry_after is not None else self.retry_backoff_s * (2 ** (attempt - 1))
                    await asyncio.sleep(min(20.0, max(0.0, delay)))
        raise AIProviderError(f"{self.name} request failed: {last_err}")


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
        log.warning("AI DISABLED: LLM_PROVIDER is %r -- WATCH/qualified tokens will NOT be sent to AI", p)
        return None
    provider = _build_provider(settings, p)
    if provider is None:
        log.error("AI DISABLED: LLM_PROVIDER=%s but its API key (or AI_MODEL for openai/anthropic) is missing", p)
    else:
        log.info("AI ENABLED: provider=%s model=%s", provider.name, provider.model)
    return provider


def _split_models(raw) -> list[str]:
    return [m.strip() for m in str(raw or "").split(",") if m.strip()]


def _build_provider(settings, p: str) -> LLMProvider | None:

    if p == "openrouter" and getattr(settings, "openrouter_api_key", None):
        model = (settings.ai_model or "").strip() or "openrouter/free"
        return OpenAICompatibleProvider(
            "openrouter",
            "https://openrouter.ai/api/v1",
            settings.openrouter_api_key.get_secret_value(),
            model,
            timeout=float(getattr(settings, "ai_timeout_s", 60.0)),
            max_retries=int(getattr(settings, "ai_max_retries", 2)),
            min_interval_s=float(getattr(settings, "ai_min_interval_s", 3.5)),
            fallback_models=_split_models(getattr(settings, "ai_fallback_models", "")),
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