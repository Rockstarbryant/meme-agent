"""AI exit reviewer: a second LLM role that looks at an OPEN position.

Safety contract (enforced by the engine, not by the prompt):
  * rule-based exits (hard stop, trailing, liquidity, momentum, take-profit tiers) always run first and win;
  * the model may only HOLD or SELL (partial / all) - it can never buy, enlarge, or hold through a rule exit;
  * output is a strict schema; garbage / outage / low confidence means HOLD (do nothing);
  * it sees numbers only (no token names), exactly like the entry analyst.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.ai.analyzer import _as_float, _as_list, _extract_object, _r, _window_view
from app.ai.provider import AIProviderError, LLMProvider
from app.domain.market import HOLDER_GROWTH_WINDOWS, MarketState
from app.portfolio.exits import ExitConfig
from app.portfolio.state import Position

EXIT_PROMPT_VERSION = "exit-v1"
_ACTIONS = {"HOLD", "SELL_PARTIAL", "SELL_ALL"}
_KEYS = {"action", "confidence", "reasoning_summary", "sell_fraction", "positive_signals", "negative_signals"}
Short = Annotated[str, StringConstraints(max_length=300)]

EXIT_SYSTEM_PROMPT = """You manage ONE open position in an automated crypto trading pipeline on the Arc network.
You receive ONLY numbers: the position's state, how far it is from the deterministic exit rules, and current market activity per time window.
Deterministic protections (hard stop, trailing stop, liquidity-drop exit, momentum exit, take-profit tiers) are ALREADY active and run before you. They have NOT triggered. You cannot buy, cannot add to a position, cannot move or disable a stop, and cannot choose contracts or addresses. You may only HOLD, or sell EARLIER than the rules would.

Reply with ONE JSON object and nothing else (no markdown, no commentary) with exactly these keys:
action (HOLD|SELL_PARTIAL|SELL_ALL), confidence (0-1), reasoning_summary, sell_fraction (0-1, share of the REMAINING position; 0 for HOLD, 1 for SELL_ALL), positive_signals[], negative_signals[].

How to decide
- Selling is irreversible and the rules already protect the downside. The default is HOLD.
- SELL_ALL only for a SPECIFIC, named deterioration you can point to in the numbers that the rules have not caught yet: for example buyers collapsing while sellers rise across several windows, sustained net sell volume, price falling on 1h, 4h and 6h together, liquidity draining over 1h or 6h, holder concentration spiking, or a gain that is clearly fading with momentum gone.
- SELL_PARTIAL (typically 0.25 to 0.5) to bank profit or cut risk when the evidence is mixed but turning negative, especially when the position is in profit.
- Do NOT sell merely because the position is flat, slightly negative, or quiet for a few minutes. Do NOT sell just to lock a tiny gain while the trend is intact.
- Fields that are null, and windows that are absent, are UNKNOWN. Treat them as neutral, not as negative.
- Judge by the strategy in use: traction_momentum positions are fast launches (5m / 15m activity matters); liquidity_trend positions are established tokens that move over hours (do not react to quiet 5m windows).
- confidence is your honest estimate that the action is right. SELL actions need at least 0.7 to be acted on; below that nothing happens, so say HOLD when unsure."""


class AIExitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["HOLD", "SELL_PARTIAL", "SELL_ALL"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = Field(max_length=1500)
    sell_fraction: float = Field(ge=0.0, le=1.0)
    positive_signals: list[Short] = Field(default_factory=list, max_length=10)
    negative_signals: list[Short] = Field(default_factory=list, max_length=10)


class AIExitOutcome(BaseModel):
    status: str  # OK | UNAVAILABLE | INVALID | DISABLED
    decision: AIExitDecision | None = None
    provider: str = ""
    model: str = ""
    prompt_version: str = EXIT_PROMPT_VERSION
    raw: str = ""
    error: str = ""


def build_exit_prompt(pos: Position, m: MarketState, cfg: ExitConfig, now: datetime) -> str:
    held_min = max(0.0, (now - pos.opened_at).total_seconds() / 60.0)
    since_high_min = max(0.0, (now - pos.last_new_high_at).total_seconds() / 60.0)
    armed = bool(pos.tiers_hit) or pos.peak_gain_pct >= cfg.trailing_activation_gain_pct
    next_tier = next((t for i, t in enumerate(cfg.tiers) if i not in pos.tiers_hit), None)
    digest = {
        "position": {
            "strategy": pos.strategy_id, "gain_pct": _r(pos.gain_pct), "peak_gain_pct": _r(pos.peak_gain_pct),
            "drawdown_from_peak_pct": _r(pos.drawdown_from_peak_pct), "held_minutes": _r(held_min),
            "minutes_since_new_high": _r(since_high_min), "tiers_hit": len(pos.tiers_hit),
            "remaining_fraction_of_initial": _r(pos.quantity / pos.initial_quantity) if pos.initial_quantity else None,
            "unrealized_pnl_usdc": _r(pos.unrealized_pnl), "realized_pnl_usdc": _r(pos.realized_pnl_usdc),
        },
        "rules_not_triggered": {
            "hard_stop_pct": cfg.hard_stop_pct, "pct_above_hard_stop": _r(pos.gain_pct + cfg.hard_stop_pct),
            "trailing_stop_pct": cfg.trailing_stop_pct, "trailing_armed": armed,
            "trailing_headroom_pct": _r(cfg.trailing_stop_pct - pos.drawdown_from_peak_pct) if armed else None,
            "next_take_profit_gain_pct": next_tier.gain_pct if next_tier else None,
            "stagnation_after_minutes": _r(cfg.stagnation_seconds / 60.0),
        },
        "market": {"price": _r(m.price), "market_cap": _r(m.market_cap), "liquidity": _r(m.liquidity),
                   "liquidity_change_5m_pct": _r(m.liquidity_change_5m_pct), "volatility_pct": _r(m.volatility_pct),
                   "expected_price_impact_pct": _r(m.expected_price_impact_pct)},
        "windows": _window_view(m),
        "growth": {"liquidity_pct": {w: _r(v) for w, v in m.liquidity_growth.items()},
                   "market_cap_pct": {w: _r(v) for w, v in m.market_cap_growth.items()}},
        "trading": {"two_way_trading": m.has_two_way_trading(), "one_sided_trading": m.one_sided_trading(),
                    "buy_sell_volume_ratio": _r(m.buy_sell_volume_ratio())},
        "holders": {"holder_count": m.holder_count,
                    "holder_growth_pct": {w: _r(m.holder_growth.get(w)) for w in HOLDER_GROWTH_WINDOWS if w in m.holder_growth},
                    "top10_holders_pct": _r(m.top10_holder_pct)},
        "creator": {"known": m.creator_known, "sold_pct": _r(m.creator_sold_pct)},
    }
    return json.dumps(digest, default=str, separators=(",", ":"))


def _normalise(raw: dict) -> dict:
    d = {k: v for k, v in raw.items() if k in _KEYS}   # unknown keys dropped, never forwarded
    a = d.get("action")
    if isinstance(a, str):
        a = a.strip().upper().replace(" ", "_").replace("-", "_")
        if a == "SELL":
            a = "SELL_ALL"
        d["action"] = a if a in _ACTIONS else a
    conf = _as_float(d.get("confidence"))
    if conf is not None and 1.0 < conf <= 100.0:
        conf /= 100.0
    if conf is not None:
        d["confidence"] = conf
    frac = _as_float(d.get("sell_fraction"), 0.0)
    if frac is not None and 1.0 < frac <= 100.0:
        frac /= 100.0
    d["sell_fraction"] = frac if frac is not None else 0.0
    if d.get("action") == "SELL_ALL":
        d["sell_fraction"] = 1.0
    elif d.get("action") == "HOLD":
        d["sell_fraction"] = 0.0
    d["reasoning_summary"] = str(d.get("reasoning_summary") or "")[:1500]
    for k in ("positive_signals", "negative_signals"):
        d[k] = _as_list(d.get(k))
    return d


def parse_exit_decision(text: str) -> AIExitDecision:
    return AIExitDecision.model_validate(_normalise(_extract_object(text)))


class AIExitAnalyzer:
    def __init__(self, provider: LLMProvider | None):
        self.provider = provider

    async def analyze(self, pos: Position, m: MarketState, cfg: ExitConfig, now: datetime) -> AIExitOutcome:
        """Never raises: outage / garbage becomes UNAVAILABLE / INVALID, which the engine treats as HOLD."""
        if self.provider is None:
            return AIExitOutcome(status="DISABLED")
        base = dict(provider=self.provider.name, model=self.provider.model)
        try:
            done = await self.provider.complete_ex(EXIT_SYSTEM_PROMPT, build_exit_prompt(pos, m, cfg, now),
                                                   validate=parse_exit_decision)
        except AIProviderError as exc:
            return AIExitOutcome(status="UNAVAILABLE", error=str(exc)[:300], **base)
        except Exception as exc:  # noqa: BLE001
            return AIExitOutcome(status="UNAVAILABLE", error=type(exc).__name__, **base)
        base = {**base, "provider": done.provider, "model": done.model}
        try:
            return AIExitOutcome(status="OK", decision=parse_exit_decision(done.text), raw=done.text[:4000], **base)
        except ValidationError as exc:
            fields = ",".join(sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")}))
            return AIExitOutcome(status="INVALID", raw=done.text[:4000], error=f"invalid fields: {fields}"[:300], **base)
        except ValueError as exc:
            return AIExitOutcome(status="INVALID", raw=done.text[:4000], error=f"{type(exc).__name__}: {exc}"[:300], **base)
