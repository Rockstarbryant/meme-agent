"""Normalized, chain-agnostic market model. Strategy code consumes only this."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContractInfo(BaseModel):
    """None = unknown / could not be verified. Unknown is never treated as safe."""

    verified: bool | None = None
    owner_renounced: bool | None = None
    mint_authority_active: bool | None = None
    pausable: bool | None = None
    blacklist_capability: bool | None = None
    transfer_restricted: bool | None = None
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None
    max_tx_limit: bool | None = None
    max_wallet_limit: bool | None = None
    is_proxy: bool | None = None
    sell_simulation_ok: bool | None = None
    # Provenance. How the values above were obtained, so a heuristic is never mistaken for a full simulation.
    #   verification_source: "blockscout" | "etherscan" | None
    #   sell_check_method:   "holder_transfer_probe" (heuristic) | "router_simulation" (full) | None
    #   checks_run:          names of on-chain / explorer checks that actually ran for this token
    verification_source: str | None = None
    sell_check_method: str | None = None
    checks_run: list[str] = Field(default_factory=list)


class MarketState(BaseModel):
    chain: str
    token_address: str
    timestamp: datetime
    launchpad: str | None = None
    # Launchpad recovered by enrichment from the creation tx. DISPLAY ONLY: it is deliberately separate from
    # ``launchpad`` so the risk engine's launchpad allowlist / exposure limits are not silently switched on for
    # every token that gets detected.
    launchpad_detected: str | None = None
    launchpad_evidence: str | None = None
    pool_address: str | None = None
    pool_id: str | None = None
    symbol: str | None = None  # display only; NEVER sent to the LLM (untrusted text)
    creator_address: str | None = None
    token_created_at: datetime | None = None

    price: float | None = None
    market_cap: float | None = None
    liquidity: float | None = None
    liquidity_change_5m_pct: float | None = None

    volume_1m: float | None = None
    volume_5m: float | None = None
    volume_15m: float | None = None
    buy_volume_5m: float | None = None
    sell_volume_5m: float | None = None
    buys_1m: int | None = None
    sells_1m: int | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None

    unique_buyers_1m: int | None = None
    unique_buyers_5m: int | None = None
    unique_buyers_15m: int | None = None
    unique_buyers_prev_5m: int | None = None
    unique_sellers_1m: int | None = None
    unique_sellers_5m: int | None = None
    unique_sellers_15m: int | None = None

    holder_count: int | None = None
    holder_growth_pct: float | None = None
    holder_growth_window_s: float | None = None  # span the growth figure was measured over
    top5_holder_pct: float | None = None
    top10_holder_pct: float | None = None
    top20_holder_pct: float | None = None

    creator_known: bool = False
    creator_balance_pct: float | None = None
    creator_sold_pct: float | None = None

    price_change_1m: float | None = None
    price_change_5m: float | None = None
    price_change_15m: float | None = None
    recent_high: float | None = None
    volatility_pct: float | None = None

    contract: ContractInfo = Field(default_factory=ContractInfo)
    expected_price_impact_pct: float | None = None
    mev_risk_score: float | None = None  # 0..1

    # "usd" = real buy/sell USD from trade history; "estimated_from_counts" = total volume split by buy/sell
    # counts; "counts" = only buy/sell transaction counts are known. Never shown as if it were exact USD flow.
    buy_sell_basis: str | None = None
    mev_method: str | None = None
    holder_basis: str | None = None  # how top-N % was computed (which holders were excluded)

    # When OUR scanner produced this snapshot (age above is the token's age, not the scan time).
    scanned_at: datetime | None = None
    enriched_at: datetime | None = None
    # Human-readable reasons a field is still missing, e.g. "holders: below liquidity floor (not enriched)".
    enrichment_gaps: list[str] = Field(default_factory=list)

    data_sources: list[str] = Field(default_factory=list)
    is_demo: bool = False  # DEMO DATA must never be mistaken for live data

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.token_address.lower()}"

    def age_seconds(self, now: datetime) -> float | None:
        if self.token_created_at is None:
            return None
        return (now - self.token_created_at).total_seconds()

    def data_age_seconds(self, now: datetime) -> float:
        return (now - self.timestamp).total_seconds()

    def drawdown_from_high_pct(self) -> float | None:
        if not self.price or not self.recent_high or self.recent_high <= 0:
            return None
        return max(0.0, (self.recent_high - self.price) / self.recent_high * 100)

    def buy_sell_count_ratio(self) -> float | None:
        """Buys / sells by transaction COUNT over 5m. None when there were no trades at all."""
        if self.buys_5m is None and self.sells_5m is None:
            return None
        b, sl = self.buys_5m or 0, self.sells_5m or 0
        if b == 0 and sl == 0:
            return None
        if sl == 0:
            return 10.0
        return min(b / sl, 10.0)

    def buy_sell_volume_ratio(self) -> float | None:
        """Buy/sell pressure ratio (capped at 10). Prefers USD flow; falls back to transaction counts."""
        if self.buy_volume_5m is not None and self.sell_volume_5m is not None:
            if self.sell_volume_5m <= 0:
                return 10.0 if self.buy_volume_5m > 0 else self.buy_sell_count_ratio()
            return min(self.buy_volume_5m / self.sell_volume_5m, 10.0)
        return self.buy_sell_count_ratio()

    def net_pressure(self) -> float | None:
        if self.buy_volume_5m is not None and self.sell_volume_5m is not None:
            tot = self.buy_volume_5m + self.sell_volume_5m
            if tot > 0:
                return (self.buy_volume_5m - self.sell_volume_5m) / tot
        b, sl = self.buys_5m, self.sells_5m
        if b is None and sl is None:
            return None
        tot = (b or 0) + (sl or 0)
        return None if tot <= 0 else ((b or 0) - (sl or 0)) / tot

    def derive_buy_sell(self) -> "MarketState":
        """Fill the USD split from counts when a provider only gave totals, and record the basis. Idempotent."""
        if self.buy_sell_basis is None:
            if self.buy_volume_5m is not None and self.sell_volume_5m is not None:
                self.buy_sell_basis = "usd"
            elif self.volume_5m and self.volume_5m > 0 and (self.buys_5m or self.sells_5m):
                tot = (self.buys_5m or 0) + (self.sells_5m or 0)
                self.buy_volume_5m = self.volume_5m * (self.buys_5m or 0) / tot
                self.sell_volume_5m = self.volume_5m * (self.sells_5m or 0) / tot
                self.buy_sell_basis = "estimated_from_counts"
            elif self.buys_5m is not None or self.sells_5m is not None:
                self.buy_sell_basis = "counts" if (self.buys_5m or self.sells_5m) else None
        return self
