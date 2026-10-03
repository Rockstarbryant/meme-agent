"""Established (not freshly launched) token: liquidity_trend -> risk -> AI BUY -> paper position -> monitored -> exits."""
from datetime import timedelta

from app.core.types import Action, ExitReason, OrderStatus
from app.strategies.liquidity_trend import LiquidityTrend
from tests.conftest import NOW, good_market, make_rig


def established(**over):
    base = dict(token_created_at=NOW - timedelta(days=5), market_cap=600_000, liquidity=120_000, holder_count=900,
                top10_holder_pct=35, volume_5m=9_000, buys_5m=30, sells_5m=18, unique_buyers_5m=14,
                price_change_5m=3.0, price_change_15m=6.0, volatility_pct=12)
    base.update(over)
    return good_market(**base)


def lt_rig(market=None):
    rig = make_rig(market=market or established())
    rig.strategy = LiquidityTrend()
    rig.pipeline.strategy = rig.strategy
    return rig


async def test_established_token_passes_and_is_bought_monitored_and_sold():
    rig = lt_rig()
    rec = await rig.engine.handle_market_state(rig.market, NOW)
    assert rec.final_action == Action.BUY, rec.final_reason
    assert rig.provider.calls == 1                       # AI was consulted
    assert rig.portfolio.open_count() == 1
    pos = rig.portfolio.open_positions()[0]

    rig.data.set(established(price=1.4))                 # +40%: first take-profit tier
    res = await rig.engine.monitor_positions(rig.clock.advance(60))
    assert [r.status for r in res] == [OrderStatus.FILLED] and pos.tiers_hit == [0]

    rig.data.set(established(price=1.6))
    await rig.engine.monitor_positions(rig.clock.advance(60))
    rig.data.set(established(price=1.25))                # >20% off the peak: trailing stop closes the rest
    await rig.engine.monitor_positions(rig.clock.advance(60))
    assert not pos.is_open and pos.exit_reason == ExitReason.TRAILING_STOP


async def test_dormant_token_is_not_bought_even_with_deep_liquidity():
    rig = lt_rig(established(volume_5m=0, buys_5m=0, sells_5m=0, unique_buyers_5m=0, price_change_5m=0.0,
                             price_change_15m=0.0))
    rec = await rig.engine.handle_market_state(rig.market, NOW)
    assert rig.provider.calls == 1                       # WATCH-band tokens are still sent to the AI...
    assert rec.final_action == Action.WATCH and "hard gate failed" in rec.final_reason  # ...but cannot be upgraded
    assert rig.portfolio.open_count() == 0
    sig = rig.strategy.score(rig.market, NOW)
    assert sig.gates["recent_activity"] is False and not sig.qualified
