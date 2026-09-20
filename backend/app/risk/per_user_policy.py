"""Per-user policy enforcement for multi-tenant / shared-worker execution.

Every trade must be evaluated in a *user-scoped* context:

  platform ceilings  ∩  user risk limits  ∩  wallet policy  ∩  (optional) local ceilings

Isolation rules enforced here:

1. ``user_id`` on the authorization must match the active tenant.
2. ``wallet_id`` / address must match the wallet bound to that user.
3. No user may raise limits above platform hard ceilings.
4. Effective limits are always the *stricter* of all layers.
5. Idempotency / correlation keys should include ``user_id`` (helper provided).

This module is pure (no DB/IO) so the control plane, shared worker, and local
runner can all call the same functions.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.errors import PolicyViolationError
from app.core.types import WalletCapability
from app.domain.trade import UnsignedTransaction
from app.risk.engine import RiskLimits
from app.wallets.base import PolicyValidator, WalletAuthorization, WalletPolicy


# ---------------------------------------------------------------------------
# Platform hard ceilings (no user / no wallet policy may exceed these)
# ---------------------------------------------------------------------------

class PlatformCeilings(BaseModel):
    """Hard upper bounds owned by the *platform*, not by any single user.

    Applied on the control plane when accepting limit updates and again on the
    worker before every trade. Shared-worker deployments rely on this so one
    misconfigured user cannot request unbounded size.
    """

    max_trade_usdc: float = 100.0
    max_position_usdc: float = 250.0
    max_daily_loss_usdc: float = 250.0
    max_total_exposure_usdc: float = 1_000.0
    max_open_positions: int = 20
    max_slippage_pct: float = 5.0
    min_liquidity_usdc: float = 5_000.0
    allowed_chains: set[str] = Field(default_factory=lambda: {"arc"})

    @model_validator(mode="after")
    def _ordered(self) -> PlatformCeilings:
        if not (0 < self.max_trade_usdc <= self.max_position_usdc <= self.max_total_exposure_usdc):
            raise ValueError("platform ceilings require 0 < max_trade <= max_position <= max_total_exposure")
        return self


DEFAULT_PLATFORM_CEILINGS = PlatformCeilings()


# ---------------------------------------------------------------------------
# User-scoped policy context (tenant isolation)
# ---------------------------------------------------------------------------

class UserPolicyContext(BaseModel):
    """Identity + policy snapshot for one user in a multi-tenant worker."""

    user_id: str = Field(min_length=1, max_length=64)
    wallet_id: str = Field(min_length=1, max_length=128)
    wallet_address: str = Field(min_length=1, max_length=128)
    wallet_provider: str = Field(min_length=1, max_length=64)
    policy_version: int = Field(ge=0, default=0)
    risk_limits: RiskLimits
    wallet_policy: WalletPolicy | None = None
    emergency_stop: bool = False
    global_pause: bool = False
    mode: str = "PAPER"  # PAPER | LIVE

    @field_validator("wallet_address")
    @classmethod
    def _addr(cls, v: str) -> str:
        v = v.strip()
        if v.upper() == "PAPER":
            return v
        if not (v.startswith("0x") and len(v) == 42):
            # Allow non-EVM later; for Arc we expect 0x…
            return v
        return v

    def scope_key(self) -> str:
        """Stable key for portfolio / idempotency isolation."""
        return f"{self.user_id}:{self.wallet_id}"


# ---------------------------------------------------------------------------
# Limit composition (strictest wins)
# ---------------------------------------------------------------------------

def clamp_to_platform(limits: RiskLimits, platform: PlatformCeilings | None = None) -> RiskLimits:
    """No user-supplied limits may exceed platform hard ceilings."""
    p = platform or DEFAULT_PLATFORM_CEILINGS
    return limits.model_copy(update={
        "max_trade_usdc": min(limits.max_trade_usdc, p.max_trade_usdc),
        "max_position_usdc": min(limits.max_position_usdc, p.max_position_usdc),
        "max_daily_loss_usdc": min(limits.max_daily_loss_usdc, p.max_daily_loss_usdc),
        "max_total_exposure_usdc": min(limits.max_total_exposure_usdc, p.max_total_exposure_usdc),
        "max_open_positions": min(limits.max_open_positions, p.max_open_positions),
        "max_slippage_pct": min(limits.max_slippage_pct, p.max_slippage_pct),
        "min_liquidity_usdc": max(limits.min_liquidity_usdc, p.min_liquidity_usdc),
        "allowed_chains": limits.allowed_chains & p.allowed_chains,
        "max_chain_exposure_usdc": min(limits.max_chain_exposure_usdc, p.max_total_exposure_usdc),
        "max_launchpad_exposure_usdc": min(limits.max_launchpad_exposure_usdc, p.max_total_exposure_usdc),
    })


def merge_wallet_policy(limits: RiskLimits, policy: WalletPolicy | None) -> RiskLimits:
    """Effective = stricter of risk limits and wallet policy (same semantics as tighten_limits)."""
    if policy is None:
        return limits
    chains = limits.allowed_chains & policy.allowed_chains
    launchpads = limits.allowed_launchpads & policy.allowed_launchpads if policy.allowed_launchpads else limits.allowed_launchpads
    return limits.model_copy(update={
        "max_trade_usdc": min(limits.max_trade_usdc, policy.max_trade_usdc),
        "max_position_usdc": min(limits.max_position_usdc, policy.max_position_usdc),
        "max_daily_loss_usdc": min(limits.max_daily_loss_usdc, policy.max_daily_loss_usdc),
        "max_open_positions": min(limits.max_open_positions, policy.max_open_positions),
        "max_slippage_pct": min(limits.max_slippage_pct, policy.max_slippage_pct),
        "min_liquidity_usdc": max(limits.min_liquidity_usdc, policy.min_liquidity_usdc),
        "allowed_chains": chains,
        "allowed_launchpads": launchpads,
    })


def effective_limits(
    user_limits: RiskLimits,
    wallet_policy: WalletPolicy | None = None,
    platform: PlatformCeilings | None = None,
    local_ceilings: Any | None = None,
) -> RiskLimits:
    """Full stack: platform ∩ user risk ∩ wallet policy ∩ optional local ceilings."""
    out = clamp_to_platform(user_limits, platform)
    out = merge_wallet_policy(out, wallet_policy)
    if local_ceilings is not None:
        # Duck-type LocalCeilings / anything with the same fields
        out = out.model_copy(update={
            "max_trade_usdc": min(out.max_trade_usdc, float(local_ceilings.max_trade_usdc)),
            "max_position_usdc": min(out.max_position_usdc, float(local_ceilings.max_position_usdc)),
            "max_daily_loss_usdc": min(out.max_daily_loss_usdc, float(local_ceilings.max_daily_loss_usdc)),
            "max_total_exposure_usdc": min(out.max_total_exposure_usdc, float(local_ceilings.max_total_exposure_usdc)),
            "max_open_positions": min(out.max_open_positions, int(local_ceilings.max_open_positions)),
            "max_slippage_pct": min(out.max_slippage_pct, float(local_ceilings.max_slippage_pct)),
            "min_liquidity_usdc": max(out.min_liquidity_usdc, float(local_ceilings.min_liquidity_usdc)),
            "allowed_chains": out.allowed_chains & set(local_ceilings.allowed_chains),
        })
    return out


def reject_if_above_platform(limits: RiskLimits, platform: PlatformCeilings | None = None) -> list[str]:
    """Return violation codes if the *requested* user limits exceed platform ceilings.

    Used by the API when accepting PUT /risk/limits so users cannot store values
    the platform will never honour.
    """
    p = platform or DEFAULT_PLATFORM_CEILINGS
    bad: list[str] = []
    if limits.max_trade_usdc > p.max_trade_usdc + 1e-9:
        bad.append(f"max_trade_usdc exceeds platform ceiling ({p.max_trade_usdc})")
    if limits.max_position_usdc > p.max_position_usdc + 1e-9:
        bad.append(f"max_position_usdc exceeds platform ceiling ({p.max_position_usdc})")
    if limits.max_daily_loss_usdc > p.max_daily_loss_usdc + 1e-9:
        bad.append(f"max_daily_loss_usdc exceeds platform ceiling ({p.max_daily_loss_usdc})")
    if limits.max_total_exposure_usdc > p.max_total_exposure_usdc + 1e-9:
        bad.append(f"max_total_exposure_usdc exceeds platform ceiling ({p.max_total_exposure_usdc})")
    if limits.max_open_positions > p.max_open_positions:
        bad.append(f"max_open_positions exceeds platform ceiling ({p.max_open_positions})")
    if limits.max_slippage_pct > p.max_slippage_pct + 1e-9:
        bad.append(f"max_slippage_pct exceeds platform ceiling ({p.max_slippage_pct})")
    if limits.min_liquidity_usdc < p.min_liquidity_usdc - 1e-9:
        bad.append(f"min_liquidity_usdc below platform floor ({p.min_liquidity_usdc})")
    extra = limits.allowed_chains - p.allowed_chains
    if extra:
        bad.append(f"allowed_chains includes non-platform chains: {sorted(extra)}")
    return bad


# ---------------------------------------------------------------------------
# Binding / isolation checks
# ---------------------------------------------------------------------------

def binding_violations(
    ctx: UserPolicyContext,
    *,
    expected_user_id: str | None = None,
    expected_wallet_id: str | None = None,
    expected_address: str | None = None,
    auth: WalletAuthorization | None = None,
) -> list[str]:
    """Cross-tenant isolation: refuse if context does not match the active job."""
    v: list[str] = []
    if expected_user_id is not None and ctx.user_id != expected_user_id:
        v.append("USER_ID_MISMATCH")
    if expected_wallet_id is not None and ctx.wallet_id != expected_wallet_id:
        v.append("WALLET_ID_MISMATCH")
    if expected_address is not None and ctx.wallet_address.lower() != expected_address.lower():
        v.append("WALLET_ADDRESS_MISMATCH")
    if auth is not None:
        if getattr(auth, "user_id", None) and auth.user_id != ctx.user_id:  # type: ignore[attr-defined]
            v.append("AUTH_USER_ID_MISMATCH")
        if auth.wallet_id != ctx.wallet_id:
            v.append("AUTH_WALLET_ID_MISMATCH")
        if auth.provider != ctx.wallet_provider:
            v.append("AUTH_PROVIDER_MISMATCH")
    return v


def scoped_idempotency_key(user_id: str, raw_key: str) -> str:
    """Prevent cross-user idempotency collisions on a shared worker."""
    return hashlib.sha256(f"{user_id}:{raw_key}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Pre-trade gate
# ---------------------------------------------------------------------------

class PerUserPolicyEnforcer:
    """Single entry point for shared-worker / multi-tenant trade authorization.

    Usage on the worker before calling ``wallet.submit``::

        enforcer = PerUserPolicyEnforcer(platform=PlatformCeilings(...))
        enforcer.authorize_trade(ctx, tx, auth, now, expected_chain_id=5042)
    """

    def __init__(self, platform: PlatformCeilings | None = None):
        self.platform = platform or DEFAULT_PLATFORM_CEILINGS

    def effective(self, ctx: UserPolicyContext, local_ceilings: Any | None = None) -> RiskLimits:
        return effective_limits(ctx.risk_limits, ctx.wallet_policy, self.platform, local_ceilings)

    def authorize_trade(
        self,
        ctx: UserPolicyContext,
        tx: UnsignedTransaction,
        auth: WalletAuthorization | None,
        now: datetime,
        *,
        expected_chain_id: int | None = None,
        expected_user_id: str | None = None,
        local_ceilings: Any | None = None,
    ) -> RiskLimits:
        """Raise PolicyViolationError with all reasons, or return effective limits."""
        reasons: list[str] = []

        if ctx.emergency_stop:
            reasons.append("EMERGENCY_STOP")
        if ctx.global_pause:
            reasons.append("GLOBAL_PAUSE")
        if ctx.mode == "LIVE" and (auth is None or auth.capability != WalletCapability.AUTONOMOUS_DELEGATED):
            # LIVE requires autonomous capability; PAPER may have no auth
            if ctx.mode == "LIVE":
                reasons.append("LIVE_REQUIRES_AUTONOMOUS_AUTHORIZATION")

        reasons += binding_violations(
            ctx,
            expected_user_id=expected_user_id or ctx.user_id,
            expected_wallet_id=ctx.wallet_id,
            expected_address=ctx.wallet_address,
            auth=auth,
        )

        # Base wallet policy checks (routers, amounts, signatures, …)
        reasons += PolicyValidator.violations(tx, auth, now, expected_chain_id)

        # Re-check amount against *effective* limits (platform ∩ user ∩ wallet)
        eff = self.effective(ctx, local_ceilings)
        if tx.amount_usdc > eff.max_trade_usdc + 1e-9:
            reasons.append("EXCEEDS_EFFECTIVE_MAX_TRADE")
        if tx.slippage_pct > eff.max_slippage_pct + 1e-9:
            reasons.append("EXCEEDS_EFFECTIVE_MAX_SLIPPAGE")
        if tx.chain not in eff.allowed_chains:
            reasons.append("CHAIN_NOT_IN_EFFECTIVE_POLICY")

        # Deduplicate while preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        if uniq:
            raise PolicyViolationError(uniq)
        return eff

    def assert_context_isolation(self, active_user_id: str, ctx: UserPolicyContext) -> None:
        if ctx.user_id != active_user_id:
            raise PolicyViolationError(["CROSS_TENANT_CONTEXT_REFUSED"])
