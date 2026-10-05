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
    if getattr(cfg, "execution_mode", "self_hosted") == "cloud_managed":
        # The shared cloud worker creates the account's runner row on its first heartbeat, so there is nothing to
        # pair; PAPER needs no wallet. LIVE readiness is enforced separately (control.live_blockers).
        if not getattr(C(request).settings, "cloud_managed_enabled", False):
            raise HTTPException(409, "The cloud runner is disabled on this server. Choose the Local runner instead.")
    elif await control.active_runner(db, user.id, cfg) is None:
        raise HTTPException(409, "Pair a Local Runner first (or choose the Cloud runner): this account has no runner to execute the agent.")
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


class ExecutionModeIn(BaseModel):
    mode: str   # "cloud_managed" | "self_hosted"


@router.post("/execution-mode")
async def set_execution_mode(body: ExecutionModeIn, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Explicit choice between the managed Cloud runner and the user's own Local runner."""
    if body.mode not in ("cloud_managed", "self_hosted"):
        raise HTTPException(422, "mode must be 'cloud_managed' or 'self_hosted'")
    cfg = await control.get_config(db, user.id)
    current = getattr(cfg, "execution_mode", "self_hosted")
    if body.mode == current:
        return await _status(request, db, user)
    s = C(request).settings
    if body.mode == "cloud_managed":
        if not getattr(s, "cloud_managed_enabled", False):
            raise HTTPException(403, "The cloud runner is not enabled on this server")
        if user.mode == "LIVE":
            w = await control.load_wallet_row(db, user.id)
            if w is None or w.provider != "privy" or not w.external_id:
                raise HTTPException(409, "LIVE on the cloud runner needs a Privy cloud wallet. Create it on the Wallet page first, or switch to PAPER.")
    # Each runner keeps its own portfolio, so switching with open positions would strand them on the old runner.
    open_n = (await db.execute(select(func.count()).select_from(M.Position).where(M.Position.user_id == user.id, M.Position.status == "OPEN"))).scalar_one()
    if open_n:
        raise HTTPException(409, f"Close your {open_n} open position(s) before switching runners: each runner manages its own positions.")
    await control.bump(db, user.id, execution_mode=body.mode, desired_state="STOPPED")  # a runner change needs an explicit START
    await repo.audit(db, user.id, user.email, "EXECUTION_MODE_SWITCH", before=current, after=body.mode)
    await db.commit()
    return await _status(request, db, user)


@router.get("/commands/{command_id}")
async def command_status(command_id: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Progress of a queued runner command (force-buy, AI retry, close, withdraw): PENDING until the runner acks."""
    c = await db.get(M.RunnerCommand, command_id)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "not found")
    return {"id": c.id, "type": c.type, "status": c.status, "detail": c.detail, "created_at": c.created_at, "updated_at": c.updated_at}


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
