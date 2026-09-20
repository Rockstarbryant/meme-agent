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


class MarketState(BaseModel):
    chain: str
    token_address: str
    timestamp: datetime
    launchpad: str | None = None
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

    def buy_sell_volume_ratio(self) -> float | None:
        if self.buy_volume_5m is None or self.sell_volume_5m is None:
            return None
        if self.sell_volume_5m <= 0:
            return 10.0 if self.buy_volume_5m > 0 else None
        return min(self.buy_volume_5m / self.sell_volume_5m, 10.0)

    def net_pressure(self) -> float | None:
        if self.buy_volume_5m is None or self.sell_volume_5m is None:
            return None
        tot = self.buy_volume_5m + self.sell_volume_5m
        return None if tot <= 0 else (self.buy_volume_5m - self.sell_volume_5m) / tot
