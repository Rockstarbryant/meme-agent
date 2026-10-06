"""Additional strategies for established (not freshly launched) Arc tokens.

  liquidity_growth  buys tokens whose liquidity is genuinely growing (money flowing into the pool) while price is
                    confirming but not yet parabolic.
  trend_pullback    buys a dip inside an established uptrend.
  volume_breakout   buys a volume surge with buyers in control.

All three share the structure of LiquidityTrend: weighted components in 0-100, hard gates, ``min_score`` /
``watch_score`` thresholds and their own exit profile. Unknown data is never treated as a pass for the signal the
strategy is built around (a breakout without volume data is not a breakout), but it does not fail unrelated gates.
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.domain.market import MarketState
from app.portfolio.exits import ExitConfig
from app.strategies.base import Strategy, StrategySignal
from app.strategies.liquidity_trend import _inv, _lin, _wavg, _wchg, _wvol


class _Weights(BaseModel):
    """Component weights: non-negative, normalised to sum to 1 (same behaviour as the other strategies)."""

    @model_validator(mode="after")
    def _normalise(self):
        vals = self.model_dump()
        if any(v < 0 for v in vals.values()):
            raise ValueError("weights must be non-negative")
        total = sum(vals.values())
        if total <= 0:
            raise ValueError("weights must sum to > 0")
        for k, v in vals.items():
            object.__setattr__(self, k, v / total)
        return self


def _depth(m: MarketState, min_liq: float) -> float | None:
    if m.liquidity is None:
        return None
    lo = math.log10(max(min_liq, 1))
    level = _lin(math.log10(max(m.liquidity, 1)), lo, lo + math.log10(30))
    ratio = _lin((m.liquidity / m.market_cap) if m.market_cap else None, 0.02, 0.25)
    return _wavg([(level, 0.6), (ratio, 0.4)])


def _holders(m: MarketState, max_top10: float = 70.0) -> float | None:
    top = _inv(_lin(m.top10_holder_pct, 15, max_top10))
    g = m.holder_growth.get("24h", m.holder_growth.get("6h", m.holder_growth.get("1h", m.holder_growth_pct)))
    return _wavg([(top, 0.5), (_lin(g, -5, 15), 0.2), (_lin(m.holder_count, 50, 2000), 0.3)])


def _flow(m: MarketState, w: str) -> float | None:
    """Buy pressure in a window: share of USD volume (or trade count) that was buying, scaled 40%..70% -> 0..100."""
    ws = m.windows.get(w)
    if ws is None:
        return None
    if ws.buy_volume_usd is not None and ws.sell_volume_usd is not None and (ws.buy_volume_usd + ws.sell_volume_usd) > 0:
        share = ws.buy_volume_usd / (ws.buy_volume_usd + ws.sell_volume_usd)
    elif ws.buys is not None and ws.sells is not None and (ws.buys + ws.sells) > 0:
        share = ws.buys / (ws.buys + ws.sells)
    else:
        return None
    return _lin(share, 0.40, 0.70)


def _recent_activity(m: MarketState) -> tuple[bool, bool]:
    """(known, has_activity) over the last hour, falling back to the 5m fields."""
    w1 = m.windows.get("1h")
    if w1 is not None and (w1.buys is not None or w1.sells is not None or w1.volume_usd is not None):
        return True, any((v or 0) > 0 for v in (w1.buys, w1.sells, w1.volume_usd))
    fields = (m.volume_5m, m.buys_5m, m.sells_5m, m.unique_buyers_5m)
    return any(v is not None for v in fields), any((v or 0) > 0 for v in fields)


def _signal(strategy: "_Base", m: MarketState, now: datetime, comps: dict[str, float | None], extra_gates: dict[str, bool],
            reasons_ok: str) -> StrategySignal:
    c = strategy.cfg
    weights = c.weights.model_dump()
    total = sum(weights[k] * (v or 0.0) for k, v in comps.items())
    known, has = _recent_activity(m)
    age = m.age_seconds(now)
    gates = {
        "price_present": m.price is not None and m.price > 0,
        "liquidity_present": m.liquidity is not None and m.liquidity >= c.min_liquidity_usdc,
        "min_score": total >= c.min_score,
        "min_age": age is None or age >= c.min_age_seconds,
        "recent_activity": (not known) or has,
        "two_way_trading": m.has_two_way_trading() is not False,
        "holder_not_extreme": m.top10_holder_pct is None or m.top10_holder_pct <= 95.0,
        **extra_gates,
    }
    failed = [k for k, ok in gates.items() if not ok]
    return StrategySignal(
        strategy_id=strategy.strategy_id, strategy_version=strategy.version, score=round(total, 2), qualified=not failed,
        components={k: (None if v is None else round(v, 2)) for k, v in comps.items()}, weights=weights, gates=gates,
        reasons=[f"gate failed: {k}" for k in failed] or [reasons_ok], data_gaps=[k for k, v in comps.items() if v is None],
        config_snapshot=strategy.config_snapshot())


class _Base(Strategy):
    def __init__(self, cfg):
        self.cfg = cfg
        self.strategy_id, self.version = cfg.strategy_id, cfg.version

    def config_snapshot(self) -> dict:
        return self.cfg.model_dump(mode="json")


# =============================================================================================== liquidity_growth
class LGWeights(_Weights):
    liquidity_growth: float = 0.30
    price_confirmation: float = 0.20
    activity: float = 0.20
    holder_quality: float = 0.15
    depth: float = 0.15


class LiquidityGrowthConfig(BaseModel):
    strategy_id: str = "liquidity_growth"
    version: int = 1
    weights: LGWeights = Field(default_factory=LGWeights)
    min_score: float = 35.0
    watch_score: float = 18.0
    min_liquidity_usdc: float = 15_000.0
    min_age_seconds: float = 3600.0
    min_liquidity_growth_pct: float = 5.0        # liquidity must be up at least this much over the 1h or 6h window
    max_price_change_1h_pct: float = 40.0        # do not chase a vertical candle
    max_price_change_24h_pct: float = 150.0
    exit: ExitConfig = Field(default_factory=ExitConfig.swing)


class LiquidityGrowth(_Base):
    def score(self, m: MarketState, now: datetime) -> StrategySignal:
        c = self.cfg
        lg = {w: m.liquidity_growth.get(w) for w in ("1h", "6h", "24h")}
        growth_comp = _wavg([(_lin(lg["1h"], 0, 25), 0.4), (_lin(lg["6h"], 0, 60), 0.4), (_lin(lg["24h"], 0, 150), 0.2)])
        mcg = _lin(m.market_cap_growth.get("6h", m.market_cap_growth.get("1h")), -5, 40)
        growth_comp = _wavg([(growth_comp, 0.8), (mcg, 0.2)]) if growth_comp is not None else None
        # price should confirm (rising) without being parabolic: peak score around +5..+20% over 6h
        pc6, pc1 = _wchg(m, "6h"), _wchg(m, "1h")
        confirm = _wavg([(_lin(pc6, -5, 15) if pc6 is None or pc6 <= 25 else _inv(_lin(pc6, 25, 80)), 0.6),
                         (_lin(pc1, -3, 8) if pc1 is None or pc1 <= 15 else _inv(_lin(pc1, 15, 40)), 0.4)])
        activity = _wavg([(_lin(m.buyers_in("1h"), 0, 40), 0.3), (_lin(m.sellers_in("1h"), 0, 30), 0.15),
                          (_lin(_wvol(m, "1h"), 0, 100_000), 0.25), (_lin(_wvol(m, "24h"), 0, 1_000_000), 0.15),
                          (_flow(m, "1h"), 0.15)])
        comps = {"liquidity_growth": growth_comp, "price_confirmation": confirm, "activity": activity,
                 "holder_quality": _holders(m), "depth": _depth(m, c.min_liquidity_usdc)}
        best = max([g for g in (lg["1h"], lg["6h"]) if g is not None], default=None)
        pc24 = _wchg(m, "24h")
        gates = {
            # The signal itself: UNKNOWN history fails (a growth strategy cannot buy "growth" it cannot see yet).
            "liquidity_growing": best is not None and best >= c.min_liquidity_growth_pct,
            "not_overextended": (pc1 is None or pc1 <= c.max_price_change_1h_pct) and (pc24 is None or pc24 <= c.max_price_change_24h_pct),
        }
        return _signal(self, m, now, comps, gates, "all liquidity-growth gates passed")


# =============================================================================================== trend_pullback
class TPWeights(_Weights):
    trend_strength: float = 0.30
    pullback_quality: float = 0.25
    flow: float = 0.20
    depth: float = 0.15
    holder_quality: float = 0.10


class TrendPullbackConfig(BaseModel):
    strategy_id: str = "trend_pullback"
    version: int = 1
    weights: TPWeights = Field(default_factory=TPWeights)
    min_score: float = 40.0
    watch_score: float = 20.0
    min_liquidity_usdc: float = 20_000.0
    min_age_seconds: float = 6 * 3600.0
    min_trend_6h_pct: float = 3.0                # uptrend: price up at least this over 6h ...
    min_trend_24h_pct: float = 0.0               # ... and not down over 24h
    pullback_max_1h_pct: float = 2.0             # the dip: 1h change at or below this ...
    pullback_floor_1h_pct: float = -12.0         # ... but not a collapse
    exit: ExitConfig = Field(default_factory=ExitConfig.pullback)


class TrendPullback(_Base):
    def score(self, m: MarketState, now: datetime) -> StrategySignal:
        c = self.cfg
        pc1, pc6, pc24, pc15 = _wchg(m, "1h"), _wchg(m, "6h"), _wchg(m, "24h"), _wchg(m, "15m")
        trend = _wavg([(_lin(pc6, 0, 20), 0.55), (_lin(pc24, 0, 40), 0.45)])
        # best pullbacks are modest (-3..-8%) and not still accelerating down in the last 15 minutes
        pull = None
        if pc1 is not None:
            depth_score = 100.0 - min(100.0, abs((pc1 - (-5.0)) / 7.0) * 100.0)      # peak at -5%
            stabilising = _lin(pc15, -3, 1) if pc15 is not None else None
            pull = _wavg([(max(0.0, depth_score), 0.7), (stabilising, 0.3)])
        comps = {"trend_strength": trend, "pullback_quality": pull, "flow": _wavg([(_flow(m, "1h"), 0.5), (_flow(m, "4h"), 0.5)]),
                 "depth": _depth(m, c.min_liquidity_usdc), "holder_quality": _holders(m)}
        gates = {
            "uptrend": pc6 is not None and pc6 >= c.min_trend_6h_pct and (pc24 is None or pc24 >= c.min_trend_24h_pct),
            "in_pullback": pc1 is not None and c.pullback_floor_1h_pct <= pc1 <= c.pullback_max_1h_pct,
        }
        return _signal(self, m, now, comps, gates, "all trend-pullback gates passed")


# =============================================================================================== volume_breakout
class VBWeights(_Weights):
    volume_surge: float = 0.35
    buy_pressure: float = 0.25
    price_follow_through: float = 0.20
    depth: float = 0.10
    holder_quality: float = 0.10


class VolumeBreakoutConfig(BaseModel):
    strategy_id: str = "volume_breakout"
    version: int = 1
    weights: VBWeights = Field(default_factory=VBWeights)
    min_score: float = 40.0
    watch_score: float = 20.0
    min_liquidity_usdc: float = 15_000.0
    min_age_seconds: float = 3600.0
    min_volume_surge: float = 2.0                # 1h volume vs the average hour of the last 24h
    min_buy_share_1h: float = 0.52               # buys / (buys + sells) over the last hour
    max_price_change_1h_pct: float = 35.0        # skip an already exhausted spike
    exit: ExitConfig = Field(default_factory=ExitConfig.breakout)


class VolumeBreakout(_Base):
    @staticmethod
    def surge_ratio(m: MarketState) -> float | None:
        v1, v24 = _wvol(m, "1h"), _wvol(m, "24h")
        if v1 is None or v24 is None or v24 <= 0:
            return None
        return v1 / (v24 / 24.0)

    def score(self, m: MarketState, now: datetime) -> StrategySignal:
        c = self.cfg
        ratio = self.surge_ratio(m)
        pc1, pc15 = _wchg(m, "1h"), _wchg(m, "15m")
        buy_flow = _flow(m, "1h")
        w1 = m.windows.get("1h")
        share = None
        if w1 is not None and w1.buys is not None and w1.sells is not None and (w1.buys + w1.sells) > 0:
            share = w1.buys / (w1.buys + w1.sells)
        comps = {"volume_surge": _lin(ratio, 1.0, 6.0), "buy_pressure": _wavg([(buy_flow, 0.6), (_lin(m.buyers_in("1h"), 0, 40), 0.4)]),
                 "price_follow_through": _wavg([(_lin(pc1, 0, 15) if pc1 is None or pc1 <= 25 else _inv(_lin(pc1, 25, 60)), 0.6),
                                                (_lin(pc15, -2, 6), 0.4)]),
                 "depth": _depth(m, c.min_liquidity_usdc), "holder_quality": _holders(m)}
        gates = {
            "volume_surge": ratio is not None and ratio >= c.min_volume_surge,      # unknown volume history = no breakout
            "buy_pressure": share is None or share >= c.min_buy_share_1h,
            "not_exhausted": pc1 is None or pc1 <= c.max_price_change_1h_pct,
        }
        return _signal(self, m, now, comps, gates, "all volume-breakout gates passed")
