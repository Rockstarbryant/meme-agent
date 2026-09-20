from datetime import timedelta

import pytest

from app.core.errors import ApprovalError
from app.core.types import ExitReason, OrderStatus, RiskDecision, Side, TradingMode
from app.domain.trade import ApprovedTrade, TradeRequest
from app.execution.paper import PaperExecutionEngine
from app.portfolio.exits import ExitConfig, PositionManager
from app.risk.approval import TradeApprover
from tests.conftest import NOW, SECRET, TOKEN, good_market, make_rig


def buy_req(amount=25.0, key="k1", slip=2.0, mode=TradingMode.PAPER):
    return TradeRequest(idempotency_key=key, mode=mode, chain="arc", token_address=TOKEN, side=Side.BUY,
                        amount_usdc=amount, max_slippage_pct=slip, reference_price=1.0)


# ---------------- paper execution ----------------
async def test_paper_buy_is_simulated_with_fees_and_slippage():
    rig = make_rig()
    r = await rig.executor.buy(rig.approver.approve_exit(buy_req()))
    assert r.status == OrderStatus.FILLED and r.simulated and r.tx_hash is None and "PAPER" in r.label
    assert r.fee_usdc == pytest.approx(25 * 0.003) and r.avg_price > 1.0 and r.slippage_pct > 0
    assert rig.portfolio.cash_usdc == pytest.approx(975) and rig.portfolio.open_count() == 1


async def test_paper_engine_has_no_chain_or_wallet_dependency():
    rig = make_rig()
    assert not any(hasattr(rig.executor, a) for a in ("chain", "wallet", "rpc"))


async def test_paper_buy_failure_modes_do_not_move_money():
    rig = make_rig()
    assert (await rig.executor.buy(rig.approver.approve_exit(buy_req(amount=5000)))).error == "INSUFFICIENT_FUNDS"
    tiny = make_rig(market=good_market(liquidity=1_000))
    r = await tiny.executor.buy(tiny.approver.approve_exit(buy_req(amount=500, slip=1.0, key="x")))
    assert r.status == OrderStatus.PARTIALLY_FILLED or r.error == "SLIPPAGE_EXCEEDED"
    missing = make_rig()
    missing.data.m.clear()
    assert (await missing.executor.buy(missing.approver.approve_exit(buy_req()))).error == "NO_MARKET_DATA"
    assert missing.portfolio.cash_usdc == 1000


async def test_paper_idempotent_and_rejects_forged_approval():
    rig = make_rig()
    a = rig.approver.approve_exit(buy_req())
    r1, r2 = await rig.executor.buy(a), await rig.executor.buy(a)
    assert r1.order_id == r2.order_id and rig.portfolio.open_count() == 1
    forged = TradeApprover("some-other-secret-0123456789").approve_exit(buy_req(key="k2"))
    with pytest.raises(ApprovalError):
        await rig.executor.buy(forged)
    with pytest.raises(ApprovalError):
        await rig.executor.buy(rig.approver.approve_exit(buy_req(key="k3", mode=TradingMode.LIVE)))


def test_approver_refuses_rejected_assessment():
    rig = make_rig()
    from app.risk.engine import RiskContext, RiskEngine, TradeIntent
    from app.portfolio.controls import ControlState
    ctx = RiskContext(good_market(liquidity=1), TradeIntent(Side.BUY, 25, 2, TradingMode.PAPER, "s"), rig.portfolio,
                      rig.limits, ControlState(), NOW)
    a = RiskEngine().assess(ctx)
    assert a.decision == RiskDecision.REJECT
    with pytest.raises(ApprovalError):
        rig.approver.approve(buy_req(), a)


# ---------------- exits ----------------
async def open_pos(rig=None):
    rig = rig or make_rig()
    await rig.executor.buy(rig.approver.approve_exit(buy_req()))
    return rig, rig.portfolio.open_positions()[0]


async def test_hard_stop_at_minus_20():
    rig, pos = await open_pos()
    pos.last_price = pos.entry_price * 0.79
    d = rig.exits.evaluate(pos, NOW)
    assert d[0].reason == ExitReason.HARD_STOP and d[0].close_all
    pos.last_price = pos.entry_price * 0.85
    assert rig.exits.evaluate(pos, NOW) == []


async def test_take_profit_ladder_and_no_double_fire():
    rig, pos = await open_pos()
    pos.last_price = pos.peak_price = pos.entry_price * 1.35
    d = rig.exits.evaluate(pos, NOW)
    assert [x.tier_index for x in d] == [0] and d[0].quantity == pytest.approx(pos.initial_quantity * 0.15)
    pos.tiers_hit.append(0)
    assert rig.exits.evaluate(pos, NOW) == []
    pos.last_price = pos.peak_price = pos.entry_price * 2.1  # crosses tiers 1,2 (60%,100%)
    assert [x.tier_index for x in rig.exits.evaluate(pos, NOW)] == [1, 2]


async def test_trailing_stop_only_after_armed():
    rig, pos = await open_pos()
    pos.peak_price, pos.last_price = pos.entry_price * 1.15, pos.entry_price * 0.98  # >20% off peak? no: 14.8% -> below
    assert rig.exits.evaluate(pos, NOW) == []
    pos.peak_price, pos.last_price = pos.entry_price * 1.5, pos.entry_price * 1.15  # armed, 23% off peak
    d = rig.exits.evaluate(pos, NOW)
    assert d[0].reason == ExitReason.TRAILING_STOP and d[0].close_all


async def test_stagnation_exit():
    rig, pos = await open_pos()
    later = NOW + timedelta(seconds=1900)
    d = rig.exits.evaluate(pos, later)
    assert d and d[0].reason == ExitReason.STAGNATION
    pos.last_price = pos.peak_price = pos.entry_price * 1.05
    pos.last_new_high_at = later  # recent progress
    assert rig.exits.evaluate(pos, later) == []


async def test_momentum_and_liquidity_deterioration_and_manual_emergency():
    rig, pos = await open_pos()
    weak = good_market(price_change_5m=-12, buy_volume_5m=3000, sell_volume_5m=9000)
    assert rig.exits.evaluate(pos, NOW, weak)[0].reason == ExitReason.MOMENTUM_DETERIORATION
    assert rig.exits.evaluate(pos, NOW, good_market(liquidity_change_5m_pct=-60))[0].reason == ExitReason.LIQUIDITY_DETERIORATION
    assert rig.exits.evaluate(pos, NOW, manual_close=True)[0].reason == ExitReason.MANUAL
    assert rig.exits.evaluate(pos, NOW, emergency=True)[0].reason == ExitReason.EMERGENCY
    assert rig.exits.evaluate(pos, NOW, risk_escalation="creator dumping")[0].reason == ExitReason.RISK_ESCALATION


async def test_paper_partial_then_full_sell_updates_pnl():
    rig, pos = await open_pos()
    rig.data.set(good_market(price=1.5))
    sell = TradeRequest(idempotency_key="s1", mode=TradingMode.PAPER, chain="arc", token_address=TOKEN, side=Side.SELL,
                        quantity=pos.quantity / 2, max_slippage_pct=15, position_id=pos.id)
    r = await rig.executor.sell(rig.approver.approve_exit(sell))
    assert r.status == OrderStatus.FILLED and rig.portfolio.realized_total > 0 and pos.is_open
    sell2 = sell.model_copy(update={"idempotency_key": "s2", "quantity": pos.quantity})
    await rig.executor.sell(rig.approver.approve_exit(sell2))
    assert not pos.is_open and pos.realized_pnl_usdc > 0
