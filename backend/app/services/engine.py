from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.ai.exit_analyzer import AIExitAnalyzer
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
log = logging.getLogger(__name__)

# Last AI exit review per position id. Module-level on purpose: the cloud worker builds a fresh engine every cycle,
# and the throttle must survive that or every 30 s cycle would call the LLM again for the same position.
_AI_EXIT_LAST: dict[str, datetime] = {}
# token key -> time until which new LIVE entries are skipped after the safety pre-flight rejected an order for it.
# Module-level for the same reason: the cloud worker rebuilds the engine every cycle.
_ENTRY_BLOCK_UNTIL: dict[str, datetime] = {}


class TradingEngine:
    """Orchestrates entries and exits.

    Protective exits are deterministic and run FIRST for every position, so an AI outage or a slow model can never
    stop or delay protection. The optional AI exit reviewer (ai_exit_mode "shadow" | "live") runs afterwards, only on
    positions the rules left alone, and can only sell earlier."""

    def __init__(self, *, mode: TradingMode, portfolio: PortfolioState, controls: ControlState,
                 pipeline: DecisionPipeline, executor: ExecutionAdapter, approver: TradeApprover,
                 exit_manager: PositionManager, market_data: MarketDataProvider, bus: EventBus,
                 idempotency: IdempotencyStore, exit_managers: dict[str, PositionManager] | None = None,
                 ai_exit: AIExitAnalyzer | None = None, ai_exit_mode: str = "off",
                 ai_exit_min_confidence: float = 0.7, ai_exit_interval_s: float = 300.0,
                 ai_exit_timeout_s: float = 30.0, live_reject_cooldown_s: float = 600.0, scope: str = ""):
        assert_executor_matches_mode(mode, executor)
        self.mode, self.portfolio, self.controls = mode, portfolio, controls
        self.pipeline, self.executor, self.approver = pipeline, executor, approver
        self.exits, self.market_data, self.bus, self.idem = exit_manager, market_data, bus, idempotency
        # Exit rules per strategy id: a position is managed by the rules of the strategy that OPENED it, not by
        # whichever strategy happens to be active now (a launch-token stagnation rule must not close a swing trade).
        self.exit_managers = dict(exit_managers or {})
        self.ai_exit = ai_exit
        self.ai_exit_mode = ai_exit_mode if ai_exit_mode in ("off", "shadow", "live") and ai_exit is not None else "off"
        self.ai_exit_min_confidence = ai_exit_min_confidence
        self.ai_exit_interval_s = max(30.0, float(ai_exit_interval_s))
        self.ai_exit_timeout_s = max(5.0, float(ai_exit_timeout_s))
        self.live_reject_cooldown_s = max(0.0, float(live_reject_cooldown_s))
        self.scope = scope   # tenant id: the rejection cooldown must never leak from one user to another

    def exit_manager_for(self, strategy_id: str | None) -> PositionManager:
        return self.exit_managers.get(strategy_id or "", self.exits)

    # ------------------------------------------------------------ entries
    async def handle_market_state(self, m: MarketState, now: datetime | None = None) -> DecisionRecord:
        now = now or utcnow()
        rec = await self.pipeline.evaluate(m, self.portfolio, self.controls, self.mode, now)
        rec, _ = await self._finalize(rec)
        return rec

    async def force_buy(self, m: MarketState, amount_override: float | None = None,
                        now: datetime | None = None) -> ExecutionResult | None:
        """Manual 'BUY ANYWAY' override for a WATCH-listed opportunity (see
        DecisionPipeline.evaluate_manual_override for exactly what this does
        and does not skip). Returns None if the risk engine still vetoes the
        trade or the order could not be placed; either way the decision is
        recorded/published like any other, so it shows up in Activity."""
        now = now or utcnow()
        rec = await self.pipeline.evaluate_manual_override(m, self.portfolio, self.controls, self.mode, now,
                                                            amount_override)
        self.last_manual_reason = rec.final_reason   # lets the runner tell the user WHY a forced buy was refused
        _, res = await self._finalize(rec)
        return res

    async def _finalize(self, rec: DecisionRecord) -> tuple[DecisionRecord, ExecutionResult | None]:
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
            res = await self._enter(rec)
            return rec, res
        if rec.final_action == Action.REJECT:
            await self.bus.publish(E.BUY_REJECTED, cid, reason=rec.final_reason)
        return rec, None

    async def _enter(self, rec: DecisionRecord) -> ExecutionResult | None:
        approved = rec.approved_trade
        assert approved is not None
        req, cid = approved.request, rec.id
        key, pend = req.idempotency_key, f"{req.chain}:{req.token_address.lower()}"
        until = _ENTRY_BLOCK_UNTIL.get(f"{self.scope}|{pend}")
        if until is not None and utcnow() < until:
            await self.bus.publish(E.BUY_REJECTED, cid, reason=f"ENTRY_COOLDOWN_AFTER_LIVE_REJECTION until {until:%H:%M:%S}")
            return None
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
            if (opened and self.live_reject_cooldown_s > 0 and res.status == OrderStatus.REJECTED
                    and str(res.error or "").startswith("LiveSafetyError")):
                _ENTRY_BLOCK_UNTIL[f"{self.scope}|arc:{res.token_address.lower()}"] = utcnow() + timedelta(seconds=self.live_reject_cooldown_s)
            await self.bus.publish(E.ORDER_FAILED, cid, order=res.model_dump(mode="json"))
        else:  # TIMEOUT / PENDING_SIGNATURE / SUBMITTED: keep the claim; must be reconciled, never blindly retried
            await self.bus.publish(E.ORDER_SUBMITTED, cid, order=res.model_dump(mode="json"))

    # ------------------------------------------------------------- exits
    async def monitor_positions(self, now: datetime | None = None, *, manual_close: set[str] | None = None,
                                emergency_close: bool = False) -> list[ExecutionResult]:
        now, out = now or utcnow(), []
        review: list[tuple] = []   # positions the rules left alone: candidates for the AI exit reviewer
        for pos in list(self.portfolio.open_positions()):
            try:
                m = await self.market_data.get_market_state(pos.token_address)
            except DataUnavailable as exc:
                await self.bus.publish(E.RISK_ALERT, pos.id, kind="PRICE_UNAVAILABLE", error=str(exc))
                continue
            if m.price and m.price > 0:
                self.portfolio.mark(pos.id, m.price, now)
            decisions = self.exit_manager_for(pos.strategy_id).evaluate(
                pos, now, m, manual_close=pos.id in (manual_close or set()), emergency=emergency_close)
            for d in decisions:
                res = await self._exit(pos.id, d, m)
                if res is not None:
                    out.append(res)
            if not decisions and pos.is_open and not emergency_close and not (manual_close and pos.id in manual_close):
                review.append((pos, m))
            await self.bus.publish(E.POSITION_UPDATED, pos.id, price=pos.last_price, pnl=round(pos.unrealized_pnl, 6))
        # Only now, with every rule exit already executed, is the (possibly slow) AI consulted.
        if self.ai_exit_mode != "off":
            live_ids = {p.id for p in self.portfolio.open_positions()}
            for stale in [k for k in _AI_EXIT_LAST if k not in live_ids]:
                _AI_EXIT_LAST.pop(stale, None)
            for pos, m in review:
                if not pos.is_open or pos.quantity <= 0:
                    continue
                res = await self._ai_exit_review(pos, m, now)
                if res is not None:
                    out.append(res)
        return out

    async def _ai_exit_review(self, pos, m: MarketState, now: datetime) -> ExecutionResult | None:
        """Ask the model about one open position. Anything unexpected means HOLD. Never raises."""
        assert self.ai_exit is not None
        last = _AI_EXIT_LAST.get(pos.id)
        if last is not None and (now - last).total_seconds() < self.ai_exit_interval_s:
            return None
        _AI_EXIT_LAST[pos.id] = now
        cfg = self.exit_manager_for(pos.strategy_id).cfg
        from app.observability.audit import AuditKind, AuditStatus, audit_scope, record_event
        try:
            with audit_scope(user_id=self.scope if self.scope != "local" else "", token_key=m.key, decision_id=f"exit:{pos.id}"):
                outcome = await asyncio.wait_for(self.ai_exit.analyze(pos, m, cfg, now), self.ai_exit_timeout_s)
        except Exception as exc:  # noqa: BLE001 - includes timeout
            log.warning("AI exit review failed for %s: %s: %s", pos.id, type(exc).__name__, exc)
            record_event(AuditKind.AI_AGENT, AuditStatus.FAILED, operation="exit_review", component="engine.ai_exit",
                         error=f"{type(exc).__name__}: {exc}", token_key=m.key, decision_id=f"exit:{pos.id}",
                         user_id=self.scope if self.scope != "local" else "")
            return None
        d = outcome.decision
        record_event(AuditKind.AI_AGENT, AuditStatus.OK if outcome.status == "OK" else AuditStatus.FAILED,
                     provider=outcome.provider, model=outcome.model, operation="exit_review", component="engine.ai_exit",
                     error="" if outcome.status == "OK" else f"{outcome.status}: {outcome.error}", token_key=m.key,
                     decision_id=f"exit:{pos.id}", user_id=self.scope if self.scope != "local" else "",
                     detail={"action": getattr(d, "action", None), "confidence": getattr(d, "confidence", None),
                             "gain_pct": round(pos.gain_pct, 2)})
        if outcome.status != "OK" or d is None:
            log.warning("AI exit review %s for %s: %s", outcome.status, pos.id, outcome.error)
            return None
        if d.action == "HOLD" or d.confidence < self.ai_exit_min_confidence:
            log.info("AI exit review: HOLD %s (action=%s conf=%.2f) %s", pos.id, d.action, d.confidence,
                     d.reasoning_summary[:120])
            return None
        if d.action == "SELL_ALL":
            qty, kind = pos.quantity, "all"
        else:   # SELL_PARTIAL: clamp so a partial is never trivially small and never a hidden full exit
            frac = min(0.9, max(0.1, d.sell_fraction))
            qty, kind = pos.quantity * frac, f"{frac:.2f}"
        audit = dict(kind="AI_EXIT_SHADOW" if self.ai_exit_mode == "shadow" else "AI_EXIT", action=d.action,
                     confidence=round(d.confidence, 3), sell_fraction=round(qty / pos.quantity, 4),
                     reasoning=d.reasoning_summary[:500], gain_pct=round(pos.gain_pct, 2),
                     provider=outcome.provider, model=outcome.model, prompt_version=outcome.prompt_version)
        await self.bus.publish(E.RISK_ALERT, pos.id, **audit)
        if self.ai_exit_mode == "shadow":
            log.info("AI exit SHADOW (no order): %s %s conf=%.2f", d.action, pos.id, d.confidence)
            return None
        bucket = int(now.timestamp() // self.ai_exit_interval_s)
        decision = ExitDecision(position_id=pos.id, reason=ExitReason.AI_EXIT, close_all=d.action == "SELL_ALL",
                                quantity=qty, detail=d.reasoning_summary[:200] or d.action,
                                key_suffix=f"ai{kind}{bucket}")
        return await self._exit(pos.id, decision, m)

    async def _exit(self, position_id: str, d: ExitDecision, m: MarketState) -> ExecutionResult | None:
        pos = self.portfolio.positions[position_id]
        kind = d.key_suffix or ("all" if d.close_all else f"tier{d.tier_index}")
        req = TradeRequest(idempotency_key=f"exit:{pos.id}:{kind}", mode=self.mode, chain=pos.chain,
                           token_address=pos.token_address, side=Side.SELL, quantity=d.quantity,
                           max_slippage_pct=self.exit_manager_for(pos.strategy_id).cfg.exit_max_slippage_pct, reference_price=m.price,
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
