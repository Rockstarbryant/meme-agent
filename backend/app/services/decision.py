"""Stage 1 (deterministic) -> Stage 2 (AI) -> policy -> risk -> approved trade. Risk always wins."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.ai.agent import AIAgentAnalyzer, ToolContext
from app.ai.analyzer import AIAnalyzer
from app.ai.schemas import AIOutcome
from app.core.clock import utcnow
from app.core.types import AIMode, Action, RiskDecision, Side, TradingMode
from app.domain.market import MarketState
from app.domain.trade import ApprovedTrade, TradeRequest
from app.portfolio.controls import ControlState
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover
from app.risk.engine import RiskAssessment, RiskContext, RiskEngine, RiskLimits, TradeIntent, max_entry_amount
from app.observability.audit import audit_scope
from app.strategies.base import Strategy, StrategySignal
from app.wallets.base import WalletPolicy


class DecisionRecord(BaseModel):
    """Everything needed to answer: why did (or didn't) the agent buy this token?"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime
    mode: TradingMode
    token_key: str
    strategy_id: str
    strategy_version: int
    strategy_config: dict
    risk_limits: dict
    controls: dict
    wallet_policy: dict | None = None
    market: dict
    signal: StrategySignal
    pre_risk: RiskAssessment
    ai_mode: str
    ai: AIOutcome | None = None
    final_risk: RiskAssessment | None = None
    sized_amount_usdc: float = 0.0
    final_action: Action
    final_reason: str
    approved_trade: ApprovedTrade | None = None


def tighten_limits(limits: RiskLimits, policy: WalletPolicy | None) -> RiskLimits:
    """Effective limits are the STRICTER of app risk limits and the wallet policy.

    Shared with ``app.risk.per_user_policy.merge_wallet_policy`` so control plane
    and worker compose limits identically.
    """
    from app.risk.per_user_policy import merge_wallet_policy
    return merge_wallet_policy(limits, policy)


# Gates the AI can never override when upgrading a WATCH-band token to BUY (see DecisionPipeline.evaluate).
_HARD_GATES = ("price_present", "liquidity_present", "required_data_present", "min_age", "holder_not_extreme",
               "recent_activity", "two_way_trading")


class DecisionPipeline:
    def __init__(self, strategy: Strategy, risk: RiskEngine, limits: RiskLimits, approver: TradeApprover,
                 analyzer: AIAnalyzer, ai_mode: AIMode = AIMode.ENABLED, min_ai_confidence: float = 0.6,
                 min_order_usdc: float = 1.0, entry_window_seconds: int = 300,
                 wallet_policy: WalletPolicy | None = None):
        self.strategy, self.risk, self.limits, self.approver = strategy, risk, limits, approver
        self.analyzer, self.ai_mode, self.min_ai_conf = analyzer, ai_mode, min_ai_confidence
        self.min_order, self.window, self.policy = min_order_usdc, entry_window_seconds, wallet_policy
        # Set by the runtime after construction (keeps the constructor signature stable):
        self.venue_router = None            # app.venues.VenueRouter | None  (LIVE only)
        self.audit_user_id = ""             # tenant for the operational audit trail
        self.wallet_state_fn = None         # async () -> dict  (balance / exposure for the agent's get_wallet_state tool)
        self.refresh_market_fn = None       # async (MarketState) -> MarketState  (agent's get_market_data refresh=true)

    def _request(self, m: MarketState, amount: float, mode: TradingMode, now: datetime, decision_id: str,
                 slippage: float, limits: RiskLimits | None = None, venue: str | None = None) -> TradeRequest:
        bucket = int(now.timestamp() // self.window)
        raw = f"{mode.value}|{m.key}|{self.strategy.strategy_id}|{self.strategy.version}|{bucket}"
        return TradeRequest(idempotency_key=hashlib.sha256(raw.encode()).hexdigest()[:32], mode=mode, chain=m.chain,
                            token_address=m.token_address, side=Side.BUY, amount_usdc=amount, max_slippage_pct=slippage,
                            reference_price=m.price, reference_liquidity_usdc=m.liquidity, launchpad=m.launchpad, pool_address=m.pool_address,
                            decision_id=decision_id, strategy_id=self.strategy.strategy_id,
                            strategy_version=self.strategy.version, reason="traction_momentum_entry", venue=venue,
                            max_price_impact_pct=limits.max_price_impact_pct if limits else None,
                            max_total_cost_pct=getattr(limits, "max_total_cost_pct", None) if limits else None)

    def _assess(self, m, amount, mode, portfolio, controls, now, limits) -> RiskAssessment:
        intent = TradeIntent(Side.BUY, amount, limits.max_slippage_pct, mode, self.strategy.strategy_id)
        return self.risk.assess(RiskContext(m, intent, portfolio, limits, controls, now))

    async def evaluate(self, m: MarketState, portfolio: PortfolioState, controls: ControlState,
                       mode: TradingMode, now: datetime | None = None) -> DecisionRecord:
        decision_id = uuid.uuid4().hex
        # Everything recorded below (provider calls, AI attempts, tool calls, venue quotes) carries this decision + token.
        with audit_scope(user_id=self.audit_user_id, token_key=m.key, decision_id=decision_id):
            return await self._evaluate(m, portfolio, controls, mode, now, decision_id)

    async def _evaluate(self, m: MarketState, portfolio: PortfolioState, controls: ControlState,
                        mode: TradingMode, now: datetime | None, decision_id: str) -> DecisionRecord:
        now = now or utcnow()
        limits = tighten_limits(self.limits, self.policy)
        signal = self.strategy.score(m, now)                                   # feature/strategy stage
        amount = max_entry_amount(m, portfolio, limits)
        pre = self._assess(m, amount, mode, portfolio, controls, now, limits)  # risk filter stage

        def rec(action: Action, reason: str, **kw) -> DecisionRecord:
            market = m.model_dump(mode="json")
            # Ensure Opportunities "Scanned" is populated even when providers omit scanned_at.
            if not market.get("scanned_at"):
                ts = market.get("timestamp")
                market["scanned_at"] = ts if isinstance(ts, str) else (now.isoformat() if now else None)
            return DecisionRecord(id=decision_id, created_at=now, mode=mode, token_key=m.key, strategy_id=self.strategy.strategy_id,
                                  strategy_version=self.strategy.version, strategy_config=self.strategy.config_snapshot(),
                                  risk_limits=limits.model_dump(mode="json"), controls=controls.snapshot(),
                                  wallet_policy=self.policy.model_dump(mode="json") if self.policy else None,
                                  market=market, signal=signal, pre_risk=pre,
                                  ai_mode=self.ai_mode.value, final_action=action, final_reason=reason,
                                  sized_amount_usdc=kw.pop("amount", amount), **kw)

        if pre.decision == RiskDecision.REJECT:
            # Hard risk veto: never spend AI budget on vetoed tokens.
            return rec(Action.REJECT, f"RISK_VETO: {pre.summary()}")

        watch_band = False
        if not signal.qualified:
            watch_band = signal.score >= getattr(self.strategy, "cfg").watch_score
            if not watch_band:
                return rec(Action.REJECT, "; ".join(signal.reasons))
            # WATCH band: still send to AI when enabled so the model can confirm WATCH,
            # upgrade to BUY, or REJECT. Strategy reasons stay attached to the decision.

        ai_out: AIOutcome | None = None
        size_note = ""
        size_cap = amount
        strategy_note = "; ".join(signal.reasons) if signal.reasons else (
            "qualified signal" if signal.qualified else "watch-band signal"
        )
        if self.ai_mode == AIMode.DISABLED:
            # Record WHY the AI stage was skipped so the UI shows "DISABLED" + the fix,
            # instead of the misleading "Not consulted".
            off = AIOutcome(status="DISABLED", error="no LLM provider configured on this runner "
                            "(set ARC_RUNNER_LLM_PROVIDER and the matching API key)")
            if mode == TradingMode.LIVE:
                return rec(Action.WATCH, "AI_REQUIRED_FOR_LIVE_ENTRIES", ai=off)
            if watch_band:
                return rec(Action.WATCH, f"AI_DISABLED: {strategy_note}", ai=off)
            # qualified + AI disabled + PAPER → fall through to deterministic BUY below
        else:
            # AI stage: qualified signals and WATCH-band signals (not hard REJECT).
            if amount < self.min_order:   # nothing the AI says can fit: say so precisely instead of asking it
                return rec(Action.WATCH, f"MAX_ENTRY_BELOW_MINIMUM_ORDER: max {amount:.2f} < min {self.min_order:.2f} USDC",
                           amount=amount)
            sizing = {"min_order_usdc": self.min_order, "max_order_usdc": amount}
            if isinstance(self.analyzer, AIAgentAnalyzer):
                tctx = ToolContext(market=m, signal=signal, risk=pre, sizing=sizing, limits=limits, mode=mode.value,
                                   decision_id=decision_id, wallet_policy=self.policy,
                                   router=self.venue_router if mode == TradingMode.LIVE else None,
                                   refresh_market=(lambda: self.refresh_market_fn(m)) if self.refresh_market_fn else None,
                                   wallet_state=self.wallet_state_fn)
                ai_out = await self.analyzer.analyze(m, signal, pre, sizing, ctx=tctx)
            else:
                ai_out = await self.analyzer.analyze(m, signal, pre, sizing)
            if ai_out.status != "OK" or ai_out.decision is None:
                return rec(
                    Action.WATCH,
                    f"AI_{ai_out.status}: no new AI-dependent entries; {strategy_note}",
                    ai=ai_out,
                )
            d = ai_out.decision
            if d.action != Action.BUY:
                mapped = Action.REJECT if d.action == Action.REJECT else Action.WATCH
                return rec(mapped, f"AI said {d.action.value}; {strategy_note}", ai=ai_out)
            if watch_band:
                # The AI is consulted for every WATCH-band token, but it may only UPGRADE one to BUY when the
                # strategy's soft gates (score / momentum / buyer-pressure) are what failed. Missing data, a dead
                # pool, no recent activity, a brand-new pool or extreme holder concentration are hard stops.
                hard_failed = [k for k in _HARD_GATES if signal.gates.get(k) is False]
                if hard_failed:
                    return rec(Action.WATCH,
                               f"AI said BUY but hard gate failed ({', '.join(hard_failed)}); {strategy_note}",
                               ai=ai_out)
            if d.confidence < self.min_ai_conf:
                return rec(
                    Action.WATCH,
                    f"AI confidence {d.confidence:.2f} below {self.min_ai_conf}; {strategy_note}",
                    ai=ai_out,
                )
            # The AI chooses a size INSIDE [min_order, max_entry]; it can shrink within that band but never enlarge past
            # the deterministic maximum, and a too-small answer is lifted to the venue minimum instead of cancelling
            # the trade (a 3% pick of a $5 portfolio is $0.16, which used to end as SIZE_BELOW_MINIMUM_ORDER).
            wanted = d.recommended_order_usdc or portfolio.total_value() * d.recommended_position_percent / 100.0
            size_cap = min(amount, max(self.min_order, wanted))
            if abs(size_cap - wanted) > 1e-9:
                size_note = f" (AI size {wanted:.2f} adjusted to {size_cap:.2f}, allowed {self.min_order:.2f}-{amount:.2f})"

        final_amount = round(min(amount, size_cap), 6)
        if final_amount < self.min_order:
            return rec(Action.WATCH, "SIZE_BELOW_MINIMUM_ORDER", ai=ai_out, amount=final_amount)
        # Policy validation + risk engine run AGAIN on the final order; state may have changed during AI latency.
        final = self._assess(m, final_amount, mode, portfolio, controls, now, limits)
        if final.decision == RiskDecision.REJECT:
            return rec(Action.REJECT, f"RISK_VETO_AFTER_AI: {final.summary()}", ai=ai_out, final_risk=final,
                       amount=final_amount)
        # WHERE to trade: LIVE re-quotes every venue at the FINAL size and only goes ahead on one that fits the user's limits.
        venue_note, chosen_venue = "", None
        if mode == TradingMode.LIVE and self.venue_router is not None:
            probe = self._request(m, final_amount, mode, now, decision_id, limits.max_slippage_pct, limits)
            results = await self.venue_router.quote_all(probe)
            pick = self.venue_router.best(results)
            ai_venue = ai_out.decision.venue if (ai_out and ai_out.decision) else None
            mine = next((r for r in results if r.venue == ai_venue), None) if ai_venue else None
            if mine is not None and mine.passes_policy and mine.executable:
                pick = mine
            if ai_out is not None:
                ai_out.venue_quotes = [r.agent_view() for r in results]
            if pick is None:
                why = "; ".join(f"{r.venue}: " + ("; ".join(r.policy_failures) or r.not_executable_reason or r.error or "no quote")
                                for r in results)[:700]
                return rec(Action.WATCH, f"NO_VENUE_WITHIN_POLICY: {why}", ai=ai_out, final_risk=final, amount=final_amount)
            chosen_venue = pick.venue
            venue_note = (f" via {pick.venue} (fee {pick.fee_pct if pick.fee_pct is not None else 'n/a'}%, impact "
                          f"{pick.price_impact_pct if pick.price_impact_pct is not None else 'n/a'}%, cost "
                          f"{pick.total_cost_pct if pick.total_cost_pct is not None else 'n/a'}%)")
            if ai_venue and ai_venue != chosen_venue:
                venue_note += f"; AI preferred {ai_venue} but it did not fit the limits"
        req = self._request(m, final_amount, mode, now, decision_id, limits.max_slippage_pct, limits, chosen_venue)
        approved = self.approver.approve(req, final)
        out = rec(Action.BUY, (("approved: watch-band upgraded by AI, risk approved" if watch_band else
                            "approved: qualified signal, AI agreement, risk approved") + size_note + venue_note) if ai_out
                  else "approved: qualified signal and risk approved (AI disabled, PAPER only)",
                  ai=ai_out, final_risk=final, amount=final_amount, approved_trade=approved)
        return out

    async def evaluate_manual_override(self, m: MarketState, portfolio: PortfolioState, controls: ControlState,
                                       mode: TradingMode, now: datetime | None = None,
                                       amount_override: float | None = None) -> DecisionRecord:
        """Explicit user 'BUY ANYWAY' override on a WATCH-listed opportunity.

        Skips the strategy-qualification and AI-agreement stages entirely --
        the user is overriding the agent's opinion on THIS token, not asking
        for a second opinion -- but still runs the SAME risk engine
        assessment as an ordinary entry, at the same amount an ordinary entry
        would use unless the caller overrides it. A risk veto (bad liquidity,
        an unverified/unsellable contract, exposure/position limits, etc.)
        still blocks the trade: "anyway" means "skip the strategy/AI opinion",
        never "skip the safety checks".
        """
        now = now or utcnow()
        limits = tighten_limits(self.limits, self.policy)
        signal = self.strategy.score(m, now)  # recorded for context/audit; not gated on here
        amount = amount_override if amount_override else max_entry_amount(m, portfolio, limits)
        assess = self._assess(m, amount, mode, portfolio, controls, now, limits)

        def rec(action: Action, reason: str, **kw) -> DecisionRecord:
            return DecisionRecord(created_at=now, mode=mode, token_key=m.key, strategy_id=self.strategy.strategy_id,
                                  strategy_version=self.strategy.version, strategy_config=self.strategy.config_snapshot(),
                                  risk_limits=limits.model_dump(mode="json"), controls=controls.snapshot(),
                                  wallet_policy=self.policy.model_dump(mode="json") if self.policy else None,
                                  market=m.model_dump(mode="json"), signal=signal, pre_risk=assess,
                                  ai_mode=self.ai_mode.value, final_action=action, final_reason=reason,
                                  sized_amount_usdc=kw.pop("amount", amount), **kw)

        if assess.decision == RiskDecision.REJECT:
            return rec(Action.REJECT, f"MANUAL_OVERRIDE_STILL_RISK_VETOED: {assess.summary()}")
        decision_id = uuid.uuid4().hex
        req = self._request(m, amount, mode, now, decision_id, limits.max_slippage_pct)
        approved = self.approver.approve(req, assess)
        out = rec(Action.BUY,
                  "MANUAL_OVERRIDE: user chose BUY ANYWAY on a WATCH opportunity; risk engine re-checked and approved",
                  final_risk=assess, amount=amount, approved_trade=approved)
        out.id = decision_id
        return out
