from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import MarketState

log = logging.getLogger("market_data")


@dataclass
class ProviderHealth:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str | None = None
    last_ok: float | None = None


class MarketDataRegistry(MarketDataProvider):
    """Fan-out/failover market-data registry.

    Providers are queried in configured priority order. A provider that returns
    401/402/403/429 or another DataUnavailable error is circuit-broken instead
    of being hammered every cycle. Successful providers are allowed to enrich
    partial MarketState objects returned by earlier providers.
    """

    def __init__(
        self,
        providers: Iterable[tuple[str, MarketDataProvider]],
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
    ) -> None:
        self.providers = list(providers)
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = max(1.0, cooldown_s)
        self.health: dict[str, ProviderHealth] = {name: ProviderHealth() for name, _ in self.providers}
        self.last_discovery_sources: list[str] = []
        self.last_state_sources: dict[str, list[str]] = {}

    def _available(self, name: str) -> bool:
        h = self.health[name]
        return time.monotonic() >= h.opened_until

    def _ok(self, name: str) -> None:
        h = self.health[name]
        h.failures = 0
        h.opened_until = 0.0
        h.last_error = None
        h.last_ok = time.monotonic()

    def _fail(self, name: str, exc: Exception) -> None:
        h = self.health[name]
        h.failures += 1
        h.last_error = str(exc)[:240]
        if h.failures >= self.failure_threshold:
            h.opened_until = time.monotonic() + self.cooldown_s
            log.warning("market provider %s circuit opened for %.0fs: %s", name, self.cooldown_s, h.last_error)
        else:
            log.warning("market provider %s failed (%d/%d): %s", name, h.failures, self.failure_threshold, h.last_error)

    def status(self) -> dict:
        now = time.monotonic()
        return {
            name: {
                "failures": h.failures,
                "circuit_open": now < h.opened_until,
                "cooldown_remaining_s": max(0.0, h.opened_until - now),
                "last_error": h.last_error,
                "last_ok_at": datetime.fromtimestamp(h.last_ok, timezone.utc).isoformat() if h.last_ok else None,
            }
            for name, h in self.health.items()
        }

    @property
    def name(self) -> str:
        return "multi-source(" + ",".join(name for name, _ in self.providers) + ")"

    async def discover_tokens(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        sources: list[str] = []
        errors: list[str] = []
        for name, provider in self.providers:
            if not self._available(name):
                continue
            try:
                tokens = await provider.discover_tokens()
                self._ok(name)
                sources.append(name)
                for token in tokens:
                    t = token.lower()
                    if t not in seen:
                        seen.add(t)
                        found.append(t)
            except DataUnavailable as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {exc}")
            except Exception as exc:  # provider isolation
                self._fail(name, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        self.last_discovery_sources = sources
        if not found and errors:
            raise DataUnavailable("all market-data providers unavailable; " + " | ".join(errors)[:1000])
        if not found:
            raise DataUnavailable("no token candidates discovered by configured market-data providers")
        return found

    @staticmethod
    def _merge(primary: MarketState, extra: MarketState) -> MarketState:
        data = primary.model_dump()
        extra_data = extra.model_dump()
        # The first provider owns non-null values. Later providers only fill gaps,
        # except for data_sources which is always unioned for auditability.
        for key, value in extra_data.items():
            if key == "data_sources":
                continue
            if data.get(key) is None and value is not None:
                data[key] = value
        data["data_sources"] = list(dict.fromkeys([*(primary.data_sources or []), *(extra.data_sources or [])]))
        return MarketState(**data)

    async def get_market_state(self, token_address: str) -> MarketState:
        merged: MarketState | None = None
        sources: list[str] = []
        errors: list[str] = []
        for name, provider in self.providers:
            if not self._available(name):
                continue
            try:
                state = await provider.get_market_state(token_address)
                self._ok(name)
                sources.append(name)
                merged = state if merged is None else self._merge(merged, state)
            except DataUnavailable as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {exc}")
            except Exception as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if merged is None:
            raise DataUnavailable("all market-data providers failed for " + token_address + ("; " + " | ".join(errors)[:800] if errors else ""))
        self.last_state_sources[token_address.lower()] = sources
        return merged

    async def aclose(self) -> None:
        for _, provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    log.exception("failed closing market-data provider %s", type(provider).__name__)
