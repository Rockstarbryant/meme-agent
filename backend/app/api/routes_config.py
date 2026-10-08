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
from app.services.webhook_safety import check_webhook_url
from app.strategies import registry as strategy_registry

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
    rows = {s.id: s for s in (await db.execute(select(M.Strategy))).scalars().all()}
    enabled = set(cfg.strategies_enabled or [])
    chosen = await repo.get_setting(db, f"user:{user.id}:active_strategy")
    active = strategy_registry.select_active(list(enabled), (chosen or {}).get("id") if isinstance(chosen, dict) else chosen)
    out = []
    for spec in strategy_registry.SPECS.values():
        row = rows.get(spec.id)
        ex = spec.default_exit()
        out.append({"id": spec.id, "name": row.name if row else spec.name, "description": row.description if row else spec.description,
                    "enabled": spec.id in enabled, "active": spec.id == active, "profile": spec.profile, "best_for": spec.best_for,
                    "weights": spec.default_config().weights.model_dump() if hasattr(spec.default_config(), "weights") else {},
                    "exit_profile": {"hard_stop_pct": ex.hard_stop_pct, "trailing_stop_pct": ex.trailing_stop_pct,
                                     "first_target_pct": ex.tiers[0].gain_pct if ex.tiers else None,
                                     "stagnation_hours": round(ex.stagnation_seconds / 3600.0, 2)}})
    return out


@router.put("/strategies/{sid}/activate")
async def activate_strategy(sid: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Choose which enabled strategy the agent runs. It is enabled automatically if it was not."""
    if strategy_registry.get(sid) is None:
        raise HTTPException(404, "unknown strategy")
    cfg = await control.get_config(db, user.id)
    enabled = list(cfg.strategies_enabled or [])
    if sid not in enabled:
        enabled.append(sid)
    await repo.put_setting(db, f"user:{user.id}:active_strategy", {"id": sid}, user.email)
    await control.bump(db, user.id, strategies_enabled=enabled)
    await repo.audit(db, user.id, user.email, "STRATEGY_ACTIVATED", strategy=sid)
    await db.commit()
    return {"strategy": sid, "active": True, "note": "Open positions keep the exit rules of the strategy that opened them."}


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
    spec = strategy_registry.get(sid)
    if spec is None:
        raise HTTPException(404, "unknown strategy")
    if user.mode == "LIVE" and not body.confirm:
        raise HTTPException(409, "changing strategy configuration in LIVE mode requires confirm=true")
    top = (await db.execute(select(func.max(M.StrategyVersion.version)).where(M.StrategyVersion.strategy_id == sid))).scalar() or 0
    try:
        cfg = spec.config_cls(**{**body.config, "strategy_id": sid, "version": top + 1})
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


# Events a user can subscribe a webhook to: a curated subset of app.services.ingest.STREAMED (the events already pushed
# to the live UI), since those are the ones a person would plausibly want pushed to Slack / Discord / their own server.
NOTIFICATION_EVENTS = [
    "POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED", "ORDER_FAILED",
    "TAKE_PROFIT_TRIGGERED", "STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED",
    "EMERGENCY_STOP_CHANGED", "RISK_ALERT", "AGENT_ERROR",
]
# What each event means, in plain words, grouped for the Settings screen.
NOTIFICATION_CATALOG = [
    {"id": "POSITION_OPENED", "group": "Trades", "label": "Position opened", "description": "The agent bought a token."},
    {"id": "POSITION_CLOSED", "group": "Trades", "label": "Position closed", "description": "A position was fully sold, with the reason and the result."},
    {"id": "ORDER_FILLED", "group": "Trades", "label": "Order filled", "description": "A buy or sell order completed."},
    {"id": "ORDER_FAILED", "group": "Trades", "label": "Order failed", "description": "An order was rejected or could not be completed."},
    {"id": "TAKE_PROFIT_TRIGGERED", "group": "Exits", "label": "Take-profit hit", "description": "A profit target was reached and part of the position sold."},
    {"id": "STOP_LOSS_TRIGGERED", "group": "Exits", "label": "Stop-loss hit", "description": "The hard stop was reached and the position is being closed."},
    {"id": "TRAILING_STOP_TRIGGERED", "group": "Exits", "label": "Trailing stop hit", "description": "Price fell back from its peak by the trailing amount."},
    {"id": "EMERGENCY_STOP_CHANGED", "group": "Safety", "label": "Emergency stop changed", "description": "The kill switch was turned on or off."},
    {"id": "RISK_ALERT", "group": "Safety", "label": "Risk alert", "description": "A risk limit was reached or a risk condition appeared."},
    {"id": "AGENT_ERROR", "group": "Safety", "label": "Agent error", "description": "The agent hit an error it could not recover from by itself."},
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
            ok, why = check_webhook_url(self.webhook_url)
            if not ok:
                raise ValueError(why)
        unknown = set(self.events) - set(NOTIFICATION_EVENTS)
        if unknown:
            raise ValueError(f"unknown event type(s): {sorted(unknown)}")
        return self


@router.get("/notifications")
async def get_notifications(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await repo.get_setting(db, f"user:{user.id}:notifications") or {}
    return {"enabled": bool(cfg.get("enabled")), "webhook_url": cfg.get("webhook_url"),
            "events": cfg.get("events") or [], "available_events": NOTIFICATION_EVENTS, "event_catalog": NOTIFICATION_CATALOG,
            "last_delivery": cfg.get("last_delivery")}


@router.put("/notifications")
async def put_notifications(body: NotificationsIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    prev = await repo.get_setting(db, f"user:{user.id}:notifications") or {}
    if prev.get("webhook_url") == data.get("webhook_url") and prev.get("last_delivery"):
        data["last_delivery"] = prev["last_delivery"]      # same endpoint: keep its delivery history
    await repo.put_setting(db, f"user:{user.id}:notifications", data, user.email)
    await repo.audit(db, user.id, user.email, "NOTIFICATIONS_UPDATED", enabled=body.enabled, events=body.events)
    await db.commit()
    return {**data, "available_events": NOTIFICATION_EVENTS, "event_catalog": NOTIFICATION_CATALOG}


class WebhookTestIn(BaseModel):
    webhook_url: str | None = None   # test the URL typed in the form before saving; defaults to the saved one


@router.post("/notifications/test")
async def test_notification(body: WebhookTestIn, request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Send one sample event to the webhook so the user can see that it works (and what it receives)."""
    import time
    import httpx
    from app.api.deps import limit
    await limit(request, "webhooktest", 6, 60, extra=user.id)
    cfg = await repo.get_setting(db, f"user:{user.id}:notifications") or {}
    url = (body.webhook_url or cfg.get("webhook_url") or "").strip()
    if not url:
        raise HTTPException(422, "Enter a webhook URL first.")
    ok, why = check_webhook_url(url)
    if not ok:
        raise HTTPException(422, why)
    sample = {"type": "TEST", "at": datetime.now(timezone.utc).isoformat(), "payload": {"message": "This is a test from Arc Agent. Real events look like this, with their own type and details."}}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            r = await client.post(url, json=sample)
        ms = round((time.monotonic() - started) * 1000)
        delivered = 200 <= r.status_code < 300
        return {"delivered": delivered, "http_status": r.status_code, "elapsed_ms": ms, "sent": sample,
                "message": "Delivered." if delivered else f"The endpoint answered HTTP {r.status_code}. A 2xx answer is needed."}
    except httpx.TimeoutException:
        return {"delivered": False, "http_status": None, "elapsed_ms": 5000, "sent": sample, "message": "The endpoint did not answer within 5 seconds."}
    except httpx.HTTPError as e:
        return {"delivered": False, "http_status": None, "elapsed_ms": round((time.monotonic() - started) * 1000), "sent": sample,
                "message": f"Could not connect ({type(e).__name__})."}
