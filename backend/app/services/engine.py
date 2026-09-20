from __future__ import annotations

from datetime import datetime

from app.chains.base import MarketDataProvider
from app.core.clock import utcnow
from app.core.errors import DataUnavailable
from app.core.types import Action, ExitReason, OrderStatus, Side, TradingMode
from app.domain.market import MarketState
from app.domain.trade import ExecutionResult, TradeRequest
from app.events.bus import EventBus, EventType as E, IdempotencyStore
from app.execution.base import ExecutionAdapter
from app.execution.live import assert_executor_matches_mode
from app.portfolio.controls import ControlState
from app.portfolio.exits import ExitDecision, PositionManager
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover
from app.services.decision import DecisionPipeline, DecisionRecord

_EXIT_EVENT = {ExitReason.HARD_STOP: E.STOP_LOSS_TRIGGERED, ExitReason.TAKE_PROFIT: E.TAKE_PROFIT_TRIGGERED,
               ExitReason.TRAILING_STOP: E.TRAILING_STOP_TRIGGERED}
_DONE = (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
_RETRYABLE = (OrderStatus.FAILED, OrderStatus.REJECTED)


class TradingEngine:
    """Orchestrates entries and exits. Exits never touch the AI layer, so an AI outage cannot stop protection."""

    def __init__(self, *, mode: TradingMode, portfolio: PortfolioState, controls: ControlState,
                 pipeline: DecisionPipeline, executor: ExecutionAdapter, approver: TradeApprover,
                 exit_manager: PositionManager, market_data: MarketDataProvider, bus: EventBus,
                 idempotency: IdempotencyStore):
        assert_executor_matches_mode(mode, executor)
        self.mode, self.portfolio, self.controls = mode, portfolio, controls
        self.pipeline, self.executor, self.approver = pipeline, executor, approver
        self.exits, self.market_data, self.bus, self.idem = exit_manager, market_data, bus, idempotency

    # ------------------------------------------------------------ entries
    async def handle_market_state(self, m: MarketState, now: datetime | None = None) -> DecisionRecord:
        now = now or utcnow()
        rec = await self.pipeline.evaluate(m, self.portfolio, self.controls, self.mode, now)
        cid = rec.id
        await self.bus.publish(E.STRATEGY_SIGNAL_CREATED, cid, score=rec.signal.score, qualified=rec.signal.qualified,
                               version=rec.strategy_version)
        await self.bus.publish(E.RISK_ASSESSMENT_CREATED, cid, decision=rec.pre_risk.decision.value,
                               vetoes=rec.pre_risk.summary())
        if rec.ai is not None:
            await self.bus.publish(E.AI_ANALYSIS_COMPLETED, cid, status=rec.ai.status, provider=rec.ai.provider,
                                   model=rec.ai.model, prompt_version=rec.ai.prompt_version)
        await self.bus.publish(E.DECISION_RECORDED, cid, action=rec.final_action.value, reason=rec.final_reason,
                               token=rec.token_key, mode=self.mode.value, decision=rec.model_dump(mode="json", exclude={"approved_trade"}))
        if rec.final_action == Action.BUY and rec.approved_trade is not None:
            await self._enter(rec)
        elif rec.final_action == Action.REJECT:
            await self.bus.publish(E.BUY_REJECTED, cid, reason=rec.final_reason)
        return rec

    async def _enter(self, rec: DecisionRecord) -> ExecutionResult | None:
        approved = rec.approved_trade
        assert approved is not None
        req, cid = approved.request, rec.id
        key, pend = req.idempotency_key, f"{req.chain}:{req.token_address.lower()}"
        if not await self.idem.claim(key):
            await self.bus.publish(E.ORDER_FAILED, cid, error="DUPLICATE_ORDER", key=key)
            return None
        self.portfolio.pending_tokens.add(pend)
        try:
            await self.bus.publish(E.BUY_APPROVED, cid, amount_usdc=req.amount_usdc, mode=self.mode.value)
            await self.bus.publish(E.ORDER_SUBMITTED, cid, key=key, mode=self.mode.value)
            res = await self.executor.buy(approved)
        except Exception as exc:  # noqa: BLE001
            await self.idem.release(key)
            await self.bus.publish(E.AGENT_ERROR, cid, error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            self.portfolio.pending_tokens.discard(pend)
        await self._after_order(res, cid, opened=True)
        return res

    async def _after_order(self, res: ExecutionResult, cid: str, opened: bool) -> None:
        key = res.idempotency_key
        if res.status in _DONE:
            await self.idem.complete(key, res.model_dump(mode="json"))
            await self.bus.publish(E.ORDER_FILLED, cid, order=res.model_dump(mode="json"))
            if opened:
                await self.bus.publish(E.POSITION_OPENED, cid, position_id=res.position_id, simulated=res.simulated)
        elif res.status in _RETRYABLE:
            await self.idem.release(key)
            await self.bus.publish(E.ORDER_FAILED, cid, order=res.model_dump(mode="json"))
        else:  # TIMEOUT / PENDING_SIGNATURE / SUBMITTED: keep the claim; must be reconciled, never blindly retried
            await self.bus.publish(E.ORDER_SUBMITTED, cid, order=res.model_dump(mode="json"))

    # ------------------------------------------------------------- exits
    async def monitor_positions(self, now: datetime | None = None, *, manual_close: set[str] | None = None,
                                emergency_close: bool = False) -> list[ExecutionResult]:
        now, out = now or utcnow(), []
        for pos in list(self.portfolio.open_positions()):
            try:
                m = await self.market_data.get_market_state(pos.token_address)
            except DataUnavailable as exc:
                await self.bus.publish(E.RISK_ALERT, pos.id, kind="PRICE_UNAVAILABLE", error=str(exc))
                continue
            if m.price and m.price > 0:
                self.portfolio.mark(pos.id, m.price, now)
            for d in self.exits.evaluate(pos, now, m, manual_close=pos.id in (manual_close or set()),
                                         emergency=emergency_close):
                res = await self._exit(pos.id, d, m)
                if res is not None:
                    out.append(res)
            await self.bus.publish(E.POSITION_UPDATED, pos.id, price=pos.last_price, pnl=round(pos.unrealized_pnl, 6))
        return out

    async def _exit(self, position_id: str, d: ExitDecision, m: MarketState) -> ExecutionResult | None:
        pos = self.portfolio.positions[position_id]
        kind = "all" if d.close_all else f"tier{d.tier_index}"
        req = TradeRequest(idempotency_key=f"exit:{pos.id}:{kind}", mode=self.mode, chain=pos.chain,
                           token_address=pos.token_address, side=Side.SELL, quantity=d.quantity,
                           max_slippage_pct=self.exits.cfg.exit_max_slippage_pct, reference_price=m.price,
                           launchpad=pos.launchpad, pool_address=m.pool_address, position_id=pos.id,
                           strategy_id=pos.strategy_id, strategy_version=pos.strategy_version,
                           reason=f"{d.reason.value}: {d.detail}")
        if not await self.idem.claim(req.idempotency_key):
            return None
        await self.bus.publish(_EXIT_EVENT.get(d.reason, E.POSITION_UPDATED), pos.id, reason=d.reason.value, detail=d.detail)
        try:
            res = await self.executor.sell(self.approver.approve_exit(req))
        except Exception as exc:  # noqa: BLE001
            await self.idem.release(req.idempotency_key)
            await self.bus.publish(E.AGENT_ERROR, pos.id, error=f"{type(exc).__name__}: {exc}")
            return None
        await self._after_order(res, pos.id, opened=False)
        if res.status in _DONE:
            if d.tier_index is not None:
                pos.tiers_hit.append(d.tier_index)
            if not pos.is_open:
                pos.exit_reason = d.reason
                await self.bus.publish(E.POSITION_CLOSED, pos.id, reason=d.reason.value,
                                       realized_pnl=round(pos.realized_pnl_usdc, 6))
        return res

    # ---------------------------------------------------------- controls
    async def set_emergency_stop(self, enabled: bool, actor: str = "user") -> None:
        self.controls.emergency_stop = enabled
        await self.bus.publish(E.EMERGENCY_STOP_CHANGED, "", enabled=enabled, actor=actor)
