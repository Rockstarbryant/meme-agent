from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db
from app.core.types import TradingMode
from app.db import models as M
from app.db import repo
from app.services import control
from app.services.control import LIVE_CONFIRMATION_PHRASE

router = APIRouter(prefix="/agent", tags=["agent"])


class ModeIn(BaseModel):
    mode: TradingMode
    confirmation: str = ""


class EmergencyIn(BaseModel):
    enabled: bool


async def _status(request: Request, db: AsyncSession, user: M.User) -> dict:
    return await control.agent_status(db, C(request).settings, user)


async def _queue(db: AsyncSession, user_id: str, type_: str, payload: dict) -> M.RunnerCommand:
    existing = (await db.execute(select(M.RunnerCommand).where(M.RunnerCommand.user_id == user_id, M.RunnerCommand.type == type_,
                                                             M.RunnerCommand.status == "PENDING"))).scalars().all()
    for c in existing:
        if c.payload == payload:
            return c  # idempotent: don't queue the same close twice
    cmd = M.RunnerCommand(user_id=user_id, type=type_, payload=payload)
    db.add(cmd)
    await db.flush()
    return cmd


@router.get("")
async def get_agent(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _status(request, db, user)


@router.post("/start")
async def start(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await control.get_config(db, user.id)
    if cfg.emergency_stop:
        raise HTTPException(409, "emergency stop is active; disable it explicitly first")
    if await control.active_runner(db, user.id) is None:
        raise HTTPException(409, "Pair a Local Runner first: trading runs on your own machine, not on this server.")
    await control.bump(db, user.id, desired_state="RUNNING")
    await repo.audit(db, user.id, user.email, "AGENT_START", mode=user.mode)
    await db.commit()
    return await _status(request, db, user)


@router.post("/pause")
async def pause(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await control.bump(db, user.id, desired_state="PAUSED")
    await repo.audit(db, user.id, user.email, "AGENT_PAUSE")
    await db.commit()
    return await _status(request, db, user)


@router.post("/stop")
async def stop(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await control.bump(db, user.id, desired_state="STOPPED")
    await repo.audit(db, user.id, user.email, "AGENT_STOP")
    await db.commit()
    return {**await _status(request, db, user), "note": "New entries stopped. Open positions stay protected by your runner."}


@router.post("/emergency-stop")
async def emergency(body: EmergencyIn, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await control.bump(db, user.id, emergency_stop=body.enabled)
    await repo.audit(db, user.id, user.email, "EMERGENCY_STOP", enabled=body.enabled)
    await db.commit()
    return await _status(request, db, user)


@router.post("/mode")
async def set_mode(body: ModeIn, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if body.mode == TradingMode.LIVE:
        if body.confirmation != LIVE_CONFIRMATION_PHRASE:
            raise HTTPException(409, {"error": "CONFIRMATION_REQUIRED", "type_exactly": LIVE_CONFIRMATION_PHRASE})
        blockers = await control.live_blockers(db, C(request).settings, user.id)
        if blockers:
            await repo.audit(db, user.id, user.email, "MODE_SWITCH_REFUSED", target="LIVE", blockers=blockers)
            await db.commit()
            raise HTTPException(409, {"error": "LIVE_NOT_READY", "blockers": blockers})
    elif user.mode == "LIVE":
        n = (await db.execute(select(func.count()).select_from(M.Position).where(M.Position.user_id == user.id, M.Position.mode == "LIVE", M.Position.status == "OPEN"))).scalar_one()
        if n:
            raise HTTPException(409, "close open LIVE positions before switching to PAPER")
    before = user.mode
    user.mode = body.mode.value
    db.add(user)
    await control.bump(db, user.id, desired_state="STOPPED")  # a mode change always needs an explicit START
    await repo.audit(db, user.id, user.email, "MODE_SWITCH", before=before, after=body.mode.value)
    await db.commit()
    return await _status(request, db, user)


@router.post("/close/{position_id}")
async def close_position(position_id: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(M.Position, position_id)
    if p is None or p.user_id != user.id or p.status != "OPEN":
        raise HTTPException(404, "no such open position")
    cmd = await _queue(db, user.id, "CLOSE_POSITION", {"position_id": position_id})
    await repo.audit(db, user.id, user.email, "MANUAL_CLOSE_REQUESTED", position_id=position_id)
    await db.commit()
    return {"queued": True, "command_id": cmd.id, "note": "Your Local Runner executes this on its next heartbeat (seconds)."}


@router.post("/close-all")
async def close_all(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cmd = await _queue(db, user.id, "CLOSE_ALL", {})
    await repo.audit(db, user.id, user.email, "EMERGENCY_CLOSE_ALL_REQUESTED")
    await db.commit()
    return {"queued": True, "command_id": cmd.id}
