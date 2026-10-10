from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

import httpx

from app.observability.audit import AuditKind, AuditStatus, record_event

log = logging.getLogger(__name__)


class AIProviderError(Exception):
    pass


@dataclass
class Completion:
    """One model answer plus WHO produced it (matters once several providers can answer)."""
    text: str
    provider: str
    model: str
    valid: bool = True                      # False only when every responding provider returned unusable output
    attempts: list[str] = field(default_factory=list)   # human-readable failures that happened before this answer


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(self, system: str, user: str) -> str: ...

    async def complete_ex(self, system: str, user: str, validate: Callable[[str], object] | None = None) -> Completion:
        """Default: a single provider. ``validate`` (raises on bad output) is only used by FallbackProvider."""
        t0 = time.monotonic()
        try:
            text = await self.complete(system, user)
        except Exception as exc:
            record_event(AuditKind.AI_PROVIDER, AuditStatus.FAILED, provider=self.name, model=self.model, operation="complete",
                         component="ai.single", latency_ms=(time.monotonic() - t0) * 1000, error=str(exc))
            raise
        record_event(AuditKind.AI_PROVIDER, AuditStatus.OK, provider=self.name, model=self.model, operation="complete",
                     component="ai.single", latency_ms=(time.monotonic() - t0) * 1000)
        return Completion(text=text, provider=self.name, model=self.model)

    def chain_info(self) -> list[dict]:
        return [{"provider": self.name, "model": self.model}]


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
                 fallback_models: list[str] | None = None, json_mode: bool = True, total_budget_s: float = 120.0,
                 token_param: str = "max_tokens", send_temperature: bool = True, max_retry_wait_s: float = 8.0,
                 model_preference: list[str] | None = None):
        self.name, self.model = name, model
        self.token_param, self.send_temperature = token_param, send_temperature
        self.max_retry_wait_s = max_retry_wait_s
        self._preference = list(model_preference or [])
        self._model_checked = not self._preference
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
        body: dict = {"model": model, self.token_param: 1200,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.send_temperature:
            body["temperature"] = 0
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _resolve_model(self) -> None:
        """Model names on free tiers change often. When the configured model is not in the provider's live /models
        list, use the first model from the preference list that is (once per process)."""
        if self._model_checked:
            return
        self._model_checked = True
        try:
            r = await self._client.get(self._url.rsplit("/chat/completions", 1)[0] + "/models",
                                       headers={"Authorization": f"Bearer {self._key}"})
            if r.status_code != 200:
                return
            ids = {str(m.get("id", "")).removeprefix("models/") for m in (r.json().get("data") or []) if isinstance(m, dict)}
        except (httpx.HTTPError, ValueError, AttributeError):
            return
        if not ids or self.model in ids:
            return
        for cand in self._preference:
            if cand in ids:
                log.warning("%s: model %r is not offered; using %r", self.name, self.model, cand)
                self.model = cand
                return
        log.warning("%s: model %r is not in the provider's model list (keeping it; set the model explicitly)",
                    self.name, self.model)

    async def complete(self, system: str, user: str) -> str:
        started = time.monotonic()
        last_err = "no attempt made"
        json_mode = self.json_mode
        await self._resolve_model()
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
                        if retry_after is not None and retry_after > self.max_retry_wait_s:
                            break   # quota window is far away: waiting here would stall every token; next model/provider
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


class FallbackProvider(LLMProvider):
    """Ordered chain: try provider 1 (it already retries once itself), then 2, 3 ... on failure or unusable output.

    A provider that keeps failing is skipped for ``cooldown_s`` so a dead provider does not add its timeout to
    every single token; if ALL are cooling down they are tried anyway (never fail without an attempt).
    """

    def __init__(self, providers: list[LLMProvider], cooldown_s: float = 60.0, failures_to_cool: int = 2):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.name, self.model = providers[0].name, providers[0].model
        self.cooldown_s, self.failures_to_cool = cooldown_s, failures_to_cool
        self._fails: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def chain_info(self) -> list[dict]:
        return [{"provider": p.name, "model": p.model} for p in self.providers]

    async def complete(self, system: str, user: str) -> str:
        return (await self.complete_ex(system, user)).text

    def _note(self, p: LLMProvider, ok: bool) -> None:
        if ok:
            self._fails[p.name] = 0
            return
        n = self._fails.get(p.name, 0) + 1
        self._fails[p.name] = n
        if n >= self.failures_to_cool:
            self._until[p.name] = time.monotonic() + self.cooldown_s

    async def complete_ex(self, system: str, user: str, validate: Callable[[str], object] | None = None) -> Completion:
        now = time.monotonic()
        order = [p for p in self.providers if self._until.get(p.name, 0.0) <= now] or list(self.providers)
        attempts: list[str] = []
        invalid: Completion | None = None
        for p in order:
            t0 = time.monotonic()
            try:
                text = await p.complete(system, user)
            except AIProviderError as exc:
                self._note(p, False)
                attempts.append(f"{p.name}: {exc}")
                record_event(AuditKind.AI_PROVIDER, AuditStatus.FAILED, provider=p.name, model=p.model, operation="complete",
                             component="ai.fallback", latency_ms=(time.monotonic() - t0) * 1000, error=str(exc))
                log.warning("AI provider %s failed (%s); trying next", p.name, str(exc)[:160])
                continue
            except Exception as exc:  # noqa: BLE001 - one provider's bug must not take the chain down
                self._note(p, False)
                attempts.append(f"{p.name}: {type(exc).__name__}")
                record_event(AuditKind.AI_PROVIDER, AuditStatus.FAILED, provider=p.name, model=p.model, operation="complete",
                             component="ai.fallback", latency_ms=(time.monotonic() - t0) * 1000, error=type(exc).__name__)
                continue
            if validate is not None:
                try:
                    validate(text)
                except Exception as exc:  # noqa: BLE001
                    self._note(p, False)
                    attempts.append(f"{p.name}: unusable output ({type(exc).__name__})")
                    record_event(AuditKind.AI_PROVIDER, AuditStatus.FAILED, provider=p.name, model=p.model, operation="complete",
                                 component="ai.fallback", latency_ms=(time.monotonic() - t0) * 1000,
                                 error=f"unusable output ({type(exc).__name__}: {str(exc)[:120]})")
                    invalid = Completion(text=text, provider=p.name, model=p.model, valid=False, attempts=attempts)
                    continue
            self._note(p, True)
            record_event(AuditKind.AI_PROVIDER, AuditStatus.OK, provider=p.name, model=p.model, operation="complete",
                         component="ai.fallback", latency_ms=(time.monotonic() - t0) * 1000,
                         detail={"failed_before": attempts[:5]} if attempts else {})
            return Completion(text=text, provider=p.name, model=p.model, attempts=attempts)
        if invalid is not None:
            invalid.attempts = attempts
            return invalid
        raise AIProviderError("; ".join(attempts)[:400] or "no AI provider available")


@dataclass(frozen=True)
class _Preset:
    base_url: str
    models: tuple[str, ...]               # first = default; the rest are used if the default is not offered
    min_interval_s: float                 # pacing for the provider's free-tier request rate
    token_param: str = "max_tokens"
    send_temperature: bool = True


PRESETS: dict[str, _Preset] = {
    "cerebras": _Preset("https://api.cerebras.ai/v1", ("gpt-oss-120b", "llama-3.3-70b", "qwen-3-32b", "llama3.1-8b"), 2.5),
    "groq": _Preset("https://api.groq.com/openai/v1", ("llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"), 2.5),
    "gemini": _Preset("https://generativelanguage.googleapis.com/v1beta/openai", ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"), 6.5),
    # SERV: OpenAI chat-completions format at inference-api.openserv.ai (docs.openserv.ai); newer OpenAI-style params.
    "openserv": _Preset("https://inference-api.openserv.ai/v1", ("gpt-5.4-mini",), 1.0, "max_completion_tokens", False),
    "openrouter": _Preset("https://openrouter.ai/api/v1", ("openrouter/free",), 3.5),
}
_ALIASES = {"serv": "openserv", "openserv": "openserv", "google": "gemini", "gemini": "gemini", "cerebras": "cerebras",
            "groq": "groq", "openrouter": "openrouter"}
_AUTO_ORDER = ("cerebras", "groq", "gemini", "openserv", "openrouter")
# settings attribute holding each provider's key / model
_KEY_ATTR = {"cerebras": "cerebras_api_key", "groq": "groq_api_key", "gemini": "gemini_api_key",
             "openserv": "serv_api_key", "openrouter": "openrouter_api_key"}
_MODEL_ATTR = {"cerebras": "cerebras_model", "groq": "groq_model", "gemini": "gemini_model",
               "openserv": "serv_model", "openrouter": "ai_model"}


def _secret(v) -> str:
    if v is None:
        return ""
    return (v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)).strip()


def configured_order(settings) -> list[str]:
    """Provider names in the order they will be tried. ``llm_providers`` (comma list, or "auto") wins; the old
    single ``llm_provider`` setting still works."""
    raw = str(getattr(settings, "llm_providers", "") or "").strip().lower()
    if raw in ("auto", "*"):
        return [n for n in _AUTO_ORDER if _secret(getattr(settings, _KEY_ATTR[n], None))]
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        legacy = str(getattr(settings, "llm_provider", "") or "none").strip().lower()
        names = [] if legacy in ("", "none") else [legacy]
    out: list[str] = []
    for n in names:
        key = _ALIASES.get(n, n)
        if key not in out:
            out.append(key)
    return out


def _build_one(settings, name: str) -> LLMProvider | None:
    if name == "anthropic":
        key, model = _secret(getattr(settings, "anthropic_api_key", None)), (getattr(settings, "ai_model", "") or "").strip()
        return AnthropicProvider(key, model) if key and model else None
    if name == "openai":
        key, model = _secret(getattr(settings, "openai_api_key", None)), (getattr(settings, "ai_model", "") or "").strip()
        if not key or not model:
            return None
        return OpenAICompatibleProvider("openai", getattr(settings, "openai_base_url", "https://api.openai.com/v1"), key, model,
                                        max_retries=int(getattr(settings, "ai_max_retries", 1)))
    preset = PRESETS.get(name)
    if preset is None:
        log.error("AI: unknown provider %r in LLM_PROVIDERS (known: %s)", name, ", ".join(sorted(PRESETS)))
        return None
    key = _secret(getattr(settings, _KEY_ATTR[name], None))
    if not key:
        log.warning("AI: provider %s is listed but its API key is not set; skipping it", name)
        return None
    model = str(getattr(settings, _MODEL_ATTR[name], "") or "").strip() or preset.models[0]
    explicit = bool(str(getattr(settings, _MODEL_ATTR[name], "") or "").strip())
    return OpenAICompatibleProvider(
        name, preset.base_url, key, model,
        timeout=float(getattr(settings, "ai_timeout_s", 60.0)),
        max_retries=int(getattr(settings, "ai_max_retries", 1)),     # "1 retry, then the next provider"
        min_interval_s=float(getattr(settings, "ai_min_interval_s", 0.0) or 0.0) or preset.min_interval_s,
        fallback_models=_split_models(getattr(settings, "ai_fallback_models", "")) if name == "openrouter" else None,
        token_param=preset.token_param, send_temperature=preset.send_temperature,
        model_preference=None if explicit else list(preset.models),
    )


def build_provider(settings) -> LLMProvider | None:
    """AI chain from settings: ``ARC_RUNNER_LLM_PROVIDERS=cerebras,groq,gemini,serv`` (or ``auto``).

    Returns None when nothing usable is configured (AI disabled). One usable provider is returned as-is; several
    become a FallbackProvider that tries them in order (each with one retry first)."""
    names = configured_order(settings)
    if not names:
        log.warning("AI DISABLED: no provider configured (set ARC_RUNNER_LLM_PROVIDERS, e.g. cerebras,groq,gemini,serv)")
        return None
    built = [p for p in (_build_one(settings, n) for n in names) if p is not None]
    if not built:
        log.error("AI DISABLED: providers %s are configured but none has the API key (or model) it needs", names)
        return None
    provider: LLMProvider = built[0] if len(built) == 1 else FallbackProvider(built)
    log.info("AI ENABLED: chain=%s", " -> ".join(f"{p['provider']}/{p['model']}" for p in provider.chain_info()))
    return provider


def _split_models(raw) -> list[str]:
    return [m.strip() for m in str(raw or "").split(",") if m.strip()]
