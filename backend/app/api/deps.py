from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import jwt
from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.security import decode_token
from app.chains.arc.adapter import ArcAdapter
from app.config import Settings
from app.db import models as M
from app.infra.redis import RateLimiter, is_revoked
from app.services.hub import EventHub


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sf: async_sessionmaker
    redis: Redis
    chain: ArcAdapter
    hub: EventHub
    limiter: RateLimiter
    jwt_key: str


def C(request: Request) -> Container:
    return request.app.state.c


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with C(request).sf() as s:
        yield s


def client_ip(request: Request) -> str:
    c = C(request)
    if c.settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def limit(request: Request, name: str, n: int, window: int = 60, extra: str = "") -> None:
    ok, _ = await C(request).limiter.hit(f"{name}:{client_ip(request)}:{extra}", n, window)
    if not ok:
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(window)})


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> M.User:
    c = C(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        claims = decode_token(auth[7:], c.jwt_key)
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    try:
        if await is_revoked(c.redis, claims["jti"]):
            raise HTTPException(401, "token revoked")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - if the revocation list is unreachable we fail closed
        raise HTTPException(503, "auth backend unavailable")
    user = await db.get(M.User, claims["sub"])
    if user is None or not user.is_active:
        raise HTTPException(401, "unknown or disabled user")
    await limit(request, "api", 300, 60, extra=user.id)
    request.state.claims = claims
    return user


async def current_runner(request: Request, db: AsyncSession = Depends(get_db)) -> M.Runner:
    """Runner credentials are separate from user JWTs and only work on /runner/* endpoints."""
    import hashlib
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer rt_"):
        raise HTTPException(401, "missing runner token")
    row = (await db.execute(select(M.Runner).where(M.Runner.token_hash == hashlib.sha256(auth[7:].encode()).hexdigest()))).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(401, "invalid or revoked runner token")
    user = await db.get(M.User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "user disabled")
    await limit(request, "runner", 900, 60, extra=row.id)
    return row
