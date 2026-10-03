"""Liquidity Trend: qualify established / high-liquidity Arc tokens.

Traction Momentum is tuned for fresh launches (unique buyers 5m, buy/sell pressure).
This strategy scores depth, stability, and longer-horizon activity so high-MC /
high-liquidity tokens from Gecko trending can reach BUY without launch-only gates.
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.domain.market import MarketState
from app.portfolio.exits import ExitConfig
from app.strategies.base import Strategy, StrategySignal


class LTWeights(BaseModel):
    liquidity_depth: float = 0.30
    market_quality: float = 0.20
    holder_quality: float = 0.20
    activity: float = 0.15
    momentum: float = 0.15

    @model_validator(mode="after")
    def _normalise(self) -> "LTWeights":
        vals = self.model_dump()
        if any(v < 0 for v in vals.values()):
            raise ValueError("weights must be non-negative")
        total = sum(vals.values())
        if total <= 0:
            raise ValueError("weights must sum to > 0")
        for k, v in vals.items():
            object.__setattr__(self, k, v / total)
        return self


class LiquidityTrendConfig(BaseModel):
    strategy_id: str = "liquidity_trend"
    version: int = 1
    weights: LTWeights = Field(default_factory=LTWeights)
    min_score: float = 25.0
    watch_score: float = 12.0
    min_liquidity_usdc: float = 15_000.0
    min_market_cap_usdc: float = 20_000.0
    max_top10_for_score: float = 70.0
    min_age_seconds: float = 60.0
    entry_window_seconds: int = 300
    exit: ExitConfig = Field(default_factory=ExitConfig)


def _lin(x: float | None, lo: float, hi: float) -> float | None:
    if x is None:
        return None
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * 100.0


def _wavg(pairs: list[tuple[float | None, float]]) -> float | None:
    got = [(v, w) for v, w in pairs if v is not None]
    if not got:
        return None
    return sum(v * w for v, w in got) / sum(w for _, w in got)


def _inv(v: float | None) -> float | None:
    return None if v is None else 100.0 - v


class LiquidityTrend(Strategy):
    def __init__(self, config: LiquidityTrendConfig | None = None):
        self.cfg = config or LiquidityTrendConfig()
        self.strategy_id = self.cfg.strategy_id
        self.version = self.cfg.version

    def config_snapshot(self) -> dict:
        return self.cfg.model_dump(mode="json")

    def _liquidity_depth(self, m: MarketState) -> float | None:
        c = self.cfg
        if m.liquidity is None:
            return None
        lo = math.log10(max(c.min_liquidity_usdc, 1))
        level = _lin(math.log10(max(m.liquidity, 1)), lo, lo + math.log10(30))
        mcap = _lin(math.log10(max(m.market_cap or 0, 1)), math.log10(max(c.min_market_cap_usdc, 1)),
                    math.log10(max(c.min_market_cap_usdc, 1)) + 2)
        ratio = _lin((m.liquidity / m.market_cap) if m.market_cap else None, 0.02, 0.25)
        impact = _inv(_lin(m.expected_price_impact_pct, 0.3, 4))
        return _wavg([(level, 0.4), (mcap, 0.25), (ratio, 0.2), (impact, 0.15)])

    def _market_quality(self, m: MarketState, now: datetime) -> float | None:
        age = m.age_seconds(now)
        age_s = _lin(age, 300, 86_400)  # prefer tokens that survived a day+
        vol = None
        if m.volume_5m is not None and m.liquidity:
            vol = _lin(m.volume_5m / max(m.liquidity, 1), 0.01, 0.5)
        # Prefer moderate volatility over pure chaos
        stab = _inv(_lin(m.volatility_pct, 5, 50)) if m.volatility_pct is not None else None
        return _wavg([(age_s, 0.35), (vol, 0.35), (stab, 0.3)])

    def _holder_quality(self, m: MarketState) -> float | None:
        c = self.cfg
        top = _inv(_lin(m.top10_holder_pct, 15, c.max_top10_for_score))
        growth = _lin(m.holder_growth_pct, -5, 15)
        count = _lin(m.holder_count, 50, 2000)
        return _wavg([(top, 0.5), (growth, 0.2), (count, 0.3)])

    def _activity(self, m: MarketState) -> float | None:
        buyers = m.unique_buyers_5m
        buys = m.buys_5m
        vol = m.volume_5m
        b_score = _lin(buyers, 0, 40)
        tx_score = _lin((buys or 0) + (m.sells_5m or 0), 0, 80)
        v_score = _lin(vol, 0, 50_000)
        return _wavg([(b_score, 0.4), (tx_score, 0.3), (v_score, 0.3)])

    def _momentum(self, m: MarketState) -> float | None:
        # Established tokens: reward mild positive move, penalize free-fall only.
        s5 = _lin(m.price_change_5m, -10, 20)
        s15 = _lin(m.price_change_15m, -15, 40)
        return _wavg([(s5, 0.5), (s15, 0.5)])

    def score(self, m: MarketState, now: datetime) -> StrategySignal:
        c = self.cfg
        comps = {
            "liquidity_depth": self._liquidity_depth(m),
            "market_quality": self._market_quality(m, now),
            "holder_quality": self._holder_quality(m),
            "activity": self._activity(m),
            "momentum": self._momentum(m),
        }
        weights = c.weights.model_dump()
        gaps = [k for k, v in comps.items() if v is None]
        total = sum(weights[k] * (v or 0.0) for k, v in comps.items())
        age = m.age_seconds(now)
        gates = {
            "price_present": m.price is not None and m.price > 0,
            "liquidity_present": m.liquidity is not None and m.liquidity >= c.min_liquidity_usdc,
            "min_score": total >= c.min_score,
            "min_age": age is None or age >= c.min_age_seconds,
            # Soft: top10 is primarily a risk veto; only hard-fail extreme concentration here
            "holder_not_extreme": m.top10_holder_pct is None or m.top10_holder_pct <= 95.0,
        }
        failed = [k for k, ok in gates.items() if not ok]
        reasons = [f"gate failed: {k}" for k in failed] or ["all liquidity-trend gates passed"]
        return StrategySignal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            score=round(total, 2),
            qualified=not failed,
            components={k: (None if v is None else round(v, 2)) for k, v in comps.items()},
            weights=weights,
            gates=gates,
            reasons=reasons,
            data_gaps=gaps,
            config_snapshot=self.config_snapshot(),
        )
