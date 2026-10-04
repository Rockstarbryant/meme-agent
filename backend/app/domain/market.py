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


# Trading windows exposed to strategies, the risk engine, the AI and the UI.
WINDOWS = ("5m", "15m", "1h", "2h", "4h", "6h", "12h", "24h")
WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "24h": 86400}
# Holder-growth windows (computed from our own holder-count history, so they fill in as the worker runs).
HOLDER_GROWTH_WINDOWS = ("1h", "6h", "24h")


class WindowStats(BaseModel):
    """Trading activity for one window. None = unknown (never silently zero).

    ``buyers``/``sellers`` are UNIQUE trading addresses; ``buys``/``sells`` are transaction counts.
    ``basis`` says where the numbers came from:
      pool_stats        provider-computed rolling window (GeckoTerminal)
      trades            computed from the pool's trade history (only used when that history fully covers the window)
      pool_stats+trades pool stats for counts/uniques plus a USD buy/sell split from trade history
    """

    buyers: int | None = None
    sellers: int | None = None
    buys: int | None = None
    sells: int | None = None
    volume_usd: float | None = None
    buy_volume_usd: float | None = None
    sell_volume_usd: float | None = None
    price_change_pct: float | None = None
    basis: str | None = None


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
    token_name: str | None = None  # display only (e.g. "ArcLand"); NEVER sent to the LLM (untrusted text)
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

    # Per-window trading stats keyed by WINDOWS ("5m" ... "24h"). Windows a provider cannot supply stay absent.
    windows: dict[str, WindowStats] = Field(default_factory=dict)

    holder_count: int | None = None
    # Holder-count growth in percent over HOLDER_GROWTH_WINDOWS; absent until our history spans that window.
    holder_growth: dict[str, float] = Field(default_factory=dict)
    # Share of circulating supply held by the top 5% / 20% / 30% of holders (by holder COUNT). None when too few
    # holder rows could be fetched to compute it exactly (never a partial figure presented as complete).
    top_5pct_holders_pct: float | None = None
    top_20pct_holders_pct: float | None = None
    top_30pct_holders_pct: float | None = None
    holders_sampled: int | None = None  # how many holder rows the concentration figures were computed from
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

    # ------------------------------------------------------------------ windows
    def win(self, name: str) -> WindowStats | None:
        return self.windows.get(name)

    def buyers_in(self, name: str) -> int | None:
        w = self.windows.get(name)
        if w is not None and w.buyers is not None:
            return w.buyers
        return {"5m": self.unique_buyers_5m, "15m": self.unique_buyers_15m}.get(name)

    def sellers_in(self, name: str) -> int | None:
        w = self.windows.get(name)
        if w is not None and w.sellers is not None:
            return w.sellers
        return {"5m": self.unique_sellers_5m, "15m": self.unique_sellers_15m}.get(name)

    def trades_in(self, name: str) -> int | None:
        """Total buys+sells in a window (None when neither count is known)."""
        w = self.windows.get(name)
        b = w.buys if w is not None else None
        s = w.sells if w is not None else None
        if b is None and s is None and name == "5m":
            b, s = self.buys_5m, self.sells_5m
        if b is None and s is None:
            return None
        return (b or 0) + (s or 0)

    def has_two_way_trading(self) -> bool | None:
        """True when BOTH buyers and sellers were observed in some window (the evidence a token can be bought AND
        sold). False when trading was observed but only one side ever appeared. None when no window is known."""
        saw_any = False
        for name in ("24h", "12h", "6h", "4h", "2h", "1h", "15m", "5m"):
            b, sl = self.buyers_in(name), self.sellers_in(name)
            if b is None and sl is None:
                bt = self.windows.get(name)
                if bt is not None and (bt.buys is not None or bt.sells is not None):
                    b, sl = bt.buys, bt.sells
            if b is None and sl is None:
                continue
            saw_any = True
            if (b or 0) > 0 and (sl or 0) > 0:
                return True
        return False if saw_any else None

    def one_sided_trading(self, min_buys: int = 5) -> bool:
        """Honeypot-like signature: at least ``min_buys`` buys in 24h and NO sells at all."""
        w = self.windows.get("24h")
        if w is None or w.buys is None or w.sells is None:
            return False
        return w.buys >= min_buys and w.sells == 0

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
