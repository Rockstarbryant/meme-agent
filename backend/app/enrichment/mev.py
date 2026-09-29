"""Deterministic MEV exposure heuristic.

There is no mempool-simulation or bundle-history API wired up for Arc, so a real "was this token sandwiched"
answer is not available. What IS available from ordinary market data is a reasonable PROXY for how attractive a
pool is to sandwich/front-run: thin liquidity relative to trade size, and a pool that is already swinging a lot on
small volume. This module turns that into a bounded 0..1 score and — critically — always labels it
``mev_method="heuristic_v1"`` so it is never confused with a real simulation. ``None`` is still returned when even
liquidity is unknown, because a wrong number is worse than an admitted gap.
"""
from __future__ import annotations

from app.domain.market import MarketState

MEV_METHOD = "heuristic_v1"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def estimate_mev_exposure(m: MarketState, *, trade_usdc: float = 25.0) -> tuple[float | None, str | None]:
    """Returns (score in [0,1], method). None when liquidity is unknown (nothing to base a score on)."""
    if m.liquidity is None or m.liquidity <= 0:
        return None, None

    # 1) Price impact of a representative trade against this pool: bigger impact = more room to sandwich.
    impact_pct = m.expected_price_impact_pct
    if impact_pct is None:
        impact_pct = 50.0 * (trade_usdc / m.liquidity)  # constant-product approximation, matches execution/math.py
    impact_score = _clamp(impact_pct / 5.0)  # 5%+ impact -> saturated

    # 2) Volatility unexplained by traded volume: a pool that moves a lot on thin volume is easier to move again.
    vol_ratio = (m.volume_5m / m.liquidity) if (m.volume_5m and m.liquidity) else None
    if m.price_change_5m is not None and vol_ratio is not None:
        churn_score = _clamp((abs(m.price_change_5m) / 100.0) / max(vol_ratio, 0.01) / 5.0)
    else:
        churn_score = 0.0

    # 3) Shallow absolute liquidity is exploitable regardless of trade size.
    depth_score = _clamp(1.0 - (m.liquidity / 50_000.0)) if m.liquidity < 50_000 else 0.0

    score = _clamp(0.5 * impact_score + 0.3 * depth_score + 0.2 * churn_score)
    return round(score, 3), MEV_METHOD
