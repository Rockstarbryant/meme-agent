
from __future__ import annotations
import asyncio, logging, time
from typing import Any, Awaitable, Callable
log = logging.getLogger("market_data.gateway")
class SharedMarketDataGateway:
    def __init__(self, *, redis=None, default_ttl_s=15.0, max_inflight=64):
        self.redis = redis
        self.default_ttl_s = default_ttl_s
        self._local_cache = {}
        self._inflight = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_inflight)
        self.metrics = {"hits": 0, "misses": 0, "provider_calls": 0, "deduped": 0}
    def _key(self, chain, address, kind="state"):
        return f"md:{kind}:{chain}:{address.lower()}"
    async def get(self, chain, address, fetch_fn, *, kind="state", ttl_s=None):
        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        key = self._key(chain, address, kind)
        now = time.monotonic()
        ent = self._local_cache.get(key)
        if ent and ent[0] > now:
            self.metrics["hits"] += 1
            return ent[1]
        async with self._lock:
            if key in self._inflight:
                fut = self._inflight[key]
                self.metrics["deduped"] += 1
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
                asyncio.create_task(self._do_fetch(key, fetch_fn, ttl, fut))
        return await fut
    async def _do_fetch(self, key, fetch_fn, ttl, fut):
        try:
            async with self._sem:
                self.metrics["provider_calls"] += 1
                self.metrics["misses"] += 1
                result = await fetch_fn()
            self._local_cache[key] = (time.monotonic() + ttl, result)
            if not fut.done(): fut.set_result(result)
        except Exception as e:
            if not fut.done(): fut.set_exception(e)
        finally:
            async with self._lock: self._inflight.pop(key, None)
    def clear(self): self._local_cache.clear()
