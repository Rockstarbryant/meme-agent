import pytest

from app.ai.provider import AIProviderError
from app.core.types import Action, ExitReason, OrderStatus, TradingMode
from app.core.errors import ModeMismatchError
from app.events.bus import EventType as E
from app.execution.live import assert_executor_matches_mode
from app.portfolio.exits import ExitConfig
from app.risk.engine import RiskLimits
from tests.conftest import NOW, StubProvider, good_market, make_rig


def types(rig):
    return [e.type for e in rig.sink.events]


async def test_full_paper_lifecycle_with_audit_trail():
    rig = make_rig()
    rec = await rig.engine.handle_market_state(rig.market, NOW)
    assert rec.final_action == Action.BUY and rig.portfolio.open_count() == 1
    ts = types(rig)
    for t in (E.STRATEGY_SIGNAL_CREATED, E.RISK_ASSESSMENT_CREATED, E.AI_ANALYSIS_COMPLETED, E.DECISION_RECORDED,
              E.BUY_APPROVED, E.ORDER_SUBMITTED, E.ORDER_FILLED, E.POSITION_OPENED):
        assert t in ts
    # "why did the agent buy?" is reconstructable from the audit log by decision id
    trail = rig.sink.for_correlation(rec.id)
    dec = next(e for e in trail if e.type == E.DECISION_RECORDED).payload["decision"]
    assert dec["strategy_version"] == 1 and dec["ai"]["decision"]["action"] == "BUY" and dec["pre_risk"]["decision"] == "APPROVE"

    pos = rig.portfolio.open_positions()[0]
    rig.data.set(good_market(price=1.4))                       # +40% -> first take-profit tier
    res = await rig.engine.monitor_positions(rig.clock.advance(60))
    assert [r.status for r in res] == [OrderStatus.FILLED] and pos.tiers_hit == [0]
    assert E.TAKE_PROFIT_TRIGGERED in types(rig)
    assert (await rig.engine.monitor_positions(rig.clock.advance(60))) == []   # no double fire

    rig.data.set(good_market(price=1.6))                       # peak
    await rig.engine.monitor_positions(rig.clock.advance(60))
    rig.data.set(good_market(price=1.25))                      # >20% off peak -> trailing stop closes remainder
    await rig.engine.monitor_positions(rig.clock.advance(60))
    assert not pos.is_open and pos.exit_reason == ExitReason.TRAILING_STOP and pos.realized_pnl_usdc > 0
    assert E.TRAILING_STOP_TRIGGERED in types(rig) and E.POSITION_CLOSED in types(rig)


async def test_duplicate_entry_is_blocked():
    rig = make_rig()
    await rig.engine.handle_market_state(rig.market, NOW)
    rec2 = await rig.engine.handle_market_state(rig.market, NOW)
    assert rig.portfolio.open_count() == 1 and rec2.final_action == Action.REJECT
    # idempotency store: same key can't be claimed twice even if risk layer were bypassed
    key = rig.executor.orders[0].idempotency_key
    assert await rig.idem.claim(key) is False


async def test_emergency_stop_blocks_entries_but_protection_continues():
    rig = make_rig()
    await rig.engine.handle_market_state(rig.market, NOW)
    pos = rig.portfolio.open_positions()[0]
    await rig.engine.set_emergency_stop(True)
    other = good_market(token_address="0x2222222222222222222222222222222222222222")
    rig.data.set(other)
    rec = await rig.engine.handle_market_state(other, NOW)
    assert rec.final_action == Action.REJECT and "EMERGENCY_STOP" in rec.final_reason and rig.portfolio.open_count() == 1
    rig.data.set(good_market(price=0.7))                       # -30% -> hard stop must still fire
    await rig.engine.monitor_positions(rig.clock.advance(30))
    assert not pos.is_open and pos.exit_reason == ExitReason.HARD_STOP
    assert E.STOP_LOSS_TRIGGERED in types(rig)


async def test_ai_outage_does_not_disable_position_protection():
    rig = make_rig()
    await rig.engine.handle_market_state(rig.market, NOW)
    pos = rig.portfolio.open_positions()[0]
    rig.provider.response = AIProviderError("down")
    other = good_market(token_address="0x3333333333333333333333333333333333333333")
    rig.data.set(other)
    rec = await rig.engine.handle_market_state(other, NOW)
    assert rec.final_action == Action.WATCH and rig.portfolio.open_count() == 1   # no new AI-dependent entry
    rig.data.set(good_market(price=0.6))
    calls_before = rig.provider.calls
    await rig.engine.monitor_positions(rig.clock.advance(30))
    assert not pos.is_open and rig.provider.calls == calls_before               # exit never consulted the AI


async def test_daily_loss_limit_blocks_new_entries_after_losses():
    rig = make_rig(limits=RiskLimits(max_daily_loss_usdc=1.0))
    await rig.engine.handle_market_state(rig.market, NOW)
    rig.data.set(good_market(price=0.7))
    await rig.engine.monitor_positions(rig.clock.advance(30))
    assert rig.portfolio.realized_today < -1.0
    other = good_market(token_address="0x4444444444444444444444444444444444444444", price=1.0)
    rig.data.set(other)
    rec = await rig.engine.handle_market_state(other, rig.clock.advance(1000))
    assert rec.final_action == Action.REJECT and "DAILY_LOSS_LIMIT" in rec.final_reason


async def test_failed_exit_can_be_retried_and_missing_price_raises_alert():
    rig = make_rig()
    await rig.engine.handle_market_state(rig.market, NOW)
    pos = rig.portfolio.open_positions()[0]
    saved = rig.data.m.copy()
    rig.data.m.clear()                                          # data outage
    assert await rig.engine.monitor_positions(rig.clock.advance(30)) == []
    assert E.RISK_ALERT in types(rig) and pos.is_open
    rig.data.m.update(saved)
    rig.data.set(good_market(price=0.5))
    await rig.engine.monitor_positions(rig.clock.advance(30))
    assert not pos.is_open


def test_mode_mismatch_is_impossible():
    rig = make_rig()
    with pytest.raises(ModeMismatchError):
        assert_executor_matches_mode(TradingMode.LIVE, rig.executor)
    with pytest.raises(ModeMismatchError):
        make_rig(mode=TradingMode.LIVE)  # paper executor wired into a LIVE engine
