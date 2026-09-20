from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.errors import IntegrationNotVerified
from app.domain.market import MarketState
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction


class ChainHealth(BaseModel):
    ok: bool
    chain_id: int | None = None
    block_number: int | None = None
    latency_ms: float | None = None
    detail: str = ""


class TxReceipt(BaseModel):
    tx_hash: str
    status: int  # 1 success, 0 reverted
    block_number: int | None = None
    gas_used: int | None = None
    logs: list[dict] = Field(default_factory=list)


class SimulationResult(BaseModel):
    ok: bool
    detail: str = ""


class FillDetails(BaseModel):
    filled_quantity: float
    avg_price: float
    fee_usdc: float


class ChainAdapter(ABC):
    """Everything chain-specific lives behind this interface. Strategy code never imports it."""

    name: str
    chain_id: int
    live_trading_verified: bool = False  # True only once quote/swap/fill parsing are verified
    receipt_poll_s: float = 2.0

    @abstractmethod
    async def health(self) -> ChainHealth: ...

    @abstractmethod
    async def get_receipt(self, tx_hash: str) -> TxReceipt | None: ...

    def router_allowlist(self) -> set[str]:
        return set()

    async def wait_for_receipt(self, tx_hash: str, timeout_s: float, poll_s: float | None = None) -> TxReceipt | None:
        poll_s = self.receipt_poll_s if poll_s is None else poll_s
        deadline = time.monotonic() + timeout_s
        while True:
            receipt = await self.get_receipt(tx_hash)
            if receipt is not None:
                return receipt
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll_s)

    async def quote(self, request: TradeRequest) -> Quote:
        raise IntegrationNotVerified(f"{self.name} quote", "Requires a verified DEX/launchpad router integration.")

    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        raise IntegrationNotVerified(f"{self.name} swap builder", "Requires a verified router ABI.")

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        raise IntegrationNotVerified(f"{self.name} simulation", "Requires eth_call/simulation support.")

    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails:
        raise IntegrationNotVerified(f"{self.name} fill parsing", "Requires verified swap event ABI.")


class MarketDataProvider(ABC):
    """Real data only. Unsupported/unavailable must raise DataUnavailable, never fabricate."""

    @abstractmethod
    async def get_market_state(self, token_address: str) -> MarketState: ...

    @abstractmethod
    async def discover_tokens(self) -> list[str]: ...
