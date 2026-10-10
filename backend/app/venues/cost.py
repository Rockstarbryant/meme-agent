"""Execution-cost maths for a quote: pool fees vs price impact vs "deviation from market".

Why this exists: the live pre-flight compared the quote's execution price with the market (mid) price and rejected anything
over 3%. But a quote price INCLUDES the pool fee, and Arc meme pools charge 1-5% per hop (a two-hop route through 5% and 2%
pools costs about 7% before any price impact). Those quotes are honest, yet were reported as "13% deviation from market".
Splitting the cost into its parts lets the policy say what is actually wrong (the fee, the impact, or a real mismatch).
"""
from __future__ import annotations

import re
from typing import Any

from app.core.types import Side
from app.domain.trade import Quote

_LEG = re.compile(r"(?P<type>[a-z0-9\-]+):(?P<fee>[0-9]+|\?)", re.I)


def route_fee_pct(route: str | None) -> float | None:
    """Combined fee of a route string like ``v4-pool:50000|v4-pool:20000`` (fee in hundredths of a basis point: 50000 = 5%).
    Returns None when no leg carries a numeric fee (unknown is never treated as zero)."""
    if not route:
        return None
    keep = 1.0
    seen = False
    for m in _LEG.finditer(route):
        f = m.group("fee")
        if f == "?":
            continue
        seen = True
        keep *= 1.0 - int(f) / 1_000_000.0
    return None if not seen else round((1.0 - keep) * 100.0, 4)


def cost_breakdown(quote: Quote, reference_price: float | None, side: Side) -> dict[str, Any]:
    """Everything in PERCENT. ``total_cost_pct`` > 0 means the trade is worse than the reference price by that much.

    BUY: price is USDC per token, so a higher quote price is worse. SELL: price is USDC received per token, lower is worse.
    ``deviation_ex_fee_pct`` is what is left after removing the pool fee: the part that is NOT explained by the fee schedule.
    """
    out: dict[str, Any] = {"fee_pct": quote.fee_pct, "price_impact_pct": quote.price_impact_pct, "total_cost_pct": None,
                           "deviation_ex_fee_pct": None, "unexplained_cost_pct": None}
    if not reference_price or reference_price <= 0 or not quote.price or quote.price <= 0:
        return out
    ratio = quote.price / reference_price
    total = (ratio - 1.0) * 100.0 if side == Side.BUY else (1.0 / ratio - 1.0) * 100.0
    out["total_cost_pct"] = round(total, 4)
    fee = quote.fee_pct
    if fee is not None:
        keep = 1.0 - fee / 100.0
        adj = ratio * keep if side == Side.BUY else ratio / keep if keep else ratio
        out["deviation_ex_fee_pct"] = round(abs(adj - 1.0) * 100.0, 4)
        # cost beyond fee and reported impact: the part nobody can explain (stale reference, thin pool, hook fee, ...)
        out["unexplained_cost_pct"] = round(total - fee - (quote.price_impact_pct or 0.0), 4)
    return out
