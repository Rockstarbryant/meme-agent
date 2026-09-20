"""Control-plane logic: what the runner should do, what it reported, and whether LIVE may be activated.

This server never trades. It stores policy/config, hands it to the user's Local Runner, and records what the runner reports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import utcnow
from app.db import models as M
from app.db import repo
from app.domain.runner_protocol import ConfigBundle
from app.portfolio.controls import ControlState
from app.risk.engine import RiskLimits
from app.risk.per_user_policy import (
    DEFAULT_PLATFORM_CEILINGS,
    PlatformCeilings,
    clamp_to_platform,
    effective_limits,
)
from app.services.decision import tighten_limits
from app.strategies.traction_momentum import TractionMomentumConfig
from app.wallets.base import WalletPolicy

LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"
ONLINE_WINDOW_S = 30


def default_limits(s: Settings) -> RiskLimits:
    return RiskLimits(max_trade_usdc=s.risk_max_trade_usdc, max_position_usdc=s.risk_max_position_usdc,
                      max_daily_loss_usdc=s.risk_max_daily_loss_usdc, max_total_exposure_usdc=s.risk_max_total_exposure_usdc,
                      max_open_positions=s.risk_max_open_positions, max_slippage_pct=s.risk_max_slippage_pct,
                      min_liquidity_usdc=s.risk_min_liquidity_usdc)


async def get_config(db: AsyncSession, user_id: str) -> M.AgentConfig:
    """Race-safe: the runner's config poll, its heartbeat and the UI can all touch a brand-new user at the same instant."""
    cfg = await db.get(M.AgentConfig, user_id)
    if cfg is None:
        await db.execute(pg_insert(M.AgentConfig).values(user_id=user_id, version=1, desired_state="STOPPED", emergency_stop=False,
                                                          strategies_enabled=["traction_momentum"], updated_at=utcnow())
                         .on_conflict_do_nothing(index_elements=["user_id"]))
        cfg = await db.get(M.AgentConfig, user_id, populate_existing=True)
    assert cfg is not None
    return cfg


async def bump(db: AsyncSession, user_id: str, **fields) -> M.AgentConfig:
    """Every change that the runner must see goes through here: it raises the config version."""
    await get_config(db, user_id)
    # one atomic UPDATE: concurrent changes each get their own version number (no lost increments)
    await db.execute(update(M.AgentConfig).where(M.AgentConfig.user_id == user_id)
                     .values(version=M.AgentConfig.version + 1, updated_at=utcnow(), **fields))
    cfg = await db.get(M.AgentConfig, user_id, populate_existing=True)
    assert cfg is not None
    return cfg


async def active_runner(db: AsyncSession, user_id: str) -> M.Runner | None:
    return (await db.execute(select(M.Runner).where(M.Runner.user_id == user_id, M.Runner.revoked_at.is_(None))
                             .order_by(M.Runner.created_at.desc()))).scalars().first()


def is_online(r: M.Runner | None, now: datetime | None = None) -> bool:
    if r is None or r.last_seen_at is None:
        return False
    return ((now or utcnow()) - r.last_seen_at.astimezone(timezone.utc)).total_seconds() <= ONLINE_WINDOW_S


async def load_wallet_row(db: AsyncSession, user_id: str) -> M.Wallet | None:
    return (await db.execute(
        select(M.Wallet).where(M.Wallet.user_id == user_id)
        .order_by(M.Wallet.ownership_verified.desc(), M.Wallet.created_at.desc())
    )).scalars().first()


async def load_policy(db: AsyncSession, user_id: str) -> WalletPolicy | None:
    w = await load_wallet_row(db, user_id)
    if w is None:
        return None
    row = (await db.execute(select(M.WalletPolicyRow).where(M.WalletPolicyRow.wallet_id == w.id, M.WalletPolicyRow.is_current.is_(True)))).scalars().first()
    return WalletPolicy(**row.policy) if row else None


async def load_policy_version(db: AsyncSession, user_id: str) -> int:
    w = await load_wallet_row(db, user_id)
    if w is None:
        return 0
    row = (await db.execute(select(M.WalletPolicyRow).where(M.WalletPolicyRow.wallet_id == w.id, M.WalletPolicyRow.is_current.is_(True)))).scalars().first()
    return int(row.version) if row else 0


def platform_ceilings_from_settings(settings: Settings) -> PlatformCeilings:
    return PlatformCeilings(
        max_trade_usdc=settings.platform_max_trade_usdc,
        max_position_usdc=settings.platform_max_position_usdc,
        max_daily_loss_usdc=settings.platform_max_daily_loss_usdc,
        max_total_exposure_usdc=settings.platform_max_total_exposure_usdc,
        max_open_positions=settings.platform_max_open_positions,
        max_slippage_pct=settings.platform_max_slippage_pct,
        min_liquidity_usdc=settings.platform_min_liquidity_usdc,
    )


async def load_limits(db: AsyncSession, settings: Settings, user_id: str) -> RiskLimits:
    """User risk limits, always clamped to platform ceilings before leaving the control plane."""
    raw = await repo.get_setting(db, f"user:{user_id}:risk_limits")
    limits = RiskLimits(**raw) if raw else default_limits(settings)
    return clamp_to_platform(limits, platform_ceilings_from_settings(settings))


async def load_controls(db: AsyncSession, user_id: str) -> ControlState:
    raw = await repo.get_setting(db, f"user:{user_id}:controls")
    return ControlState(**raw) if raw else ControlState()


async def build_bundle(db: AsyncSession, settings: Settings, user: M.User, runner: M.Runner) -> ConfigBundle:
    cfg = await get_config(db, user.id)
    sv = (await db.execute(select(M.StrategyVersion).where(M.StrategyVersion.strategy_id == "traction_momentum",
                                                         (M.StrategyVersion.user_id == user.id) | M.StrategyVersion.user_id.is_(None))
                           .order_by(M.StrategyVersion.user_id.is_(None), M.StrategyVersion.version.desc()))).scalars().first()
    controls = await load_controls(db, user.id)
    controls.emergency_stop = cfg.emergency_stop
    controls.global_pause = cfg.desired_state != "RUNNING"
    policy = await load_policy(db, user.id)
    wallet = await load_wallet_row(db, user.id)
    policy_version = await load_policy_version(db, user.id)
    platform = platform_ceilings_from_settings(settings)
    user_limits = await load_limits(db, settings, user.id)
    # Bundle carries *effective* limits (platform ∩ user ∩ wallet) so workers cannot skip a layer
    eff = effective_limits(user_limits, policy, platform)
    return ConfigBundle(
        version=cfg.version,
        runner_id=runner.id,
        mode=user.mode,  # type: ignore[arg-type]
        desired_state=cfg.desired_state,  # type: ignore[arg-type]
        emergency_stop=cfg.emergency_stop,
        strategy=(sv.config if sv else TractionMomentumConfig().model_dump(mode="json")),
        strategies_enabled=list(cfg.strategies_enabled or []),
        risk_limits=eff.model_dump(mode="json"),
        wallet_policy=policy.model_dump(mode="json") if policy else None,
        controls=controls.model_dump(mode="json"),
        issued_at=utcnow(),
        user_id=user.id,
        wallet_id=wallet.id if wallet else None,
        wallet_address=wallet.address if wallet else None,
        wallet_provider=wallet.provider if wallet else None,
        policy_version=policy_version,
        platform_ceilings=platform.model_dump(mode="json"),
    )


async def live_blockers(db: AsyncSession, settings: Settings, user_id: str) -> list[str]:
    b: list[str] = []
    if not settings.live_trading_enabled:
        b.append("LIVE_TRADING_ENABLED is false on the server")
    cfg = await get_config(db, user_id)
    if getattr(cfg, "execution_mode", "self_hosted") == "cloud_managed":
        if not getattr(settings, "cloud_managed_enabled", False):
            b.append("CLOUD_MANAGED_ENABLED is false on the server")
        w = await load_wallet_row(db, user_id)
        if w is None or w.provider != "privy" or not w.external_id:
            b.append("No Privy cloud wallet provisioned (POST /wallet/cloud/provision)")
        r = await active_runner(db, user_id)
        if r is None or not is_online(r):
            b.append("Shared cloud worker is offline")
        else:
            live = (r.status or {}).get("live") or {}
            if not live.get("available"):
                b.extend(live.get("reasons") or ["Cloud worker reports LIVE is unavailable"])
        return b
    r = await active_runner(db, user_id)
    if r is None:
        b.append("No Local Runner is paired (LIVE executes only on your own runner)")
    elif not is_online(r):
        b.append("Your Local Runner is offline")
    else:
        live = (r.status or {}).get("live") or {}
        if not live.get("available"):
            b.extend(live.get("reasons") or ["The Local Runner reports LIVE is unavailable"])
    return b


def runner_info(r: M.Runner | None, cfg: M.AgentConfig) -> dict | None:
    if r is None:
        return None
    st = r.status or {}
    return {"id": r.id, "name": r.name, "online": is_online(r), "last_seen_at": r.last_seen_at, "version": r.version,
            "applied_config_version": st.get("applied_config_version", 0), "desired_config_version": cfg.version,
            "wallet_provider": st.get("wallet_provider"), "live": st.get("live"), "local_ceilings": st.get("local_ceilings", {}),
            "entries_suspended_reason": st.get("entries_suspended_reason"), "last_error": st.get("last_error"), "state": st.get("state")}


async def agent_status(db: AsyncSession, settings: Settings, user: M.User) -> dict:
    cfg, r = await get_config(db, user.id), await active_runner(db, user.id)
    st = (r.status or {}) if r else {}
    online = is_online(r)
    last = (await db.execute(select(M.Decision).where(M.Decision.user_id == user.id).order_by(M.Decision.created_at.desc()).limit(1))).scalars().first()
    open_n = (await db.execute(select(func.count()).select_from(M.Position).where(M.Position.user_id == user.id, M.Position.status == "OPEN", M.Position.mode == user.mode))).scalar_one()
    limits = tighten_limits(await load_limits(db, settings, user.id), await load_policy(db, user.id))
    sv = st.get("applied_config_version")
    return {
        "state": (st.get("state") or "STARTING") if online else "OFFLINE", "desired_state": cfg.desired_state,
        "mode": user.mode, "mode_label": "LIVE MODE" if user.mode == "LIVE" else "PAPER MODE",
        "data_source": st.get("data_source") or "no runner connected", "data_status": st.get("data_status") or "",
        "emergency_stop": cfg.emergency_stop, "global_pause": cfg.desired_state != "RUNNING",
        "ai": st.get("ai") or {"mode": "UNKNOWN", "provider": None, "model": None},
        "strategy": {"id": "traction_momentum", "version": (st.get("strategy_version") or 0)},
        "open_positions": open_n, "last_activity_at": st.get("last_activity_at"),
        "last_decision": None if last is None else {"id": last.id, "token": last.token_key, "symbol": last.symbol, "action": last.final_action,
                                                   "reason": last.final_reason, "at": last.created_at.isoformat()},
        "limits": limits.model_dump(mode="json"), "live_blockers": await live_blockers(db, settings, user.id),
        "live_confirmation_phrase": LIVE_CONFIRMATION_PHRASE, "config_version": cfg.version, "applied_config_version": sv or 0,
        "runner": runner_info(r, cfg),
        "execution_mode": getattr(cfg, "execution_mode", "self_hosted"),
    }
