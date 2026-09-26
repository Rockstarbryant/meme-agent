"""Platform shared-worker API.

Authenticated with PLATFORM_WORKER_TOKEN (not user JWTs). The shared worker
lists active cloud-managed tenants, pulls each user's ConfigBundle, and posts
events/heartbeats scoped to that user_id.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, get_db
from app.core.clock import utcnow
from app.db import models as M
from app.db import repo
from app.domain.runner_protocol import ConfigBundle, EventBatch, Heartbeat, HeartbeatResponse, Command, CommandAck
from app.services import control
from app.services.ingest import ingest

router = APIRouter(prefix="/platform", tags=["platform worker"])

__all__ = ["router", "platform_worker"]


async def platform_worker(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    settings = C(request).settings
    expected = settings.platform_worker_token.get_secret_value() if settings.platform_worker_token else ""
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "platform worker token required")
    got = authorization[7:].strip()
    if not secrets.compare_digest(got, expected):
        raise HTTPException(401, "invalid platform worker token")
    if not settings.cloud_managed_enabled:
        raise HTTPException(403, "CLOUD_MANAGED_ENABLED is false")


class TenantSummary(BaseModel):
    user_id: str
    mode: str
    desired_state: str
    emergency_stop: bool
    config_version: int
    wallet_id: str
    wallet_address: str
    privy_wallet_id: str
    policy_version: int = 0


async def _cloud_runner(db: AsyncSession, user_id: str) -> M.Runner:
    r = (
        await db.execute(
            select(M.Runner).where(
                M.Runner.user_id == user_id,
                M.Runner.name == "platform-shared-worker",
                M.Runner.revoked_at.is_(None),
            )
        )
    ).scalars().first()
    if r is None:
        r = M.Runner(
            user_id=user_id,
            name="platform-shared-worker",
            token_hash=hashlib.sha256(f"cloud:{user_id}:{secrets.token_hex(8)}".encode()).hexdigest(),
            version="cloud",
        )
        db.add(r)
        await db.flush()
    return r


@router.get("/tenants/active", response_model=list[TenantSummary])
async def active_tenants(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    rows = (
        await db.execute(
            select(M.User, M.AgentConfig, M.Wallet, M.WalletPolicyRow)
            .join(M.AgentConfig, M.AgentConfig.user_id == M.User.id)
            .join(M.Wallet, M.Wallet.user_id == M.User.id)
            .outerjoin(
                M.WalletPolicyRow,
                (M.WalletPolicyRow.wallet_id == M.Wallet.id) & (M.WalletPolicyRow.is_current.is_(True)),
            )
            .where(
                M.User.is_active.is_(True),
                M.AgentConfig.execution_mode == "cloud_managed",
                M.Wallet.provider == "privy",
                M.Wallet.external_id.is_not(None),
            )
            .order_by(M.Wallet.created_at.desc())
        )
    ).all()
    out: list[TenantSummary] = []
    seen: set[str] = set()
    for user, cfg, wallet, pol in rows:
        if user.id in seen:
            continue
        seen.add(user.id)
        if not wallet.external_id or not wallet.address:
            continue
        out.append(
            TenantSummary(
                user_id=user.id,
                mode=user.mode,
                desired_state=cfg.desired_state,
                emergency_stop=cfg.emergency_stop,
                config_version=cfg.version,
                wallet_id=wallet.id,
                wallet_address=wallet.address,
                privy_wallet_id=wallet.external_id,
                policy_version=int(pol.version) if pol else 0,
            )
        )
    return out


@router.get("/tenants/{user_id}/config", response_model=ConfigBundle)
async def tenant_config(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    user = await db.get(M.User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(404, "user not found")
    cfg = await control.get_config(db, user_id)
    if cfg.execution_mode != "cloud_managed":
        raise HTTPException(409, "user is not cloud_managed")
    runner = await _cloud_runner(db, user_id)
    bundle = await control.build_bundle(db, C(request).settings, user, runner)
    await db.commit()
    return bundle


@router.post("/tenants/{user_id}/heartbeat")
async def tenant_heartbeat(
    user_id: str,
    hb: Heartbeat,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    user = await db.get(M.User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    r = await _cloud_runner(db, user_id)
    r.last_seen_at = utcnow()
    r.version = hb.version or r.version
    # Receiving this heartbeat proves the worker can reach the control plane.
    # The runner builds its heartbeat payload before making the HTTP request, so
    # an older payload can still contain the dead-man message from the previous
    # outage. Never persist that stale message after a successful heartbeat;
    # otherwise the UI can show "control plane unreachable" while last_seen_at
    # is only a few seconds old and the runner has already resumed entries.
    entries_suspended_reason = hb.entries_suspended_reason
    if entries_suspended_reason and entries_suspended_reason.startswith("control plane unreachable"):
        entries_suspended_reason = None

    r.status = {
        "state": hb.state,
        "mode": hb.mode.value if hasattr(hb.mode, "value") else hb.mode,
        "applied_config_version": hb.applied_config_version,
        "strategy_version": hb.strategy_version,
        "data_source": hb.data_source,
        "data_status": hb.data_status,
        "ai": hb.ai,
        "open_positions": hb.open_positions,
        "last_activity_at": hb.last_activity_at.isoformat() if hb.last_activity_at else None,
        "last_decision": hb.last_decision,
        "live": hb.live.model_dump(mode="json") if hb.live else {},
        "wallet_provider": hb.wallet_provider or "privy",
        "local_ceilings": hb.local_ceilings,
        "entries_suspended_reason": entries_suspended_reason,
        "last_error": hb.last_error,
        "execution_mode": "cloud_managed",
    }
    # The self-hosted runner path (/runner/heartbeat, routes_runner.py) delivers
    # pending RunnerCommand rows (manual close-position, close-all, withdraw...)
    # in its heartbeat response. This cloud-tenant heartbeat used to always
    # return an empty command list, which meant those commands were queued by
    # the API but silently never delivered to a Privy/cloud-managed user's
    # worker cycle. Deliver them the same way the self-hosted path does.
    cmds = (await db.execute(select(M.RunnerCommand).where(M.RunnerCommand.user_id == user_id, M.RunnerCommand.status == "PENDING")
                             .order_by(M.RunnerCommand.created_at).limit(20))).scalars().all()
    await db.commit()
    return HeartbeatResponse(server_time=utcnow(), desired_config_version=(await control.get_config(db, user_id)).version,
                             commands=[Command(id=c.id, type=c.type, payload=c.payload) for c in cmds]).model_dump(mode="json")


@router.post("/tenants/{user_id}/commands/{cid}/ack")
async def ack_tenant_command(
    user_id: str,
    cid: str,
    body: CommandAck,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    c = await db.get(M.RunnerCommand, cid)
    if c is None or c.user_id != user_id:
        raise HTTPException(404, "command not found")
    c.status, c.detail, c.updated_at = body.status, body.detail, utcnow()
    db.add(c)
    await db.commit()
    return {"ok": True}


@router.get("/tenants/{user_id}/state")
async def tenant_state(
    user_id: str,
    mode: str = Query("PAPER"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    user = await db.get(M.User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(404, "user not found")
    from app.core.types import TradingMode
    try:
        tm = TradingMode(mode)
    except ValueError:
        raise HTTPException(400, "mode must be PAPER or LIVE")
    pf = await repo.load_portfolio(db, user_id, tm, 0.0, utcnow())
    active = (await db.execute(
        select(M.Order.idempotency_key, M.Order.status).where(
            M.Order.user_id == user_id, M.Order.mode == tm.value,
            M.Order.status.in_(["PENDING_SIGNATURE", "SUBMITTED", "TIMEOUT", "FILLED", "PARTIALLY_FILLED"])
        )
    )).all()
    return {"user_id": user_id, "mode": tm.value, "portfolio": pf.to_dict(),
            "active_idempotency": [{"key": k, "status": st} for k, st in active]}


@router.post("/tenants/{user_id}/state")
async def save_tenant_state(
    user_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    user = await db.get(M.User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(404, "user not found")
    from app.core.types import TradingMode
    try:
        tm = TradingMode(body.get("mode", "PAPER"))
    except ValueError:
        raise HTTPException(400, "mode must be PAPER or LIVE")
    portfolio = body.get("portfolio")
    if not isinstance(portfolio, dict):
        raise HTTPException(400, "portfolio object required")
    from app.portfolio.state import PortfolioState
    pf = PortfolioState.from_dict(portfolio)
    if pf.mode != tm:
        raise HTTPException(400, "portfolio mode mismatch")
    await repo.save_portfolio(db, user_id, pf, utcnow())
    for pos in pf.open_positions():
        await repo.upsert_position(db, user_id, pos)
    await db.commit()
    return {"ok": True}


@router.post("/tenants/{user_id}/lease")
async def acquire_tenant_lease(user_id: str, body: dict, request: Request, _: None = Depends(platform_worker)):
    if not isinstance(body.get("worker_id"), str) or not body["worker_id"]:
        raise HTTPException(400, "worker_id required")
    ttl = max(30, min(int(body.get("ttl_s", 45)), 300))
    key = f"cloud-tenant-lease:{user_id}"
    ok = await C(request).redis.set(key, body["worker_id"], nx=True, ex=ttl)
    return {"acquired": bool(ok)}


@router.delete("/tenants/{user_id}/lease")
async def release_tenant_lease(user_id: str, worker_id: str, request: Request, _: None = Depends(platform_worker)):
    key = f"cloud-tenant-lease:{user_id}"
    current = await C(request).redis.get(key)
    if current and (current.decode() if isinstance(current, bytes) else current) == worker_id:
        await C(request).redis.delete(key)
    return {"ok": True}


@router.post("/tenants/{user_id}/events")
async def tenant_events(
    user_id: str,
    batch: EventBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(platform_worker),
):
    user = await db.get(M.User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    r = await _cloud_runner(db, user_id)
    return await ingest(db, C(request).hub, r, batch)