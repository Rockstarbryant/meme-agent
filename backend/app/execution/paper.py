"""Paper execution: real market data, simulated fills. Has NO chain/wallet dependency by construction."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable

from app.chains.base import MarketDataProvider
from app.core.clock import utcnow
from app.core.errors import ApprovalError, DataUnavailable
from app.core.types import OrderStatus, Side, TradingMode
from app.domain.market import MarketState
from app.domain.trade import ApprovedTrade, ExecutionResult, Quote, TradeRequest
from app.execution.base import ExecutionAdapter
from app.execution.math import estimate_price_impact_pct
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover

LABEL = "PAPER (SIMULATED - no blockchain transaction)"


class PaperExecutionEngine(ExecutionAdapter):
    mode = TradingMode.PAPER

    def __init__(self, portfolio: PortfolioState, market_data: MarketDataProvider, approver: TradeApprover,
                 fee_bps: float = 30.0, extra_slippage_bps: float = 10.0, max_fill_liquidity_ratio: float = 0.02,
                 clock: Callable[[], datetime] = utcnow):
        self.portfolio, self.market_data, self.approver = portfolio, market_data, approver
        self.fee_bps, self.extra_slip_bps, self.max_fill_ratio = fee_bps, extra_slippage_bps, max_fill_liquidity_ratio
        self._clock = clock
        self._results: dict[str, ExecutionResult] = {}
        self.orders: list[ExecutionResult] = []

    # ---- helpers ------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _result(self, req: TradeRequest, status: OrderStatus, **kw) -> ExecutionResult:
        r = ExecutionResult(order_id=f"paper-{uuid.uuid4().hex[:12]}", idempotency_key=req.idempotency_key,
                            mode=TradingMode.PAPER, status=status, simulated=True, side=req.side,
                            token_address=req.token_address, tx_hash=None, label=LABEL, at=self._now(), **kw)
        self.orders.append(r)
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            self._results[req.idempotency_key] = r
        return r

    def _check(self, approved: ApprovedTrade, side: Side) -> TradeRequest:
        if not self.approver.verify(approved):
            raise ApprovalError("invalid or forged trade approval")
        req = approved.request
        if req.mode != TradingMode.PAPER or req.side != side:
            raise ApprovalError("approval does not match paper execution")
        return req

    async def _market(self, token: str) -> MarketState | None:
        try:
            m = await self.market_data.get_market_state(token)
        except DataUnavailable:
            return None
        return m if m.price and m.price > 0 and m.liquidity else None

    # ---- ExecutionAdapter --------------------------------------------
    async def buy(self, approved: ApprovedTrade) -> ExecutionResult:
        req = self._check(approved, Side.BUY)
        if req.idempotency_key in self._results:
            return self._results[req.idempotency_key]
        amount = req.amount_usdc or 0.0
        m = await self._market(req.token_address)
        if m is None:
            return self._result(req, OrderStatus.FAILED, error="NO_MARKET_DATA", requested_amount_usdc=amount)
        if amount <= 0 or amount > self.portfolio.cash_usdc + 1e-9:
            return self._result(req, OrderStatus.FAILED, error="INSUFFICIENT_FUNDS", requested_amount_usdc=amount)
        fill_amount = min(amount, m.liquidity * self.max_fill_ratio)
        impact = estimate_price_impact_pct(fill_amount, m.liquidity)
        slip = impact + self.extra_slip_bps / 100.0
        if slip > req.max_slippage_pct:
            return self._result(req, OrderStatus.FAILED, error="SLIPPAGE_EXCEEDED", requested_amount_usdc=amount,
                                slippage_pct=slip, price_impact_pct=impact)
        fee = fill_amount * self.fee_bps / 1e4
        fill_price = m.price * (1 + slip / 100.0)
        qty = (fill_amount - fee) / fill_price
        pos = self.portfolio.apply_buy(chain=req.chain, launchpad=req.launchpad, token=req.token_address,
                                       symbol=m.symbol, qty=qty, price=fill_price, cost_usdc=fill_amount,
                                       now=self._now(), strategy_id=req.strategy_id,
                                       strategy_version=req.strategy_version, decision_id=req.decision_id)
        status = OrderStatus.FILLED if fill_amount >= amount - 1e-9 else OrderStatus.PARTIALLY_FILLED
        return self._result(req, status, requested_amount_usdc=amount, filled_quantity=qty, avg_price=fill_price,
                            notional_usdc=fill_amount, fee_usdc=fee, slippage_pct=slip, price_impact_pct=impact,
                            position_id=pos.id)

    async def sell(self, approved: ApprovedTrade) -> ExecutionResult:
        req = self._check(approved, Side.SELL)
        if req.idempotency_key in self._results:
            return self._results[req.idempotency_key]
        pos = self.portfolio.positions.get(req.position_id or "")
        if pos is None or not pos.is_open:
            return self._result(req, OrderStatus.FAILED, error="NO_OPEN_POSITION")
        m = await self._market(req.token_address)
        if m is None:
            return self._result(req, OrderStatus.FAILED, error="NO_MARKET_DATA", position_id=pos.id)
        qty = min(req.quantity or 0.0, pos.quantity)
        notional = qty * m.price
        impact = estimate_price_impact_pct(notional, m.liquidity)
        slip = impact + self.extra_slip_bps / 100.0
        if slip > req.max_slippage_pct:
            return self._result(req, OrderStatus.FAILED, error="SLIPPAGE_EXCEEDED", slippage_pct=slip,
                                price_impact_pct=impact, position_id=pos.id)
        fill_price = m.price * (1 - slip / 100.0)
        gross = qty * fill_price
        fee = gross * self.fee_bps / 1e4
        self.portfolio.apply_sell(pos.id, qty, fill_price, gross - fee, self._now())
        return self._result(req, OrderStatus.FILLED, filled_quantity=qty, avg_price=fill_price,
                            notional_usdc=gross, fee_usdc=fee, slippage_pct=slip, price_impact_pct=impact,
                            position_id=pos.id)

    async def get_balance(self) -> float:
        return self.portfolio.cash_usdc

    async def get_position(self, token_address: str):
        return self.portfolio.position_for_token(token_address)

    async def quote(self, request: TradeRequest) -> Quote:
        m = await self._market(request.token_address)
        if m is None:
            raise DataUnavailable("no market data for quote")
        now = self._now()
        amt = request.amount_usdc or (request.quantity or 0) * m.price
        impact = estimate_price_impact_pct(amt, m.liquidity)
        return Quote(side=request.side, token_address=request.token_address, amount_in=amt,
                     expected_out=amt / (m.price * (1 + impact / 100)), price=m.price, price_impact_pct=impact,
                     fee_usdc=amt * self.fee_bps / 1e4, router_address="PAPER", pool_liquidity_usdc=m.liquidity,
                     quoted_at=now, expires_at=now, source="paper-estimate")

    async def simulate(self, request: TradeRequest) -> dict:
        return {"ok": True, "simulated": True, "note": LABEL}
