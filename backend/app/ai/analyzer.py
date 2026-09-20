from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.ai.provider import AIProviderError, LLMProvider
from app.ai.schemas import AIDecision, AIOutcome
from app.domain.market import MarketState
from app.risk.engine import RiskAssessment
from app.strategies.base import StrategySignal

PROMPT_VERSION = "traction-v1"

SYSTEM_PROMPT = """You are a cautious crypto market analyst inside a trading system.
You only receive numeric market features. You cannot trade, choose contracts, set calldata, or change limits.
A deterministic risk engine has the final say and will veto anything unsafe.
Respond with ONE JSON object and nothing else, with exactly these keys:
action (BUY|WATCH|REJECT|HOLD|SELL), confidence (0-1), reasoning_summary, positive_signals[], negative_signals[],
risk_flags[], strategy_score (0-100), recommended_position_percent (0-100, share of portfolio value).
Treat missing data as a risk, not as neutral. Prefer WATCH or REJECT when unsure."""

_FEATURES = ["price", "market_cap", "liquidity", "liquidity_change_5m_pct", "volume_1m", "volume_5m", "volume_15m",
             "buy_volume_5m", "sell_volume_5m", "unique_buyers_1m", "unique_buyers_5m", "unique_buyers_prev_5m",
             "unique_sellers_5m", "holder_count", "holder_growth_pct", "top5_holder_pct", "top10_holder_pct",
             "creator_known", "creator_balance_pct", "creator_sold_pct", "price_change_1m", "price_change_5m",
             "price_change_15m", "volatility_pct", "expected_price_impact_pct", "mev_risk_score"]


def build_user_prompt(m: MarketState, s: StrategySignal, r: RiskAssessment) -> str:
    """Numeric features only. Token names/symbols/descriptions are attacker-controlled and are never included."""
    digest = {
        "features": {k: getattr(m, k) for k in _FEATURES},
        "contract": m.contract.model_dump(),
        "strategy": {"id": s.strategy_id, "version": s.strategy_version, "score": s.score,
                     "components": s.components, "data_gaps": s.data_gaps},
        "risk": {"score": r.risk_score, "flags": [{"rule": f.rule, "severity": f.severity.value} for f in r.flags]},
    }
    return json.dumps(digest, default=str)


def parse_decision(text: str) -> AIDecision:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    return AIDecision.model_validate(json.loads(t))


class AIAnalyzer:
    def __init__(self, provider: LLMProvider | None):
        self.provider = provider

    async def analyze(self, m: MarketState, s: StrategySignal, r: RiskAssessment) -> AIOutcome:
        """Never raises: outages/garbage become UNAVAILABLE/INVALID and simply block new entries."""
        if self.provider is None:
            return AIOutcome(status="DISABLED", prompt_version=PROMPT_VERSION)
        base = dict(provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION)
        try:
            raw = await self.provider.complete(SYSTEM_PROMPT, build_user_prompt(m, s, r))
        except AIProviderError as exc:
            return AIOutcome(status="UNAVAILABLE", error=str(exc), **base)
        except Exception as exc:  # noqa: BLE001
            return AIOutcome(status="UNAVAILABLE", error=type(exc).__name__, **base)
        try:
            return AIOutcome(status="OK", decision=parse_decision(raw), raw=raw[:4000], **base)
        except (ValidationError, ValueError) as exc:
            return AIOutcome(status="INVALID", raw=raw[:4000], error=type(exc).__name__, **base)
