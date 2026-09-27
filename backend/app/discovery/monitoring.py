
from __future__ import annotations
import asyncio, logging
from datetime import datetime
from typing import Any, Awaitable, Callable
from pydantic import BaseModel
from app.core.clock import utcnow
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus
log = logging.getLogger("monitoring")
class MonitoringConfig(BaseModel):
    hot_interval_s: float = 30.0
    warm_interval_s: float = 120.0
    cold_interval_s: float = 600.0
    dead_after_s: float = 86400.0
    score_improve_threshold: float = 10.0
    score_decline_threshold: float = -15.0
class GlobalMonitoringService:
    LOCK_KEY = "global:monitoring:lock"
    def __init__(self, registry, *, redis=None, config=None, fetch_state_fn=None, score_fn=None, lock_ttl_s=120):
        self.registry = registry
        self.redis = redis
        self.config = config or MonitoringConfig()
        self._fetch = fetch_state_fn
        self._score = score_fn
        self.lock_ttl_s = lock_ttl_s
        self.store = store
    def _interval_for(self, priority):
        return {"HOT": self.config.hot_interval_s, "WARM": self.config.warm_interval_s, "COLD": self.config.cold_interval_s}.get(priority, self.config.warm_interval_s)
    async def _acquire(self):
        if self.redis is None: return True
        try: return bool(await self.redis.set(self.LOCK_KEY, "1", nx=True, ex=self.lock_ttl_s))
        except Exception: return False
    async def _release(self):
        if self.redis is None: return
        try: await self.redis.delete(self.LOCK_KEY)
        except Exception: pass
    async def run_once(self):
        if not await self._acquire(): return 0
        n = 0
        now = utcnow()
        try:
            candidates = self.registry.list_by_status(TokenStatus.WATCHING, TokenStatus.IMPROVING, TokenStatus.QUALIFIED, TokenStatus.SCREENING, TokenStatus.DISCOVERED)
            for token in candidates:
                if token.priority == "DEAD": continue
                last = token.last_monitored_at
                if last and (now - last).total_seconds() < self._interval_for(token.priority): continue
                if self._fetch is None: continue
                try:
                    snap = await self._fetch(token)
                    token.last_monitored_at = now
                    token.meta["last_snapshot"] = snap
                    if self._score is not None:
                        new_score = await self._score(token, snap)
                        old = token.current_score or token.initial_score
                        token.score_delta = (new_score - old) if old is not None else None
                        if token.initial_score is None: token.initial_score = new_score
                        token.current_score = new_score
                        token.last_score_at = now
                        if token.score_delta is not None:
                            if token.score_delta >= self.config.score_improve_threshold:
                                token.status = TokenStatus.IMPROVING; token.priority = "HOT"
                            elif token.score_delta <= self.config.score_decline_threshold:
                                token.status = TokenStatus.LOST_MOMENTUM; token.priority = "COLD"
                        if token.status == TokenStatus.DISCOVERED: token.status = TokenStatus.WATCHING
                    self.registry.upsert(token)
                    if self.store is not None:
                        try:
                            # fetch_state_fn already writes snapshot via store when wired
                            await self.store.upsert(token)
                        except Exception:
                            log.exception("persist monitored token")
                    n += 1
                except Exception: log.exception("monitor %s", token.token_key)
            return n
        finally: await self._release()
    async def scheduler_loop(self, stop_event=None):
        while True:
            if stop_event and stop_event.is_set(): break
            try: await self.run_once()
            except Exception: log.exception("mon cycle")
            sleep_s = min(self.config.hot_interval_s, 15.0)
            if stop_event:
                try: await asyncio.wait_for(stop_event.wait(), timeout=sleep_s); break
                except asyncio.TimeoutError: pass
            else: await asyncio.sleep(sleep_s)
