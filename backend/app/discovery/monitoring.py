"""Global token monitoring — independent of trade cycles and discovery.

Candidates are monitored according to priority:
  HOT  → frequent
  WARM → moderate
  COLD → slow
  DEAD → stop

Monitoring updates market snapshots, scores, and lifecycle state.

Tokens that repeatedly fail market-data fetch (no pool on any provider) are
marked DEAD after ``max_consecutive_failures`` so they stop consuming quota.
Each cycle processes at most ``max_per_cycle`` due tokens (HOT first).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.core.clock import utcnow
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus

log = logging.getLogger("monitoring")

_PRIORITY_RANK = {"HOT": 0, "WARM": 1, "COLD": 2, "DEAD": 9}


class MonitoringConfig(BaseModel):
    hot_interval_s: float = 30.0
    warm_interval_s: float = 120.0
    cold_interval_s: float = 600.0
    dead_after_s: float = 24 * 3600
    score_improve_threshold: float = 10.0
    score_decline_threshold: float = -15.0
    # Cap API pressure: only this many due tokens are refreshed per cycle.
    max_per_cycle: int = Field(default=12, ge=1, le=200)
    # After this many consecutive DataUnavailable fetches, mark DEAD.
    max_consecutive_failures: int = Field(default=5, ge=1, le=50)


class GlobalMonitoringService:
    LOCK_KEY = "global:monitoring:lock"

    def __init__(
        self,
        registry: GlobalTokenRegistry,
        *,
        redis: Any = None,
        config: MonitoringConfig | None = None,
        fetch_state_fn: Callable[[LaunchpadToken], Awaitable[dict]] | None = None,
        score_fn: Callable[[LaunchpadToken, dict], Awaitable[float]] | None = None,
        lock_ttl_s: int = 120,
        store: Any = None,
    ) -> None:
        self.registry = registry
        self.redis = redis
        self.config = config or MonitoringConfig()
        self._fetch = fetch_state_fn
        self._score = score_fn
        self.lock_ttl_s = lock_ttl_s
        self.store = store

    def _interval_for(self, priority: str) -> float:
        return {
            "HOT": self.config.hot_interval_s,
            "WARM": self.config.warm_interval_s,
            "COLD": self.config.cold_interval_s,
        }.get(priority, self.config.warm_interval_s)

    async def _acquire(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.set(self.LOCK_KEY, "1", nx=True, ex=self.lock_ttl_s))
        except Exception:
            return False

    async def _release(self) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(self.LOCK_KEY)
        except Exception:
            pass

    def _due_candidates(self, now) -> list[LaunchpadToken]:
        candidates = self.registry.list_by_status(
            TokenStatus.WATCHING,
            TokenStatus.IMPROVING,
            TokenStatus.QUALIFIED,
            TokenStatus.SCREENING,
            TokenStatus.DISCOVERED,
        )
        due: list[LaunchpadToken] = []
        for token in candidates:
            if token.priority == "DEAD":
                continue
            last = token.last_monitored_at
            interval = self._interval_for(token.priority)
            if last and (now - last).total_seconds() < interval:
                continue
            due.append(token)
        # HOT first, then WARM, then COLD; stable by token_key.
        due.sort(key=lambda t: (_PRIORITY_RANK.get(t.priority or "WARM", 5), t.token_key))
        return due[: self.config.max_per_cycle]

    def _mark_dead(self, token: LaunchpadToken, reason: str) -> None:
        token.priority = "DEAD"
        token.status = TokenStatus.REJECTED
        token.meta = dict(token.meta or {})
        token.meta["dead_reason"] = reason
        token.meta["monitor_failures"] = int(token.meta.get("monitor_failures") or 0)

    async def run_once(self) -> int:
        if not await self._acquire():
            return 0
        n = 0
        now = utcnow()
        try:
            due = self._due_candidates(now)
            if not due:
                return 0
            log.info("monitoring cycle: %d due tokens (cap=%d)", len(due), self.config.max_per_cycle)
            for token in due:
                if self._fetch is None:
                    continue
                try:
                    snap = await self._fetch(token)
                    token.last_monitored_at = now
                    token.meta = dict(token.meta or {})
                    token.meta["last_snapshot"] = snap
                    token.meta["monitor_failures"] = 0  # reset on success
                    if self._score is not None:
                        new_score = await self._score(token, snap)
                        old_score = token.current_score or token.initial_score
                        token.score_delta = (new_score - old_score) if old_score is not None else None
                        if token.initial_score is None:
                            token.initial_score = new_score
                        token.current_score = new_score
                        token.last_score_at = now
                        if token.score_delta is not None:
                            if token.score_delta >= self.config.score_improve_threshold:
                                token.status = TokenStatus.IMPROVING
                                token.priority = "HOT"
                            elif token.score_delta <= self.config.score_decline_threshold:
                                token.status = TokenStatus.LOST_MOMENTUM
                                token.priority = "COLD"
                        if token.status == TokenStatus.DISCOVERED:
                            token.status = TokenStatus.WATCHING
                    self.registry.upsert(token)
                    if self.store is not None:
                        try:
                            await self.store.upsert(token)
                        except Exception:
                            log.exception("persist monitored token")
                    n += 1
                except Exception as exc:
                    from app.core.errors import DataUnavailable

                    token.meta = dict(token.meta or {})
                    fails = int(token.meta.get("monitor_failures") or 0) + 1
                    token.meta["monitor_failures"] = fails
                    token.last_monitored_at = now  # backoff even on failure
                    if isinstance(exc, DataUnavailable):
                        # Log at most every few failures to avoid drowning logs.
                        if fails == 1 or fails >= self.config.max_consecutive_failures or fails % 3 == 0:
                            log.warning(
                                "monitor data unavailable for %s (fail %d/%d): %s",
                                token.token_key,
                                fails,
                                self.config.max_consecutive_failures,
                                exc,
                            )
                        if fails >= self.config.max_consecutive_failures:
                            self._mark_dead(token, f"data_unavailable_x{fails}")
                            log.info("marked DEAD %s after %d consecutive data failures", token.token_key, fails)
                    else:
                        log.exception("monitor failed for %s", token.token_key)
                        if fails >= self.config.max_consecutive_failures:
                            self._mark_dead(token, f"error_x{fails}:{type(exc).__name__}")
                    self.registry.upsert(token)
                    if self.store is not None and token.priority == "DEAD":
                        try:
                            await self.store.upsert(token)
                        except Exception:
                            log.exception("persist DEAD token %s", token.token_key)
            return n
        finally:
            await self._release()

    async def scheduler_loop(self, stop_event: asyncio.Event | None = None) -> None:
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                await self.run_once()
            except Exception:
                log.exception("monitoring cycle error")
            sleep_s = min(self.config.hot_interval_s, 15.0)
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_s)
