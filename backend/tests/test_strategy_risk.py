from datetime import timedelta

import pytest

from app.core.types import RiskDecision, Side, TradingMode
from app.domain.market import ContractInfo
from app.portfolio.controls import ControlState
from app.portfolio.state import PortfolioState
from app.risk.engine import RiskContext, RiskEngine, RiskLimits, RiskRule, TradeIntent, max_entry_amount
from app.core.types import RiskCategory
from app.strategies.traction_momentum import (StrategyVersionStore, StrategyWeights, TractionMomentum,
                                              TractionMomentumConfig)
from tests.conftest import NOW, good_market


def assess(market=None, *, mode=TradingMode.PAPER, amount=25.0, limits=None, controls=None, portfolio=None, now=NOW):
    market = market or good_market()
    limits = limits or RiskLimits()
    ctx = RiskContext(market, TradeIntent(Side.BUY, amount, limits.max_slippage_pct, mode, "traction_momentum"),
                      portfolio or PortfolioState(1000), limits, controls or ControlState(), now)
    return RiskEngine().assess(ctx)


def vetoes(a):
    return {f.rule for f in a.vetoes}


# ---------------- strategy ----------------
def test_good_market_qualifies_and_records_version():
    s = TractionMomentum().score(good_market(), NOW)
    assert s.qualified and s.score >= 70
    assert s.strategy_id == "traction_momentum" and s.strategy_version == 1
    assert s.config_snapshot["weights"]["momentum"] == pytest.approx(0.25)


def test_weak_traction_does_not_qualify():
    s = TractionMomentum().score(good_market(unique_buyers_5m=2, buy_volume_5m=5000, sell_volume_5m=5000), NOW)
    assert not s.qualified and not s.gates["min_unique_buyers_5m"] and not s.gates["buy_sell_ratio"]


def test_brand_new_launch_is_not_bought_blindly():
    s = TractionMomentum().score(good_market(token_created_at=NOW - timedelta(seconds=30)), NOW)
    assert not s.qualified and not s.gates["min_age"]


def test_missing_data_is_flagged_and_never_qualifies_when_required():
    s = TractionMomentum().score(good_market(price_change_5m=None), NOW)
    assert "momentum" in s.data_gaps and not s.qualified


def test_unknown_creator_is_not_treated_as_good():
    known = TractionMomentum().score(good_market(), NOW)
    unknown = TractionMomentum().score(good_market(creator_known=False), NOW)
    assert unknown.components["creator_behavior"] < known.components["creator_behavior"]


def test_weights_configurable_and_normalised():
    w = StrategyWeights(momentum=50, buyer_growth=50, buy_sell_pressure=0, liquidity_quality=0,
                        holder_distribution=0, creator_behavior=0, market_quality=0)
    assert w.momentum == pytest.approx(0.5) and sum(w.model_dump().values()) == pytest.approx(1.0)


def test_strategy_versions_are_immutable():
    store = StrategyVersionStore()
    store.register(TractionMomentumConfig())
    with pytest.raises(ValueError):
        store.register(TractionMomentumConfig(min_score=99))
    store.register(TractionMomentumConfig(min_score=99, version=2))


# ---------------- risk ----------------
def test_clean_market_is_approved():
    assert assess().decision == RiskDecision.APPROVE


def test_liquidity_below_minimum_vetoes():
    assert "LIQUIDITY_BELOW_MIN" in vetoes(assess(good_market(liquidity=5_000)))


def test_emergency_stop_blocks_entry():
    assert "EMERGENCY_STOP" in vetoes(assess(controls=ControlState(emergency_stop=True)))


def test_daily_loss_limit_blocks_entry():
    p = PortfolioState(1000)
    p.daily_pnl(NOW)
    p.realized_today = -60
    assert "DAILY_LOSS_LIMIT" in vetoes(assess(portfolio=p))


def test_unrealized_gains_do_not_mask_daily_loss():
    p = PortfolioState(1000)
    p.daily_pnl(NOW)
    p.realized_today = -60
    pos = p.apply_buy(chain="arc", launchpad=None, token="0x1", symbol=None, qty=100, price=1, cost_usdc=50, now=NOW)
    p.mark(pos.id, 5.0, NOW)  # big unrealised gain
    assert p.daily_pnl(NOW) == -60


def test_launchpad_must_be_allowlisted_and_not_blacklisted():
    m = good_market(launchpad="somepad")
    assert "LAUNCHPAD_NOT_ALLOWED" in vetoes(assess(m))
    ok = assess(m, limits=RiskLimits(allowed_launchpads={"somepad"}))
    assert ok.decision == RiskDecision.APPROVE
    bl = assess(m, limits=RiskLimits(allowed_launchpads={"somepad"}), controls=ControlState(blacklisted_launchpads={"somepad"}))
    assert "LAUNCHPAD_BLACKLISTED" in vetoes(bl)


@pytest.mark.parametrize("over,rule", [
    (dict(timestamp=NOW - timedelta(minutes=5)), "STALE_DATA"),
    (dict(price=None), "PRICE_UNAVAILABLE"),
    (dict(top10_holder_pct=90), "TOP10_CONCENTRATION"),
    (dict(creator_sold_pct=60), "CREATOR_DUMPING"),
    (dict(mev_risk_score=0.95), "MEV_RISK_HIGH"),
    (dict(price_change_5m=400), "OVERHEATED_CHASE"),
    (dict(liquidity_change_5m_pct=-50), "LIQUIDITY_DRAINING"),
    (dict(token_created_at=NOW - timedelta(seconds=10)), "TOKEN_TOO_NEW"),
    (dict(is_demo=True), None),
])
def test_market_vetoes(over, rule):
    a = assess(good_market(**over), mode=TradingMode.LIVE if rule is None else TradingMode.PAPER)
    if rule is None:
        assert "DEMO_DATA_IN_LIVE" in vetoes(a)
    else:
        assert rule in vetoes(a)


NO_SELLERS = dict(unique_sellers_1m=0, unique_sellers_5m=0, sells_1m=0, sells_5m=0)


def test_contract_vetoes_apply_without_two_way_trading():
    bad = ContractInfo(mint_authority_active=True, blacklist_capability=True, sell_tax_pct=40, sell_simulation_ok=False)
    v = vetoes(assess(good_market(contract=bad, **NO_SELLERS)))
    assert {"MINT_AUTHORITY_ACTIVE", "BLACKLIST_CAPABILITY", "SELL_TAX_TOO_HIGH", "SELL_SIMULATION_FAILED"} <= v


def test_contract_flags_are_informational_once_buyers_and_sellers_are_seen():
    bad = ContractInfo(mint_authority_active=True, blacklist_capability=True, pausable=True, is_proxy=True,
                       verified=None, sell_tax_pct=40, sell_simulation_ok=False)
    a = assess(good_market(contract=bad))                       # fixture has buyers AND sellers
    v = vetoes(a)
    assert not ({"MINT_AUTHORITY_ACTIVE", "BLACKLIST_CAPABILITY", "PAUSABLE"} & v)
    assert {"SELL_TAX_TOO_HIGH", "SELL_SIMULATION_FAILED"} <= v   # measured facts still block
    info = {f.rule for f in a.flags if f.severity.value == "INFO"}
    assert {"MINT_AUTHORITY_ACTIVE", "BLACKLIST_CAPABILITY", "PAUSABLE", "UPGRADEABLE_PROXY",
            "CONTRACT_VERIFICATION_UNKNOWN"} <= info


def test_contract_only_issues_never_block_a_two_way_traded_token():
    m = good_market(contract=ContractInfo(verified=None, is_proxy=True, mint_authority_active=None, pausable=None))
    a = assess(m)
    assert a.decision == RiskDecision.APPROVE and a.risk_score == 0


def test_sellability_unknown_is_no_longer_flagged_and_one_sided_trading_is_vetoed():
    from app.domain.market import WindowStats
    m = good_market(contract=ContractInfo(verified=True, sell_simulation_ok=None))
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        a = assess(m, mode=mode)
        assert a.decision == RiskDecision.APPROVE
        assert "SELLABILITY_UNVERIFIED" not in {f.rule for f in a.flags}
    honeypot = good_market(windows={"24h": WindowStats(buys=40, sells=0, buyers=12, sellers=0)}, **NO_SELLERS)
    assert "ONE_SIDED_TRADING" in vetoes(assess(honeypot))
    quiet = good_market(windows={"24h": WindowStats(buys=2, sells=0, buyers=2, sellers=0)}, **NO_SELLERS)
    assert "ONE_SIDED_TRADING" not in vetoes(assess(quiet))     # too few buys to call it


def test_position_and_exposure_limits():
    p = PortfolioState(1000)
    for i in range(5):
        p.apply_buy(chain="arc", launchpad=None, token=f"0x{i}", symbol=None, qty=10, price=1, cost_usdc=10, now=NOW - timedelta(hours=1))
    assert "MAX_OPEN_POSITIONS" in vetoes(assess(portfolio=p))
    p2 = PortfolioState(1000)
    p2.apply_buy(chain="arc", launchpad=None, token=good_market().token_address, symbol=None, qty=10, price=1, cost_usdc=10, now=NOW - timedelta(hours=1))
    assert "DUPLICATE_POSITION" in vetoes(assess(portfolio=p2))
    assert "MAX_TRADE_EXCEEDED" in vetoes(assess(amount=500))
    assert "INSUFFICIENT_FUNDS" in vetoes(assess(portfolio=PortfolioState(5)))


def test_cooldown_blocks_reentry():
    p = PortfolioState(1000)
    pos = p.apply_buy(chain="arc", launchpad=None, token=good_market().token_address, symbol=None, qty=10, price=1, cost_usdc=10, now=NOW - timedelta(seconds=60))
    p.apply_sell(pos.id, 10, 1, 10, NOW - timedelta(seconds=30))
    assert "COOLDOWN_ACTIVE" in vetoes(assess(portfolio=p))


def test_slippage_request_above_limit_vetoes():
    ctx = RiskContext(good_market(), TradeIntent(Side.BUY, 25, 10.0, TradingMode.PAPER, "s"), PortfolioState(1000),
                      RiskLimits(), ControlState(), NOW)
    assert "SLIPPAGE_LIMIT_EXCEEDED" in vetoes(RiskEngine().assess(ctx))


def test_sells_are_never_vetoed_by_entry_rules():
    ctx = RiskContext(good_market(liquidity=1), TradeIntent(Side.SELL, 25, 15, TradingMode.PAPER, "s"), PortfolioState(1000),
                      RiskLimits(), ControlState(emergency_stop=True), NOW)
    assert RiskEngine().assess(ctx).decision == RiskDecision.APPROVE


def test_crashing_rule_fails_closed():
    def boom(ctx):
        raise RuntimeError("x")
    eng = RiskEngine([RiskRule("boom", RiskCategory.MARKET_RISK, boom)])
    ctx = RiskContext(good_market(), TradeIntent(Side.BUY, 25, 2, TradingMode.PAPER, "s"), PortfolioState(1000),
                      RiskLimits(), ControlState(), NOW)
    assert eng.assess(ctx).decision == RiskDecision.REJECT


def test_sizing_only_shrinks():
    m = good_market(liquidity=20_000)  # 1% of liquidity = $200 -> max trade $25 binds
    assert max_entry_amount(m, PortfolioState(1000), RiskLimits()) == 25
    assert max_entry_amount(m, PortfolioState(10), RiskLimits()) == 10
    assert max_entry_amount(good_market(liquidity=1000), PortfolioState(1000), RiskLimits()) == 10
