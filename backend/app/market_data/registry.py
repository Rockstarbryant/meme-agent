from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import MarketState
from app.observability.audit import RECORDER, AuditKind, AuditRecorder, AuditStatus, record_event

log = logging.getLogger("market_data")

# Housekeeping fields that are not "data a provider supplied" (excluded from field-level provenance).
_NOT_PROVENANCE = frozenset({"chain", "token_address", "timestamp", "data_sources", "field_sources", "is_demo", "scanned_at",
                             "enriched_at", "enrichment_gaps", "buy_sell_basis", "contract", "windows", "holder_basis"})


def filled_fields(state: MarketState) -> list[str]:
    """Names of the fields a provider actually populated (windows are listed per window: ``windows.1h``)."""
    out: list[str] = []
    for k, v in state.model_dump().items():
        if k in _NOT_PROVENANCE:
            continue
        if v is None or v == [] or v == {} or v is False:
            continue
        out.append(k)
    for w, ws in (state.windows or {}).items():
        if any(x is not None for x in ws.model_dump(exclude={"basis"}).values()):
            out.append(f"windows.{w}")
    for ck, cv in state.contract.model_dump(exclude={"checks_run", "verification_source", "sell_check_method"}).items():
        if cv is not None:
            out.append(f"contract.{ck}")
    return out

# Fields the risk engine / strategy actually gate decisions on (see
# app/risk/engine.py and app/strategies/traction_momentum.py). Once every
# provider we've already queried has filled these, later providers in the
# chain are purely optional enrichment and are skipped for this call. This is
# what keeps an unstable/quota-limited *last* provider (e.g. a paid Bitquery
# plan that started rejecting requests) from being hit on every single token
# lookup once the free sources already answered the question.
#
# ``unique_buyers_5m`` is part of the set because the strategy's ``buyer_growth`` component (a REQUIRED gate)
# is missing without it, and only some providers supply it. The buy/sell ratio is checked through
# ``buy_sell_volume_ratio()`` so a count-based ratio counts as present.
_CORE_FIELDS = ("price", "liquidity", "volume_5m", "price_change_5m", "unique_buyers_5m")


def _is_complete(state: MarketState) -> bool:
    return all(getattr(state, f, None) is not None for f in _CORE_FIELDS) and state.buy_sell_volume_ratio() is not None


@dataclass
class ProviderHealth:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str | None = None
    last_ok: float | None = None

    def to_dict(self) -> dict:
        return {"failures": self.failures, "opened_until": self.opened_until, "last_error": self.last_error, "last_ok": self.last_ok}

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderHealth":
        return cls(failures=int(d.get("failures", 0)), opened_until=float(d.get("opened_until", 0.0)),
                    last_error=d.get("last_error"), last_ok=d.get("last_ok"))


class MarketDataRegistry(MarketDataProvider):
    """Fan-out/failover market-data registry.

    Providers are queried in configured priority order. A provider that returns
    401/402/403/429 or another DataUnavailable error is circuit-broken instead
    of being hammered every cycle. Successful providers are allowed to enrich
    partial MarketState objects returned by earlier providers. Once the merged
    state already has every field the risk/strategy engine needs, remaining
    (lower-priority, "optional enrichment") providers are skipped for that call.

    Health is process-local by default (``time.monotonic()``-based), which is
    correct for a long-lived local runner. A shared cloud worker builds a fresh
    ``RunnerRuntime`` — and therefore a fresh registry — every cycle; pass
    ``on_health_change``/an initial ``health`` snapshot (see runner/runtime.py)
    so a provider that is actively erroring stays circuit-broken across cycles
    instead of being retried from a clean slate every few seconds.
    """

    def __init__(
        self,
        providers: Iterable[tuple[str, MarketDataProvider]],
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
        essential: Iterable[str] = (),
        initial_health: dict[str, dict] | None = None,
        on_health_change: Callable[[dict[str, dict]], None] | None = None,
        audit: AuditRecorder | None = None,
    ) -> None:
        self.audit = audit or RECORDER
        self.providers = list(providers)
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = max(1.0, cooldown_s)
        # "Essential" providers (typically the free, always-required on-chain
        # sources) get the normal failure_threshold before circuit-breaking.
        # Everything else is treated as optional enrichment: a single failure
        # is enough to circuit-break it, so a flaky/quota-limited optional
        # source (bitquery) never gets hammered every cycle once it's failing.
        self._essential = set(essential)
        self.health: dict[str, ProviderHealth] = {name: ProviderHealth() for name, _ in self.providers}
        if initial_health:
            for name, snap in initial_health.items():
                if name in self.health:
                    self.health[name] = ProviderHealth.from_dict(snap)
        self._on_health_change = on_health_change
        self.last_discovery_sources: list[str] = []
        self.last_state_sources: dict[str, list[str]] = {}

    def _audit(self, provider: str, operation: str, status: AuditStatus, *, latency_ms: float | None = None, error: str = "",
               token: str = "", detail: dict | None = None) -> None:
        h = self.health.get(provider)
        record_event(AuditKind.MARKET_PROVIDER, status, provider=provider, operation=operation, component="market_registry",
                     latency_ms=latency_ms, error=error, token_key=f"arc:{token.lower()}" if token else "",
                     detail={**(detail or {}), **({"consecutive_failures": h.failures} if h and h.failures else {})},
                     recorder=self.audit)

    def _threshold(self, name: str) -> int:
        return self.failure_threshold if (not self._essential or name in self._essential) else 1

    def _persist(self) -> None:
        if self._on_health_change is not None:
            try:
                self._on_health_change({name: h.to_dict() for name, h in self.health.items()})
            except Exception:
                log.exception("failed persisting market-data provider health")

    def _available(self, name: str) -> bool:
        h = self.health[name]
        return time.monotonic() >= h.opened_until

    def _ok(self, name: str) -> None:
        h = self.health[name]
        was_open = h.failures > 0 or h.opened_until > 0
        h.failures = 0
        h.opened_until = 0.0
        h.last_error = None
        h.last_ok = time.monotonic()
        if was_open:
            self._persist()

    def _fail(self, name: str, exc: Exception) -> None:
        h = self.health[name]
        h.failures += 1
        h.last_error = str(exc)[:240]
        if h.failures >= self._threshold(name):
            h.opened_until = time.monotonic() + self.cooldown_s
            log.warning("market provider %s circuit opened for %.0fs: %s", name, self.cooldown_s, h.last_error)
        else:
            log.warning("market provider %s failed (%d/%d): %s", name, h.failures, self._threshold(name), h.last_error)
        self._persist()

    def status(self) -> dict:
        now = time.monotonic()
        return {
            name: {
                "failures": h.failures,
                "circuit_open": now < h.opened_until,
                "cooldown_remaining_s": max(0.0, h.opened_until - now),
                "last_error": h.last_error,
                "last_ok_at": datetime.fromtimestamp(h.last_ok, timezone.utc).isoformat() if h.last_ok else None,
                "essential": (not self._essential) or name in self._essential,
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
                self._audit(name, "discover_tokens", AuditStatus.SKIPPED, error="circuit open (cooling down after failures)",
                            detail={"cooldown_remaining_s": round(max(0.0, self.health[name].opened_until - time.monotonic()), 1)})
                continue
            t0 = time.monotonic()
            try:
                tokens = await provider.discover_tokens()
                self._ok(name)
                sources.append(name)
                self._audit(name, "discover_tokens", AuditStatus.OK, latency_ms=(time.monotonic() - t0) * 1000,
                            detail={"tokens": len(tokens)})
                for token in tokens:
                    t = token.lower()
                    if t not in seen:
                        seen.add(t)
                        found.append(t)
            except DataUnavailable as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {exc}")
                self._audit(name, "discover_tokens", AuditStatus.FAILED, latency_ms=(time.monotonic() - t0) * 1000, error=str(exc))
            except Exception as exc:  # provider isolation
                self._fail(name, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self._audit(name, "discover_tokens", AuditStatus.FAILED, latency_ms=(time.monotonic() - t0) * 1000,
                            error=f"{type(exc).__name__}: {exc}")
        self.last_discovery_sources = sources
        if not found and errors:
            raise DataUnavailable("all market-data providers unavailable; " + " | ".join(errors)[:1000])
        if not found:
            raise DataUnavailable("no token candidates discovered by configured market-data providers")
        return found

    # Prefer a real trade-history USD split over count-based estimates.
    _VOLUME_SPLIT_FIELDS = ("buy_volume_5m", "sell_volume_5m", "buy_sell_basis")

    @staticmethod
    def _merge(primary: MarketState, extra: MarketState) -> MarketState:
        data = primary.model_dump()
        extra_data = extra.model_dump()
        extra_sources = extra.data_sources or []
        real_split = extra.buy_sell_basis == "usd" or (
            any("geckoterminal" in str(s) for s in extra_sources) and extra.buy_volume_5m is not None
        )
        for key, value in extra_data.items():
            if key == "data_sources":
                continue
            if key == "contract":
                # Field-wise: a later provider may know one contract fact the earlier one did not.
                merged_c = dict(data.get("contract") or {})
                for ck, cv in (value or {}).items():
                    if cv in (None, []):
                        continue
                    if merged_c.get(ck) in (None, []):
                        merged_c[ck] = cv
                data["contract"] = merged_c
                continue
            if key == "windows":
                # Field-wise per window: a later provider may add a window (or a missing field of one) the earlier
                # provider did not have. Earlier providers keep priority for fields they already filled.
                merged_w = {k: dict(v) for k, v in (data.get("windows") or {}).items()}
                for wk, wv in (value or {}).items():
                    cur = merged_w.setdefault(wk, {})
                    for fk, fv in (wv or {}).items():
                        if fv is not None and cur.get(fk) is None:
                            cur[fk] = fv
                data["windows"] = merged_w
                continue
            if key in ("holder_growth", "liquidity_growth", "market_cap_growth"):
                data[key] = {**(value or {}), **(data.get(key) or {})}
                continue
            if value is None or value == [] or value == {}:
                continue
            if key in MarketDataRegistry._VOLUME_SPLIT_FIELDS and real_split:
                data[key] = value
                continue
            if data.get(key) is None or data.get(key) == []:
                data[key] = value
            elif key == "enrichment_gaps":
                data[key] = list(dict.fromkeys([*data[key], *value]))
        data["data_sources"] = list(dict.fromkeys([*(primary.data_sources or []), *(extra.data_sources or [])]))
        return MarketState(**data)

    async def get_market_state(self, token_address: str) -> MarketState:
        merged: MarketState | None = None
        sources: list[str] = []
        errors: list[str] = []
        field_sources: dict[str, str] = {}
        for name, provider in self.providers:
            if merged is not None and _is_complete(merged):
                # Every field the strategy/risk engine needs is already filled by
                # higher-priority providers. Remaining providers in the chain are
                # optional enrichment (e.g. bitquery) — do not spend a request on
                # them, and do not let them count as a source of truth or trip
                # their own circuit breaker on an unrelated quota issue.
                break
            if not self._available(name):
                self._audit(name, "get_market_state", AuditStatus.SKIPPED, token=token_address,
                            error="circuit open (cooling down after failures)",
                            detail={"cooldown_remaining_s": round(max(0.0, self.health[name].opened_until - time.monotonic()), 1)})
                continue
            t0 = time.monotonic()
            try:
                state = await provider.get_market_state(token_address)
                latency = (time.monotonic() - t0) * 1000
                self._ok(name)
                sources.append(name)
                if not state.data_sources:
                    # Provenance: a provider that does not label its own fields is still recorded by name.
                    state = state.model_copy(update={"data_sources": [name]})
                # Derive per-provider so a real USD split from a later provider can still override an estimate.
                state.derive_buy_sell()
                got = filled_fields(state)
                for f in got:
                    # Earlier providers keep priority for a field they already filled (see _merge).
                    if f not in field_sources:
                        field_sources[f] = name
                if merged is not None and (state.buy_sell_basis == "usd"):
                    for f in self._VOLUME_SPLIT_FIELDS:
                        if f in got:
                            field_sources[f] = name
                merged = state if merged is None else self._merge(merged, state)
                self._audit(name, "get_market_state", AuditStatus.OK, latency_ms=latency, token=token_address,
                            detail={"fields_supplied": len(got)})
            except DataUnavailable as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {exc}")
                self._audit(name, "get_market_state", AuditStatus.FAILED, latency_ms=(time.monotonic() - t0) * 1000,
                            token=token_address, error=str(exc))
            except Exception as exc:
                self._fail(name, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self._audit(name, "get_market_state", AuditStatus.FAILED, latency_ms=(time.monotonic() - t0) * 1000,
                            token=token_address, error=f"{type(exc).__name__}: {exc}")
        if merged is None:
            self._audit("*", "get_market_state", AuditStatus.FAILED, token=token_address,
                        error="all market-data providers failed", detail={"errors": errors[:6]})
            raise DataUnavailable("all market-data providers failed for " + token_address + ("; " + " | ".join(errors)[:800] if errors else ""))
        if errors:
            # Someone failed but the chain still produced a state: say which provider was bypassed.
            self._audit("*", "get_market_state", AuditStatus.DEGRADED, token=token_address,
                        error="answered via fallback after provider failure(s)",
                        detail={"answered_by": sources, "failed": errors[:6]})
        self.last_state_sources[token_address.lower()] = sources
        merged.field_sources = {**merged.field_sources, **field_sources}
        merged.derive_buy_sell()
        merged.scanned_at = datetime.now(timezone.utc)
        return merged

    async def aclose(self) -> None:
        for _, provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    log.exception("failed closing market-data provider %s", type(provider).__name__)