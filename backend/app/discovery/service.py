
from __future__ import annotations
import asyncio, logging
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable
from pydantic import BaseModel, Field
from app.core.clock import utcnow
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus
from datetime import timezone

def _parse_dt(v):
    if v is None: return None
    if isinstance(v, datetime): return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

log = logging.getLogger("discovery")
DEFAULT_DISCOVERY_INTERVAL_S = 3 * 3600
DEFAULT_LOOKBACK_S = 15 * 60
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
    def __init__(self, registry, *, redis=None, config=None, discover_fn=None, screen_fn=None, lock_ttl_s=600):
        self.registry = registry
        self.redis = redis
        self.config = config or DiscoveryConfig()
        self._discover_fn = discover_fn
        self._screen_fn = screen_fn
        self.lock_ttl_s = lock_ttl_s
        self.store = store
        self.checkpoint = DiscoveryCheckpoint()
    async def load_checkpoint(self):
        if self.redis is None: return self.checkpoint
        try:
            raw = await self.redis.get(self.CHECKPOINT_KEY)
            if raw: self.checkpoint = DiscoveryCheckpoint.model_validate_json(raw)
        except Exception: log.exception("load checkpoint")
        return self.checkpoint
    async def save_checkpoint(self):
        if self.redis is None: return
        try: await self.redis.set(self.CHECKPOINT_KEY, self.checkpoint.model_dump_json())
        except Exception: log.exception("save checkpoint")
    async def _acquire_lock(self):
        if self.redis is None: return True
        try: return bool(await self.redis.set(self.LOCK_KEY, "1", nx=True, ex=self.lock_ttl_s))
        except Exception: return False
    async def _release_lock(self):
        if self.redis is None: return
        try: await self.redis.delete(self.LOCK_KEY)
        except Exception: pass
    def _window(self, now):
        if self.checkpoint.last_success_at:
            start = self.checkpoint.last_success_at - timedelta(seconds=self.config.lookback_s)
        else:
            start = now - timedelta(seconds=self.config.interval_s + self.config.lookback_s)
        return start, now
    async def run_once(self):
        now = utcnow()
        await self.load_checkpoint()
        if not await self._acquire_lock():
            log.info("discovery skipped: lock held")
            return 0
        found = 0
        try:
            self.checkpoint.last_attempt_at = now
            start, end = self._window(now)
            self.checkpoint.last_window_start, self.checkpoint.last_window_end = start, end
            raw = []
            if self._discover_fn: raw = await self._discover_fn(start, end)
            for row in raw:
                chain = row.get("chain") or "arc"
                addr = (row.get("token_address") or row.get("address") or "").lower()
                if not addr: continue
                token = LaunchpadToken(chain=chain, token_address=addr, launchpad=row.get("launchpad"),
                    launchpad_contract=row.get("launchpad_contract"), launch_event=row.get("launch_event"),
                    launch_tx_hash=row.get("launch_tx_hash"), creator_address=row.get("creator_address"),
                    launched_at=row.get("launched_at"), discovered_at=now, symbol=row.get("symbol"),
                    name=row.get("name"), status=TokenStatus.DISCOVERED, meta=row.get("meta") or {})
                token = self.registry.upsert(token)
                if self._screen_fn:
                    try: token = await self._screen_fn(token); self.registry.upsert(token)
                    except Exception: log.exception("screen")
                found += 1
            self.checkpoint.last_success_at = now
            self.checkpoint.tokens_found = found
            self.checkpoint.error = None
            await self.save_checkpoint()
            return found
        except Exception as e:
            self.checkpoint.error = f"{type(e).__name__}: {e}"[:300]
            await self.save_checkpoint()
            raise
        finally:
            await self._release_lock()
    async def scheduler_loop(self, stop_event=None):
        while True:
            if stop_event and stop_event.is_set(): break
            try: await self.run_once()
            except Exception: pass
            sleep_s = max(60.0, self.config.interval_s)
            if stop_event:
                try: await asyncio.wait_for(stop_event.wait(), timeout=sleep_s); break
                except asyncio.TimeoutError: pass
            else: await asyncio.sleep(sleep_s)
