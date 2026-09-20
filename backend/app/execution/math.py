def estimate_price_impact_pct(amount_usdc: float, liquidity_usdc: float) -> float:
    """Constant-product approximation: quote-side reserve ~= liquidity/2.

    This is an ESTIMATE used by paper fills and as a risk fallback; live trading must use a real quote.
    """
    if liquidity_usdc <= 0:
        return 100.0
    reserve = liquidity_usdc / 2.0
    return amount_usdc / (reserve + amount_usdc) * 100.0
