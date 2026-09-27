"""Global launch discovery service.

Runs on its own schedule (default DISCOVERY_INTERVAL = 3 hours).
Uses a Redis distributed lock so multiple worker replicas never run the same job.
Idempotent: does not duplicate tokens. Checkpoint advances only on success.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.core.clock import utcnow
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus

log = logging.getLogger("discovery")

DEFAULT_DISCOVERY_INTERVAL_S = 3 * 3600
DEFAULT_LOOKBACK_S = 15 * 60


def _parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class DiscoveryCheckpoint(BaseModel):
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_window_start: datetime | None = None
    last_window_end: datetime | None = None
    tokens_found: int = 0
    error: str | None = None


class DiscoveryConfig(BaseModel):
    interval_s: float = DEFAULT_DISCOVERY_INTERVAL_S
    lookback_s: float = DEFAULT_LOOKBACK_S
    enabled_launchpads: list[str] = Field(default_factory=list)
    min_market_cap: float = 0.0
    min_liquidity: float = 0.0
    min_volume: float = 0.0
    candidate_score_threshold: float = 40.0


class GlobalDiscoveryService:
    LOCK_KEY = "global:discovery:lock"
    CHECKPOINT_KEY = "global:discovery:checkpoint"

    def __init__(
        self,
        registry: GlobalTokenRegistry,
        *,
        redis: Any = None,
        config: DiscoveryConfig | None = None,
        discover_fn: Callable[[datetime, datetime], Awaitable[list[dict]]] | None = None,
        screen_fn: Callable[[LaunchpadToken], Awaitable[LaunchpadToken]] | None = None,
        lock_ttl_s: int = 600,
        store: Any = None,
    ) -> None:
        self.registry = registry
        self.redis = redis
        self.config = config or DiscoveryConfig()
        self._discover_fn = discover_fn
        self._screen_fn = screen_fn
        self.lock_ttl_s = lock_ttl_s
        self.store = store
        self.checkpoint = DiscoveryCheckpoint()

    async def load_checkpoint(self) -> DiscoveryCheckpoint:
        if self.store is not None:
            try:
                data = await self.store.load_checkpoint("global")
                if data:
                    self.checkpoint = DiscoveryCheckpoint(
                        last_success_at=_parse_dt(data.get("last_success_at")),
                        last_attempt_at=_parse_dt(data.get("last_attempt_at")),
                        last_window_start=_parse_dt(data.get("last_window_start")),
                        last_window_end=_parse_dt(data.get("last_window_end")),
                        tokens_found=int(data.get("tokens_found") or 0),
                        error=data.get("error"),
                    )
                    return self.checkpoint
            except Exception:
                log.exception("store load_checkpoint failed")
        if self.redis is not None:
            try:
                raw = await self.redis.get(self.CHECKPOINT_KEY)
                if raw:
                    self.checkpoint = DiscoveryCheckpoint.model_validate_json(raw)
            except Exception:
                log.exception("failed loading discovery checkpoint")
        return self.checkpoint

    async def save_checkpoint(self) -> None:
        data = self.checkpoint.model_dump(mode="json")
        if self.store is not None:
            try:
                await self.store.save_checkpoint("global", data)
            except Exception:
                log.exception("store save_checkpoint failed")
        if self.redis is not None:
            try:
                await self.redis.set(self.CHECKPOINT_KEY, self.checkpoint.model_dump_json())
            except Exception:
                log.exception("failed saving discovery checkpoint")

    async def _acquire_lock(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.set(self.LOCK_KEY, "1", nx=True, ex=self.lock_ttl_s))
        except Exception:
            log.exception("discovery lock acquire failed")
            return False

    async def _release_lock(self) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(self.LOCK_KEY)
        except Exception:
            log.exception("discovery lock release failed")

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        if self.checkpoint.last_success_at:
            start = self.checkpoint.last_success_at - timedelta(seconds=self.config.lookback_s)
        else:
            start = now - timedelta(seconds=self.config.interval_s + self.config.lookback_s)
        return start, now

    async def run_once(self) -> int:
        now = utcnow()
        await self.load_checkpoint()
        if not await self._acquire_lock():
            log.info("discovery skipped: another worker holds the lock")
            return 0
        found = 0
        try:
            self.checkpoint.last_attempt_at = now
            start, end = self._window(now)
            self.checkpoint.last_window_start = start
            self.checkpoint.last_window_end = end
            log.info("global discovery window %s → %s", start.isoformat(), end.isoformat())

            raw_tokens: list[dict] = []
            if self._discover_fn is not None:
                raw_tokens = await self._discover_fn(start, end)
            else:
                log.warning("no discover_fn configured; discovery is a no-op")

            for row in raw_tokens:
                chain = row.get("chain") or "arc"
                addr = (row.get("token_address") or row.get("address") or "").lower()
                if not addr:
                    continue
                token = LaunchpadToken(
                    chain=chain,
                    token_address=addr,
                    launchpad=row.get("launchpad"),
                    launchpad_contract=row.get("launchpad_contract"),
                    launch_event=row.get("launch_event"),
                    launch_tx_hash=row.get("launch_tx_hash"),
                    creator_address=row.get("creator_address"),
                    launched_at=row.get("launched_at"),
                    discovered_at=now,
                    symbol=row.get("symbol"),
                    name=row.get("name"),
                    status=TokenStatus.DISCOVERED,
                    meta=row.get("meta") or {},
                )
                token = self.registry.upsert(token)
                if self.store is not None:
                    try:
                        await self.store.upsert(token)
                    except Exception:
                        log.exception("persist discovered token")
                if self._screen_fn is not None:
                    try:
                        token = await self._screen_fn(token)
                        self.registry.upsert(token)
                    except Exception:
                        log.exception("screening failed for %s", token.token_key)
                found += 1

            self.checkpoint.last_success_at = now
            self.checkpoint.tokens_found = found
            self.checkpoint.error = None
            await self.save_checkpoint()
            log.info("global discovery completed: %d tokens", found)
            return found
        except Exception as e:
            self.checkpoint.error = f"{type(e).__name__}: {e}"[:300]
            await self.save_checkpoint()
            log.exception("global discovery failed")
            raise
        finally:
            await self._release_lock()

    async def scheduler_loop(self, stop_event: asyncio.Event | None = None) -> None:
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                await self.run_once()
            except Exception:
                pass
            sleep_s = max(60.0, self.config.interval_s)
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_s)