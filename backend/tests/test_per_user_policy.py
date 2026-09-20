"""Per-user policy enforcement: platform ceilings, binding isolation, effective limits."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.errors import PolicyViolationError
from app.core.types import Side, WalletCapability
from app.domain.trade import UnsignedTransaction
from app.risk.engine import RiskLimits
from app.risk.per_user_policy import (
    DEFAULT_PLATFORM_CEILINGS,
    PerUserPolicyEnforcer,
    PlatformCeilings,
    UserPolicyContext,
    binding_violations,
    clamp_to_platform,
    effective_limits,
    reject_if_above_platform,
    scoped_idempotency_key,
)
from app.wallets.base import PolicyValidator, WalletAuthorization, WalletPolicy


def _policy(**kw) -> WalletPolicy:
    base = dict(
        allocated_capital_usdc=100,
        max_trade_usdc=25,
        max_position_usdc=50,
        max_daily_loss_usdc=50,
        max_open_positions=5,
        max_slippage_pct=2,
        min_liquidity_usdc=10_000,
        allowed_chains={"arc"},
        allowed_routers={"0x4fcA4a51Ab4F23A7447b3284fBd7D73289A89Fb1"},
    )
    base.update(kw)
    return WalletPolicy(**base)


def _tx(**kw) -> UnsignedTransaction:
    base = dict(
        chain="arc",
        chain_id=5042,
        to="0x4fcA4a51Ab4F23A7447b3284fBd7D73289A89Fb1",
        data="0x1234",
        value=0,
        token_address="0x1111111111111111111111111111111111111111",
        side=Side.BUY,
        amount_usdc=10,
        min_out=1,
        slippage_pct=1,
        deadline=datetime.now(timezone.utc),
        built_by="deterministic-adapter",
        function_signature="execute(bytes,bytes[])",
        params=[],
    )
    base.update(kw)
    return UnsignedTransaction(**base)


def _auth(user_id: str = "user-a", **kw) -> WalletAuthorization:
    p = kw.pop("policy", _policy())
    return WalletAuthorization(
        wallet_id=kw.pop("wallet_id", "wal-a"),
        provider=kw.pop("provider", "privy"),
        capability=WalletCapability.AUTONOMOUS_DELEGATED,
        policy=p,
        granted_at=datetime.now(timezone.utc),
        user_id=user_id,
        wallet_address=kw.pop("wallet_address", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        **kw,
    )


def _ctx(user_id: str = "user-a", **kw) -> UserPolicyContext:
    return UserPolicyContext(
        user_id=user_id,
        wallet_id=kw.pop("wallet_id", "wal-a"),
        wallet_address=kw.pop("wallet_address", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        wallet_provider=kw.pop("wallet_provider", "privy"),
        risk_limits=kw.pop("risk_limits", RiskLimits(max_trade_usdc=25)),
        wallet_policy=kw.pop("wallet_policy", _policy()),
        mode=kw.pop("mode", "LIVE"),
        **kw,
    )


def test_clamp_to_platform_caps_user_limits():
    high = RiskLimits(max_trade_usdc=10_000, max_position_usdc=20_000, max_total_exposure_usdc=50_000)
    out = clamp_to_platform(high, DEFAULT_PLATFORM_CEILINGS)
    assert out.max_trade_usdc == DEFAULT_PLATFORM_CEILINGS.max_trade_usdc
    assert out.max_position_usdc == DEFAULT_PLATFORM_CEILINGS.max_position_usdc


def test_reject_if_above_platform():
    high = RiskLimits(max_trade_usdc=500)
    bad = reject_if_above_platform(high, PlatformCeilings(max_trade_usdc=100, max_position_usdc=250, max_total_exposure_usdc=1000))
    assert any("max_trade_usdc" in x for x in bad)
    ok = reject_if_above_platform(RiskLimits(max_trade_usdc=10), DEFAULT_PLATFORM_CEILINGS)
    assert ok == []


def test_effective_limits_strictest_wins():
    user = RiskLimits(max_trade_usdc=40, min_liquidity_usdc=1_000)
    policy = _policy(max_trade_usdc=15, min_liquidity_usdc=20_000)
    platform = PlatformCeilings(max_trade_usdc=100, max_position_usdc=250, max_total_exposure_usdc=1000, min_liquidity_usdc=5_000)
    eff = effective_limits(user, policy, platform)
    assert eff.max_trade_usdc == 15
    assert eff.min_liquidity_usdc == 20_000


def test_binding_violations_cross_tenant():
    ctx = _ctx(user_id="user-a")
    assert binding_violations(ctx, expected_user_id="user-b") == ["USER_ID_MISMATCH"]
    auth = _auth(user_id="user-b")
    assert "AUTH_USER_ID_MISMATCH" in binding_violations(ctx, auth=auth)


def test_policy_validator_user_binding():
    now = datetime.now(timezone.utc)
    tx = _tx()
    auth = _auth(user_id="user-a")
    v = PolicyValidator.violations(tx, auth, now, expected_chain_id=5042, expected_user_id="user-b")
    assert "AUTH_USER_ID_MISMATCH" in v
    v_ok = PolicyValidator.violations(tx, auth, now, expected_chain_id=5042, expected_user_id="user-a")
    assert "AUTH_USER_ID_MISMATCH" not in v_ok


def test_enforcer_blocks_pause_and_wrong_user():
    enforcer = PerUserPolicyEnforcer()
    now = datetime.now(timezone.utc)
    ctx = _ctx(global_pause=True)
    with pytest.raises(PolicyViolationError) as ei:
        enforcer.authorize_trade(ctx, _tx(), _auth(), now, expected_chain_id=5042)
    assert "GLOBAL_PAUSE" in ei.value.violations

    ctx2 = _ctx(user_id="user-a")
    with pytest.raises(PolicyViolationError) as ei2:
        enforcer.authorize_trade(ctx2, _tx(), _auth(user_id="user-b"), now, expected_chain_id=5042)
    assert any("USER" in c or "AUTH" in c for c in ei2.value.violations)


def test_enforcer_allows_valid_trade():
    enforcer = PerUserPolicyEnforcer()
    now = datetime.now(timezone.utc)
    ctx = _ctx()
    auth = _auth()
    eff = enforcer.authorize_trade(ctx, _tx(amount_usdc=5), auth, now, expected_chain_id=5042)
    assert eff.max_trade_usdc <= 25


def test_scoped_idempotency_differs_per_user():
    a = scoped_idempotency_key("user-a", "same-raw")
    b = scoped_idempotency_key("user-b", "same-raw")
    assert a != b
    assert len(a) == 32


def test_assert_context_isolation():
    enforcer = PerUserPolicyEnforcer()
    ctx = _ctx(user_id="user-a")
    enforcer.assert_context_isolation("user-a", ctx)
    with pytest.raises(PolicyViolationError):
        enforcer.assert_context_isolation("user-b", ctx)
