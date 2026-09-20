"""Redis-backed primitives: idempotency, single-runner lease, rate limiting, SSE tickets, token revocation."""
from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict

from redis.asyncio import Redis

from app.events.bus import IdempotencyStore


class RedisIdempotencyStore(IdempotencyStore):
    """Atomic SET NX claim. Survives restarts and works across multiple backend instances."""

    def __init__(self, r: Redis, prefix: str = "idem:", ttl_s: int = 7 * 86400):
        self.r, self.p, self.ttl = r, prefix, ttl_s

    async def claim(self, key: str) -> bool:
        return bool(await self.r.set(self.p + key, "claimed", nx=True, ex=self.ttl))

    async def release(self, key: str) -> None:
        await self.r.delete(self.p + key)

    async def complete(self, key: str, result: dict) -> None:
        await self.r.set(self.p + key, json.dumps(result, default=str), ex=self.ttl)

    async def get(self, key: str) -> dict | None:
        v = await self.r.get(self.p + key)
        try:
            return json.loads(v) if v and v != "claimed" else None
        except ValueError:
            return None


_RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
_RENEW = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"


class RedisLease:
    """Only one process may actively trade for a given user, even with several backend replicas."""

    def __init__(self, r: Redis, name: str, ttl_ms: int = 30_000):
        self.r, self.key, self.ttl = r, f"lease:{name}", ttl_ms
        self.owner = secrets.token_hex(8)

    async def acquire(self) -> bool:
        return bool(await self.r.set(self.key, self.owner, nx=True, px=self.ttl))

    async def renew(self) -> bool:
        return bool(await self.r.eval(_RENEW, 1, self.key, self.owner, self.ttl))

    async def ensure(self) -> bool:
        return await self.renew() or await self.acquire()

    async def release(self) -> None:
        await self.r.eval(_RELEASE, 1, self.key, self.owner)


class RateLimiter:
    """Fixed-window limiter. Redis-backed; falls back to per-process counters if Redis is down."""

    def __init__(self, r: Redis | None):
        self.r = r
        self._local: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

    async def hit(self, key: str, limit: int, window_s: int) -> tuple[bool, int]:
        try:
            if self.r is None:
                raise ConnectionError
            bucket = f"rl:{key}:{int(time.time() // window_s)}"
            n = await self.r.incr(bucket)
            if n == 1:
                await self.r.expire(bucket, window_s + 1)
            return n <= limit, max(0, limit - n)
        except Exception:  # noqa: BLE001 - degrade to local counting, never to "unlimited"
            start, n = self._local[key]
            if time.time() - start > window_s:
                start, n = time.time(), 0
            n += 1
            self._local[key] = (start, n)
            return n <= limit, max(0, limit - n)


async def issue_ticket(r: Redis, user_id: str, ttl_s: int = 60) -> str:
    t = secrets.token_urlsafe(24)
    await r.set(f"ticket:{t}", user_id, ex=ttl_s)
    return t


async def consume_ticket(r: Redis, ticket: str) -> str | None:
    v = await r.getdel(f"ticket:{ticket}")  # single use
    return v.decode() if isinstance(v, bytes) else v


async def revoke_jti(r: Redis, jti: str, ttl_s: int) -> None:
    await r.set(f"revoked:{jti}", "1", ex=max(ttl_s, 1))


async def is_revoked(r: Redis, jti: str) -> bool:
    return bool(await r.exists(f"revoked:{jti}"))
