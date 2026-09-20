"""Trade-shaped value objects shared by risk, wallets and execution."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.types import OrderStatus, RiskDecision, Side, TradingMode


class TradeRequest(BaseModel):
    """A structured, deterministic trade instruction. Never contains LLM-authored calldata."""

    model_config = ConfigDict(frozen=True)
    idempotency_key: str
    mode: TradingMode
    chain: str
    token_address: str
    side: Side
    amount_usdc: float | None = None  # BUY notional
    quantity: float | None = None  # SELL token quantity
    max_slippage_pct: float
    reference_price: float | None = None
    launchpad: str | None = None
    pool_address: str | None = None
    decision_id: str = ""
    strategy_id: str = ""
    strategy_version: int = 0
    position_id: str | None = None
    reason: str = ""


class ApprovedTrade(BaseModel):
    """Proof-carrying approval. Executors refuse anything without a valid signature."""

    model_config = ConfigDict(frozen=True)
    request: TradeRequest
    assessment_id: str
    decision: RiskDecision
    signature: str
    approved_at: datetime


class Quote(BaseModel):
    side: Side
    token_address: str
    amount_in: float
    expected_out: float
    price: float
    price_impact_pct: float
    fee_usdc: float
    router_address: str
    pool_liquidity_usdc: float | None = None
    quoted_at: datetime
    expires_at: datetime
    source: str


class UnsignedTransaction(BaseModel):
    """Built ONLY by a deterministic chain adapter from a verified Quote."""

    chain: str
    chain_id: int
    to: str  # router/contract; must be on the allowlist
    data: str  # hex calldata
    value: int = 0
    token_address: str
    side: Side
    amount_usdc: float
    min_out: float
    slippage_pct: float
    deadline: datetime
    built_by: str = "deterministic-adapter"
    # Canonical call shape for providers that take an ABI call instead of raw calldata (e.g. Circle CLI `wallet execute`).
    function_signature: str | None = None   # e.g. "approve(address,uint256)"
    params: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    order_id: str
    idempotency_key: str
    mode: TradingMode
    status: OrderStatus
    simulated: bool
    side: Side
    token_address: str
    tx_hash: str | None = None
    signing_request_id: str | None = None
    provider_tx_id: str | None = None  # wallet-provider transaction id when no chain hash is available yet
    requested_amount_usdc: float | None = None
    filled_quantity: float = 0.0
    avg_price: float | None = None
    notional_usdc: float = 0.0
    fee_usdc: float = 0.0
    slippage_pct: float | None = None
    price_impact_pct: float | None = None
    position_id: str | None = None
    error: str | None = None
    label: str = ""
    at: datetime | None = None
