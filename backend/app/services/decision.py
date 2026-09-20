"""Stage 1 (deterministic) -> Stage 2 (AI) -> policy -> risk -> approved trade. Risk always wins."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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


class DecisionPipeline:
    def __init__(self, strategy: Strategy, risk: RiskEngine, limits: RiskLimits, approver: TradeApprover,
                 analyzer: AIAnalyzer, ai_mode: AIMode = AIMode.ENABLED, min_ai_confidence: float = 0.6,
                 min_order_usdc: float = 1.0, entry_window_seconds: int = 300,
                 wallet_policy: WalletPolicy | None = None):
        self.strategy, self.risk, self.limits, self.approver = strategy, risk, limits, approver
        self.analyzer, self.ai_mode, self.min_ai_conf = analyzer, ai_mode, min_ai_confidence
        self.min_order, self.window, self.policy = min_order_usdc, entry_window_seconds, wallet_policy

    def _request(self, m: MarketState, amount: float, mode: TradingMode, now: datetime, decision_id: str,
                 slippage: float) -> TradeRequest:
        bucket = int(now.timestamp() // self.window)
        raw = f"{mode.value}|{m.key}|{self.strategy.strategy_id}|{self.strategy.version}|{bucket}"
        return TradeRequest(idempotency_key=hashlib.sha256(raw.encode()).hexdigest()[:32], mode=mode, chain=m.chain,
                            token_address=m.token_address, side=Side.BUY, amount_usdc=amount, max_slippage_pct=slippage,
                            reference_price=m.price, launchpad=m.launchpad, pool_address=m.pool_address,
                            decision_id=decision_id, strategy_id=self.strategy.strategy_id,
                            strategy_version=self.strategy.version, reason="traction_momentum_entry")

    def _assess(self, m, amount, mode, portfolio, controls, now, limits) -> RiskAssessment:
        intent = TradeIntent(Side.BUY, amount, limits.max_slippage_pct, mode, self.strategy.strategy_id)
        return self.risk.assess(RiskContext(m, intent, portfolio, limits, controls, now))

    async def evaluate(self, m: MarketState, portfolio: PortfolioState, controls: ControlState,
                       mode: TradingMode, now: datetime | None = None) -> DecisionRecord:
        now = now or utcnow()
        limits = tighten_limits(self.limits, self.policy)
        signal = self.strategy.score(m, now)                                   # feature/strategy stage
        amount = max_entry_amount(m, portfolio, limits)
        pre = self._assess(m, amount, mode, portfolio, controls, now, limits)  # risk filter stage

        def rec(action: Action, reason: str, **kw) -> DecisionRecord:
            return DecisionRecord(created_at=now, mode=mode, token_key=m.key, strategy_id=self.strategy.strategy_id,
                                  strategy_version=self.strategy.version, strategy_config=self.strategy.config_snapshot(),
                                  risk_limits=limits.model_dump(mode="json"), controls=controls.snapshot(),
                                  wallet_policy=self.policy.model_dump(mode="json") if self.policy else None,
                                  market=m.model_dump(mode="json"), signal=signal, pre_risk=pre,
                                  ai_mode=self.ai_mode.value, final_action=action, final_reason=reason,
                                  sized_amount_usdc=kw.pop("amount", amount), **kw)

        if pre.decision == RiskDecision.REJECT:
            return rec(Action.REJECT, f"RISK_VETO: {pre.summary()}")
        if not signal.qualified:
            watch = signal.score >= getattr(self.strategy, "cfg").watch_score
            return rec(Action.WATCH if watch else Action.REJECT, "; ".join(signal.reasons))

        ai_out: AIOutcome | None = None
        size_cap = amount
        if self.ai_mode == AIMode.DISABLED:
            if mode == TradingMode.LIVE:
                return rec(Action.WATCH, "AI_REQUIRED_FOR_LIVE_ENTRIES")
        else:
            ai_out = await self.analyzer.analyze(m, signal, pre)               # AI stage (qualified only)
            if ai_out.status != "OK" or ai_out.decision is None:
                return rec(Action.WATCH, f"AI_{ai_out.status}: no new AI-dependent entries", ai=ai_out)
            d = ai_out.decision
            if d.action != Action.BUY:
                mapped = Action.REJECT if d.action == Action.REJECT else Action.WATCH
                return rec(mapped, f"AI said {d.action.value}", ai=ai_out)
            if d.confidence < self.min_ai_conf:
                return rec(Action.WATCH, f"AI confidence {d.confidence:.2f} below {self.min_ai_conf}", ai=ai_out)
            # AI may only SHRINK the position, never enlarge it beyond deterministic sizing.
            size_cap = min(amount, portfolio.total_value() * d.recommended_position_percent / 100.0)

        final_amount = round(min(amount, size_cap), 6)
        if final_amount < self.min_order:
            return rec(Action.WATCH, "SIZE_BELOW_MINIMUM_ORDER", ai=ai_out, amount=final_amount)
        # Policy validation + risk engine run AGAIN on the final order; state may have changed during AI latency.
        final = self._assess(m, final_amount, mode, portfolio, controls, now, limits)
        if final.decision == RiskDecision.REJECT:
            return rec(Action.REJECT, f"RISK_VETO_AFTER_AI: {final.summary()}", ai=ai_out, final_risk=final,
                       amount=final_amount)
        decision_id = uuid.uuid4().hex
        req = self._request(m, final_amount, mode, now, decision_id, limits.max_slippage_pct)
        approved = self.approver.approve(req, final)
        out = rec(Action.BUY, "approved: qualified signal, AI agreement, risk approved" if ai_out
                  else "approved: qualified signal and risk approved (AI disabled, PAPER only)",
                  ai=ai_out, final_risk=final, amount=final_amount, approved_trade=approved)
        out.id = decision_id
        return out
