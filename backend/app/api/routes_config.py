from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db
from app.db import models as M
from app.db import repo
from app.risk.engine import RiskLimits
from app.services import control
from app.services.decision import tighten_limits
from app.strategies.traction_momentum import TractionMomentumConfig

router = APIRouter(tags=["config"])


class LimitsIn(RiskLimits):
    confirm: bool = False

    @model_validator(mode="after")
    def _sane(self):
        if not (0 < self.max_trade_usdc <= self.max_position_usdc <= self.max_total_exposure_usdc):
            raise ValueError("require 0 < max_trade <= max_position <= max_total_exposure")
        if self.max_slippage_pct <= 0 or self.max_slippage_pct > 20:
            raise ValueError("max_slippage_pct must be in (0, 20]")
        return self


class StrategyVersionIn(BaseModel):
    config: dict
    confirm: bool = False


class EnabledIn(BaseModel):
    enabled: bool


class BlacklistIn(BaseModel):
    kind: str  # token | creator | launchpad
    value: str


@router.get("/strategies")
async def strategies(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await control.get_config(db, user.id)
    rows = (await db.execute(select(M.Strategy))).scalars().all()
    return [{"id": s.id, "name": s.name, "description": s.description, "enabled": s.id in (cfg.strategies_enabled or []), "active": s.id == "traction_momentum"} for s in rows]


@router.put("/strategies/{sid}/enabled")
async def set_enabled(sid: str, body: EnabledIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if await db.get(M.Strategy, sid) is None:
        raise HTTPException(404, "unknown strategy")
    cfg = await control.get_config(db, user.id)
    enabled = [s for s in (cfg.strategies_enabled or []) if s != sid] + ([sid] if body.enabled else [])
    await control.bump(db, user.id, strategies_enabled=enabled)
    await repo.audit(db, user.id, user.email, "STRATEGY_ENABLED" if body.enabled else "STRATEGY_DISABLED", strategy=sid)
    await db.commit()
    return {"strategy": sid, "enabled": body.enabled}


@router.get("/strategies/{sid}/versions")
async def versions(sid: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.StrategyVersion).where(M.StrategyVersion.strategy_id == sid, (M.StrategyVersion.user_id == user.id) | M.StrategyVersion.user_id.is_(None)).order_by(M.StrategyVersion.version.desc()))).scalars().all()
    return [{"version": r.version, "scope": "user" if r.user_id else "default", "created_at": r.created_at, "config": r.config} for r in rows]


@router.post("/strategies/{sid}/versions", status_code=201)
async def new_version(sid: str, body: StrategyVersionIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if sid != "traction_momentum":
        raise HTTPException(404, "unknown strategy")
    if user.mode == "LIVE" and not body.confirm:
        raise HTTPException(409, "changing strategy configuration in LIVE mode requires confirm=true")
    top = (await db.execute(select(func.max(M.StrategyVersion.version)).where(M.StrategyVersion.strategy_id == sid))).scalar() or 0
    try:
        cfg = TractionMomentumConfig(**{**body.config, "strategy_id": sid, "version": top + 1})
    except (ValidationError, TypeError) as e:
        raise HTTPException(422, str(e)[:500])
    db.add(M.StrategyVersion(strategy_id=sid, user_id=user.id, version=cfg.version, config=cfg.model_dump(mode="json")))
    await control.bump(db, user.id)
    await repo.audit(db, user.id, user.email, "STRATEGY_VERSION_CREATED", strategy=sid, version=cfg.version)
    await db.commit()
    return {"strategy_id": sid, "version": cfg.version, "config": cfg.model_dump(mode="json")}


@router.get("/risk/limits")
async def get_limits(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.risk.per_user_policy import effective_limits
    from app.services.control import platform_ceilings_from_settings

    settings = C(request).settings
    platform = platform_ceilings_from_settings(settings)
    limits = await control.load_limits(db, settings, user.id)
    policy = await control.load_policy(db, user.id)
    eff = effective_limits(limits, policy, platform)
    return {
        "limits": limits.model_dump(mode="json"),
        "effective_limits": eff.model_dump(mode="json"),
        "platform_ceilings": platform.model_dump(mode="json"),
        "note": "Effective = platform ceilings ∩ user risk limits ∩ wallet policy. Runner may tighten further with local ceilings.",
    }


@router.put("/risk/limits")
async def put_limits(request: Request, body: LimitsIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.mode == "LIVE" and not body.confirm:
        raise HTTPException(409, "changing risk limits in LIVE mode requires confirm=true")
    requested = RiskLimits(**body.model_dump(exclude={"confirm"}))
    from app.risk.per_user_policy import reject_if_above_platform
    from app.services.control import platform_ceilings_from_settings

    platform = platform_ceilings_from_settings(C(request).settings)
    over = reject_if_above_platform(requested, platform)
    if over:
        raise HTTPException(422, {"error": "EXCEEDS_PLATFORM_CEILINGS", "detail": over})
    before = await repo.get_setting(db, f"user:{user.id}:risk_limits")
    data = requested.model_dump(mode="json")
    await repo.put_setting(db, f"user:{user.id}:risk_limits", data, user.email)
    await control.bump(db, user.id)
    await repo.audit(db, user.id, user.email, "RISK_LIMITS_CHANGED", before=before, after=data)
    await db.commit()
    return {
        "limits": data,
        "platform_ceilings": platform.model_dump(mode="json"),
        "note": "Stored limits are clamped to platform ceilings on delivery. Runner also applies its own local ceilings.",
    }


def _bl(ctl, kind: str) -> set:
    try:
        return {"token": ctl.blacklisted_tokens, "creator": ctl.blacklisted_creators, "launchpad": ctl.blacklisted_launchpads}[kind]
    except KeyError:
        raise HTTPException(422, "kind must be token, creator or launchpad")


@router.get("/controls/blacklist")
async def get_blacklist(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    ctl = await control.load_controls(db, user.id)
    return {"tokens": sorted(ctl.blacklisted_tokens), "creators": sorted(ctl.blacklisted_creators), "launchpads": sorted(ctl.blacklisted_launchpads)}


async def _edit_blacklist(body: BlacklistIn, user: M.User, db: AsyncSession, add: bool) -> dict:
    ctl = await control.load_controls(db, user.id)
    s, v = _bl(ctl, body.kind), (body.value if body.kind == "launchpad" else body.value.lower())
    (s.add if add else s.discard)(v)
    await repo.put_setting(db, f"user:{user.id}:controls", ctl.model_dump(mode="json"), user.email)
    await control.bump(db, user.id)
    await repo.audit(db, user.id, user.email, "BLACKLIST_ADD" if add else "BLACKLIST_REMOVE", kind=body.kind, value=body.value)
    await db.commit()
    return {"ok": True}


@router.post("/controls/blacklist", status_code=201)
async def add_blacklist(body: BlacklistIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _edit_blacklist(body, user, db, True)


@router.delete("/controls/blacklist")
async def remove_blacklist(body: BlacklistIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _edit_blacklist(body, user, db, False)


@router.get("/settings")
async def settings(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = C(request).settings
    r = await control.active_runner(db, user.id)
    st = (r.status or {}) if r else {}
    ai = st.get("ai") or {}
    notif = await repo.get_setting(db, f"user:{user.id}:notifications") or {}
    return {"mode": user.mode, "paper_trading_enabled": s.paper_trading_enabled, "live_trading_enabled_on_server": s.live_trading_enabled,
            "chains": [{"id": "arc", "chain_id": s.arc_chain_id, "rpc_configured": bool(s.arc_rpc_urls)}],
            "ai": {"provider": ai.get("provider"), "model": ai.get("model"),
                   "note": "AI provider, model and API keys live on your Local Runner. This server never holds them."},
            "market_data": st.get("data_source") or "no runner connected: market data is configured on your Local Runner",
            "notifications": {"implemented": True, "enabled": bool(notif.get("enabled")), "webhook_configured": bool(notif.get("webhook_url"))}}


# Events a user can subscribe a webhook to — a curated subset of app.services.ingest.STREAMED
# (the events already pushed to the live UI), since those are the ones a person would plausibly
# want pushed to Slack/Discord/etc. too.
NOTIFICATION_EVENTS = [
    "POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED", "ORDER_FAILED",
    "TAKE_PROFIT_TRIGGERED", "STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED",
    "EMERGENCY_STOP_CHANGED", "RISK_ALERT", "AGENT_ERROR",
]


class NotificationsIn(BaseModel):
    enabled: bool = False
    webhook_url: str | None = None
    events: list[str] = []

    @model_validator(mode="after")
    def _sane(self) -> "NotificationsIn":
        if self.enabled:
            if not self.webhook_url:
                raise ValueError("webhook_url is required when enabled=true")
            if not (self.webhook_url.startswith("https://") or self.webhook_url.startswith("http://")):
                raise ValueError("webhook_url must start with http:// or https://")
        unknown = set(self.events) - set(NOTIFICATION_EVENTS)
        if unknown:
            raise ValueError(f"unknown event type(s): {sorted(unknown)}")
        return self


@router.get("/notifications")
async def get_notifications(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await repo.get_setting(db, f"user:{user.id}:notifications") or {}
    return {"enabled": bool(cfg.get("enabled")), "webhook_url": cfg.get("webhook_url"),
            "events": cfg.get("events") or [], "available_events": NOTIFICATION_EVENTS}


@router.put("/notifications")
async def put_notifications(body: NotificationsIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    await repo.put_setting(db, f"user:{user.id}:notifications", data, user.email)
    await repo.audit(db, user.id, user.email, "NOTIFICATIONS_UPDATED", enabled=body.enabled, events=body.events)
    await db.commit()
    return {**data, "available_events": NOTIFICATION_EVENTS}
