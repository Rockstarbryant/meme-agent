"""Trading venues the agent can quote and (when verified) execute on. Uniswap Trading API and KyberSwap on Arc today."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.chains.base import FillDetails, SimulationResult, TxReceipt
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction


class VenuePolicy(BaseModel):
    """The limits a quote must satisfy to be executable. All percentages."""
    max_price_impact_pct: float = 2.0
    max_total_cost_pct: float = 5.0        # pool fees + impact, vs the reference (market) price
    max_unexplained_pct: float = 3.0       # cost that neither the fee schedule nor reported impact explains
    max_deviation_pct: float = 3.0         # |quote - market| after removing the pool fee (same meaning as the live pre-flight)
    require_executable: bool = True


class VenueResult(BaseModel):
    """One venue's answer, shaped for the agent (numbers only) and for the audit trail. ``quote`` is internal."""
    venue: str
    ok: bool
    error: str = ""
    latency_ms: float | None = None
    amount_in_usdc: float | None = None
    expected_out: float | None = None
    price: float | None = None
    fee_pct: float | None = None
    price_impact_pct: float | None = None
    total_cost_pct: float | None = None
    unexplained_cost_pct: float | None = None
    route: str | None = None
    router_address: str | None = None
    executable: bool = False
    not_executable_reason: str = ""
    passes_policy: bool = False
    policy_failures: list[str] = Field(default_factory=list)
    quote: Quote | None = Field(default=None, exclude=True)

    def agent_view(self) -> dict[str, Any]:
        d = self.model_dump(exclude={"quote", "router_address"})
        return {k: v for k, v in d.items() if v not in (None, "", [])}


class Venue(ABC):
    name: str = ""
    label: str = ""

    @abstractmethod
    async def quote(self, request: TradeRequest) -> Quote: ...

    @abstractmethod
    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction: ...

    @abstractmethod
    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult: ...

    @abstractmethod
    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails: ...

    def router_allowlist(self) -> set[str]:
        return set()

    def executable(self) -> tuple[bool, str]:
        """(can this venue place a live order right now, why not)."""
        return True, ""

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None
