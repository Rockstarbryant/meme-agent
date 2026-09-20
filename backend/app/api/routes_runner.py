from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_runner, current_user, get_db, limit
from app.core.clock import utcnow
from app.db import models as M
from app.db import repo
from app.domain.runner_protocol import (Command, CommandAck, ConfigPoll, EventAck, EventBatch, Heartbeat, HeartbeatResponse, PairRequest,
                                        PairResponse)
from app.services import control
from app.services.ingest import ingest

user_router = APIRouter(prefix="/runners", tags=["runners"])
runner_router = APIRouter(prefix="/runner", tags=["runner (used by the Local Runner)"])
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_TTL_S = 600


def _norm(code: str) -> str:
    return code.replace("-", "").replace(" ", "").upper()


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ------------------------------------------------------------------ user (JWT) side
@user_router.post("/pairing-codes", status_code=201)
async def create_pairing_code(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await limit(request, "paircode", 5, 60, extra=user.id)
    if await control.active_runner(db, user.id):
        raise HTTPException(409, "You already have a paired runner. Revoke it first (one active runner per account).")
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    await C(request).redis.set(f"pair:{_hash(raw)}", user.id, ex=CODE_TTL_S)
    await repo.audit(db, user.id, user.email, "RUNNER_PAIRING_CODE_CREATED")
    await db.commit()
    return {"code": f"{raw[:4]}-{raw[4:8]}-{raw[8:]}", "expires_in": CODE_TTL_S,
            "command": "python -m runner pair --server <THIS_SERVER_URL> --code " + f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"}


@user_router.get("")
async def list_runners(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await control.get_config(db, user.id)
    rows = (await db.execute(select(M.Runner).where(M.Runner.user_id == user.id).order_by(M.Runner.created_at.desc()).limit(10))).scalars().all()
    return [{**(control.runner_info(r, cfg) or {}), "created_at": r.created_at, "revoked_at": r.revoked_at} for r in rows]


@user_router.delete("/{runner_id}")
async def revoke_runner(runner_id: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    r = await db.get(M.Runner, runner_id)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "not found")
    r.revoked_at = utcnow()
    await control.bump(db, user.id, desired_state="STOPPED")
    await repo.audit(db, user.id, user.email, "RUNNER_REVOKED", runner_id=runner_id)
    await db.commit()
    return {"revoked": True, "note": "The runner can no longer connect. Wallet/session access lives on your machine: also log out of the Circle CLI there if you retire it."}


# ------------------------------------------------------------------ runner side
@runner_router.post("/pair", response_model=PairResponse)
async def pair(body: PairRequest, request: Request, db: AsyncSession = Depends(get_db)):
    await limit(request, "pair", 10, 60)
    uid = await C(request).redis.getdel(f"pair:{_hash(_norm(body.code))}")  # single use
    if not uid:
        raise HTTPException(400, "invalid or expired pairing code")
    uid = uid.decode() if isinstance(uid, bytes) else uid
    if await control.active_runner(db, uid):
        raise HTTPException(409, "this account already has an active runner")
    token = "rt_" + secrets.token_urlsafe(32)
    r = M.Runner(user_id=uid, name=body.name.strip() or "runner", token_hash=_hash(token), version=body.version)
    db.add(r)
    await repo.audit(db, uid, "runner", "RUNNER_PAIRED", name=r.name)
    await db.commit()
    return PairResponse(runner_id=r.id, token=token)


@runner_router.get("/config", response_model=ConfigPoll)
async def get_config(request: Request, since: int = 0, wait: int = 0, runner: M.Runner = Depends(current_runner)):
    """Long-poll: returns as soon as the config version exceeds `since` (pause / emergency stop reach the runner in ~1s)."""
    c, wait = C(request), max(0, min(wait, 25))
    deadline = asyncio.get_event_loop().time() + wait
    while True:
        async with c.sf() as db:
            cfg = await control.get_config(db, runner.user_id)
            if cfg.version > since or wait == 0:
                user = await db.get(M.User, runner.user_id)
                bundle = await control.build_bundle(db, c.settings, user, runner)
                await db.commit()
                return ConfigPoll(changed=cfg.version > since, bundle=bundle)
        if asyncio.get_event_loop().time() >= deadline:
            return ConfigPoll(changed=False)
        await asyncio.sleep(0.4)


@runner_router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(hb: Heartbeat, runner: M.Runner = Depends(current_runner), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    runner.status = {**hb.model_dump(mode="json"), "received_at": now.isoformat()}
    runner.last_seen_at, runner.version = now, hb.version[:32]
    db.add(runner)
    cfg = await control.get_config(db, runner.user_id)
    cmds = (await db.execute(select(M.RunnerCommand).where(M.RunnerCommand.user_id == runner.user_id, M.RunnerCommand.status == "PENDING")
                             .order_by(M.RunnerCommand.created_at).limit(20))).scalars().all()
    await db.commit()
    return HeartbeatResponse(server_time=now, desired_config_version=cfg.version,
                             commands=[Command(id=c.id, type=c.type, payload=c.payload) for c in cmds])  # type: ignore[arg-type]


@runner_router.post("/commands/{cid}/ack")
async def ack_command(cid: str, body: CommandAck, runner: M.Runner = Depends(current_runner), db: AsyncSession = Depends(get_db)):
    c = await db.get(M.RunnerCommand, cid)
    if c is None or c.user_id != runner.user_id:
        raise HTTPException(404, "not found")
    c.status, c.detail, c.updated_at = body.status, body.detail, utcnow()
    await db.commit()
    return {"ok": True}


@runner_router.post("/events", response_model=EventAck)
async def post_events(batch: EventBatch, request: Request, runner: M.Runner = Depends(current_runner), db: AsyncSession = Depends(get_db)):
    return await ingest(db, C(request).hub, runner, batch)
