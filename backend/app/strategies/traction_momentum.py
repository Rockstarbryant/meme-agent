"""Traction Momentum v1: only tokens showing measurable, sustained traction qualify.

Weights/thresholds are INITIAL DEFAULTS, not claimed optimal. Every decision records the version and
full config snapshot that produced it.
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.domain.market import MarketState
from app.portfolio.exits import ExitConfig
from app.strategies.base import Strategy, StrategySignal


class StrategyWeights(BaseModel):
    momentum: float = 0.25
    buyer_growth: float = 0.20
    buy_sell_pressure: float = 0.15
    liquidity_quality: float = 0.15
    holder_distribution: float = 0.10
    creator_behavior: float = 0.10
    market_quality: float = 0.05

    @model_validator(mode="after")
    def _normalise(self) -> "StrategyWeights":
        vals = self.model_dump()
        if any(v < 0 for v in vals.values()):
            raise ValueError("weights must be non-negative")
        total = sum(vals.values())
        if total <= 0:
            raise ValueError("weights must sum to > 0")
        for k, v in vals.items():
            object.__setattr__(self, k, v / total)
        return self


class TractionMomentumConfig(BaseModel):
    strategy_id: str = "traction_momentum"
    version: int = 1
    weights: StrategyWeights = Field(default_factory=StrategyWeights)
    min_score: float = 70.0
    watch_score: float = 50.0
    min_unique_buyers_5m: int = 8
    full_score_unique_buyers_5m: int = 60
    min_buy_sell_volume_ratio: float = 1.2
    min_age_seconds: float = 300.0
    momentum_full_score_5m_pct: float = 30.0
    momentum_full_score_15m_pct: float = 60.0
    min_liquidity_for_scoring_usdc: float = 10_000.0
    unknown_creator_score: float = 40.0  # unknown creator is NOT treated as good
    entry_window_seconds: int = 300  # idempotency bucket for entries
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


class TractionMomentum(Strategy):
    def __init__(self, config: TractionMomentumConfig | None = None):
        self.cfg = config or TractionMomentumConfig()
        self.strategy_id = self.cfg.strategy_id
        self.version = self.cfg.version

    def config_snapshot(self) -> dict:
        return self.cfg.model_dump(mode="json")

    # -- components ------------------------------------------------------
    def _momentum(self, m: MarketState) -> float | None:
        c = self.cfg
        if m.price_change_5m is None:
            return None
        s5 = _lin(m.price_change_5m, 0, c.momentum_full_score_5m_pct)
        s15 = _lin(m.price_change_15m, 0, c.momentum_full_score_15m_pct)
        pace = (m.price_change_15m / 3.0) if m.price_change_15m is not None else 0.0
        accel = _lin(m.price_change_5m - pace, -5, 15)
        dd = _inv(_lin(m.drawdown_from_high_pct(), 0, 30))
        return _wavg([(s5, .4), (s15, .2), (accel, .2), (dd, .2)])

    def _buyer_growth(self, m: MarketState) -> float | None:
        c = self.cfg
        b5 = m.unique_buyers_5m
        if b5 is None:
            return None
        level = _lin(b5, c.min_unique_buyers_5m, c.full_score_unique_buyers_5m)
        growth = None
        if m.unique_buyers_prev_5m is not None:
            growth = _lin((b5 - m.unique_buyers_prev_5m) / max(m.unique_buyers_prev_5m, 1), 0, 2.0)
        accel = _lin(m.unique_buyers_1m * 5 / max(b5, 1), 0.8, 2.0) if m.unique_buyers_1m is not None else None
        return _wavg([(level, .4), (growth, .4), (accel, .2)])

    def _pressure(self, m: MarketState) -> float | None:
        ratio, net = m.buy_sell_volume_ratio(), m.net_pressure()
        return _wavg([(_lin(ratio, 1, 3), .5), (_lin(net, 0, .6), .5)])

    def _liquidity(self, m: MarketState) -> float | None:
        c = self.cfg
        if m.liquidity is None:
            return None
        lo = math.log10(max(c.min_liquidity_for_scoring_usdc, 1))
        level = _lin(math.log10(max(m.liquidity, 1)), lo, lo + math.log10(20))
        ratio = _lin(m.liquidity / m.market_cap, 0.02, 0.15) if m.market_cap else None
        impact = _inv(_lin(m.expected_price_impact_pct, 0.5, 5))
        chg = _lin(m.liquidity_change_5m_pct, -10, 20)
        return _wavg([(level, .35), (ratio, .25), (impact, .25), (chg, .15)])

    def _holders(self, m: MarketState) -> float | None:
        return _wavg([(_inv(_lin(m.top10_holder_pct, 20, 60)), .5), (_lin(m.holder_growth_pct, 0, 20), .25),
                      (_lin(m.holder_count, 20, 300), .25)])

    def _creator(self, m: MarketState) -> float | None:
        if not m.creator_known:
            return self.cfg.unknown_creator_score
        return _wavg([(_inv(_lin(m.creator_sold_pct, 0, 20)), .6), (_inv(_lin(m.creator_balance_pct, 5, 30)), .4)])

    def _market_quality(self, m: MarketState, now: datetime) -> float | None:
        txs = (m.buys_5m or 0) + (m.sells_5m or 0) if (m.buys_5m is not None or m.sells_5m is not None) else None
        vl = (m.volume_5m / m.liquidity) if (m.volume_5m is not None and m.liquidity) else None
        return _wavg([(_lin(txs, 10, 150), .3), (_inv(_lin(m.volatility_pct, 5, 40)), .25),
                      (_lin(m.age_seconds(now), 300, 3600), .2), (_lin(vl, .05, 1.0), .25)])

    # -- scoring ---------------------------------------------------------
    def score(self, m: MarketState, now: datetime) -> StrategySignal:
        c = self.cfg
        comps = {
            "momentum": self._momentum(m), "buyer_growth": self._buyer_growth(m),
            "buy_sell_pressure": self._pressure(m), "liquidity_quality": self._liquidity(m),
            "holder_distribution": self._holders(m), "creator_behavior": self._creator(m),
            "market_quality": self._market_quality(m, now),
        }
        weights = c.weights.model_dump()
        gaps = [k for k, v in comps.items() if v is None]
        total = sum(weights[k] * (v or 0.0) for k, v in comps.items())  # missing data scores 0: conservative
        age = m.age_seconds(now)
        ratio = m.buy_sell_volume_ratio()
        gates = {
            "required_data_present": not ({"momentum", "buyer_growth", "buy_sell_pressure", "liquidity_quality"} & set(gaps)),
            "min_score": total >= c.min_score,
            "min_unique_buyers_5m": (m.unique_buyers_5m or 0) >= c.min_unique_buyers_5m,
            "buy_sell_ratio": ratio is not None and ratio >= c.min_buy_sell_volume_ratio,
            "min_age": age is not None and age >= c.min_age_seconds,
            "positive_5m_momentum": (m.price_change_5m or 0) > 0,
        }
        failed = [k for k, ok in gates.items() if not ok]
        reasons = [f"gate failed: {k}" for k in failed] or ["all traction gates passed"]
        return StrategySignal(strategy_id=self.strategy_id, strategy_version=self.version,
                              score=round(total, 2), qualified=not failed,
                              components={k: (None if v is None else round(v, 2)) for k, v in comps.items()},
                              weights=weights, gates=gates, reasons=reasons, data_gaps=gaps,
                              config_snapshot=self.config_snapshot())


class StrategyVersionStore:
    """In-memory registry of strategy configs; the DB-backed store implements the same shape."""

    def __init__(self) -> None:
        self._v: dict[tuple[str, int], dict] = {}

    def register(self, cfg: TractionMomentumConfig) -> None:
        key = (cfg.strategy_id, cfg.version)
        snap = cfg.model_dump(mode="json")
        if key in self._v and self._v[key] != snap:
            raise ValueError("strategy versions are immutable; bump the version")
        self._v[key] = snap

    def get(self, strategy_id: str, version: int) -> dict:
        return self._v[(strategy_id, version)]
