from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db, limit
from app.api.security import hash_password, make_token, verify_password
from app.db import models as M
from app.db import repo
from app.chains.arc import deployments
from app.chains.arc.network import NETWORKS, wallet_chain_params
from app.infra.redis import revoke_jti

router = APIRouter()
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=256)


def _user_out(u: M.User) -> dict:
    return {"id": u.id, "email": u.email, "mode": u.mode, "created_at": u.created_at}


def _token_out(c, u: M.User) -> dict:
    tok, ttl = make_token(u.id, c.jwt_key, c.settings.access_token_minutes)
    return {"access_token": tok, "token_type": "bearer", "expires_in": ttl, "user": _user_out(u)}


@router.post("/auth/register", tags=["auth"], status_code=201)
async def register(body: Credentials, request: Request, db: AsyncSession = Depends(get_db)):
    c = C(request)
    if not c.settings.registration_enabled:
        raise HTTPException(403, "registration is disabled")
    await limit(request, "register", 5, 60)
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(422, "invalid email")
    if len(body.password) < c.settings.min_password_length:
        raise HTTPException(422, f"password must be at least {c.settings.min_password_length} characters")
    if (await db.execute(select(M.User).where(M.User.email == email))).scalar_one_or_none():
        raise HTTPException(409, "email already registered")
    u = M.User(email=email, password_hash=hash_password(body.password), mode="PAPER")
    db.add(u)
    await db.flush()
    from app.services import control
    await control.get_config(db, u.id)
    await repo.audit(db, u.id, email, "REGISTER")
    await db.commit()
    return _token_out(c, u)


@router.post("/auth/login", tags=["auth"])
async def login(body: Credentials, request: Request, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    await limit(request, "login", 10, 60, extra=email)
    u = (await db.execute(select(M.User).where(M.User.email == email))).scalar_one_or_none()
    ok = verify_password(body.password, u.password_hash if u else None)
    if not (ok and u and u.is_active):
        raise HTTPException(401, "invalid credentials")
    return _token_out(C(request), u)


@router.post("/auth/logout", tags=["auth"])
async def logout(request: Request, user: M.User = Depends(current_user)):
    cl = request.state.claims
    await revoke_jti(C(request).redis, cl["jti"], int(cl["exp"] - time.time()))
    return {"ok": True}


@router.get("/auth/me", tags=["auth"])
async def me(user: M.User = Depends(current_user)):
    return _user_out(user)


@router.get("/health/live", tags=["system"])
async def live():
    return {"status": "alive"}


@router.get("/health", tags=["system"])
async def health(request: Request):
    c, out = C(request), {}
    try:
        async with c.sf() as db:
            await db.execute(text("select 1"))
        out["database"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        out["database"] = {"ok": False, "error": type(e).__name__}
    try:
        out["redis"] = {"ok": bool(await c.redis.ping())}
    except Exception as e:  # noqa: BLE001
        out["redis"] = {"ok": False, "error": type(e).__name__}
    h = await c.chain.health()
    out["arc_rpc"] = {"ok": h.ok, "chain_id": h.chain_id, "block": h.block_number, "latency_ms": h.latency_ms, "detail": h.detail}
    out["live_trading"] = {"enabled": c.settings.live_trading_enabled, "arc_integration_verified": c.chain.live_trading_verified}
    ok = out["database"]["ok"] and out["redis"]["ok"]  # RPC being down degrades the agent, not the API
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok" if ok else "degraded", **out}, status_code=200 if ok else 503)


@router.get("/chains", tags=["system"])
async def chains(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    c = C(request)
    rows = (await db.execute(select(M.Chain))).scalars().all()
    h = await c.chain.health()
    return [{"id": r.id, "name": r.name, "chain_id": r.chain_id, "enabled": r.enabled, "paused": r.paused,
             "live_trading_verified": c.chain.live_trading_verified if r.id == "arc" else False,
             "network_status": {"ok": h.ok, "block": h.block_number, "detail": h.detail} if r.id == "arc" else None,
             "explorer_url": c.settings.arc_explorer_url or None,
             "network": c.settings.arc_network if r.id == "arc" else None,
             "wallet_chain_params": wallet_chain_params(NETWORKS[c.settings.arc_network]) if r.id == "arc" else None,
             "deployments": deployments.describe() if r.id == "arc" else None} for r in rows]


@router.get("/launchpads", tags=["system"])
async def launchpads(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.Launchpad))).scalars().all()
    return {"launchpads": [{"id": r.id, "chain": r.chain_id, "name": r.name, "verified": r.verified, "enabled": r.enabled,
                            "descriptor": r.descriptor} for r in rows],
            "note": "No launchpad is enabled until verified against official docs and on-chain (see docs/launchpads.md)."}
