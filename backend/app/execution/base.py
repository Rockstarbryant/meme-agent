from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.types import TradingMode
from app.domain.trade import ApprovedTrade, ExecutionResult, Quote, TradeRequest


class ExecutionAdapter(ABC):
    """Strategy code sees only this interface and cannot tell PAPER from LIVE."""

    mode: TradingMode

    @abstractmethod
    async def buy(self, approved: ApprovedTrade) -> ExecutionResult: ...

    @abstractmethod
    async def sell(self, approved: ApprovedTrade) -> ExecutionResult: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    @abstractmethod
    async def get_position(self, token_address: str): ...

    @abstractmethod
    async def quote(self, request: TradeRequest) -> Quote: ...

    @abstractmethod
    async def simulate(self, request: TradeRequest) -> dict: ...

    async def estimate_trade(self, request: TradeRequest) -> dict:
        q = await self.quote(request)
        return {"price": q.price, "price_impact_pct": q.price_impact_pct, "fee_usdc": q.fee_usdc}
