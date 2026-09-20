from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.types import ExitReason, TradingMode


class Position(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    mode: TradingMode
    chain: str
    launchpad: str | None = None
    token_address: str
    symbol: str | None = None
    opened_at: datetime
    entry_price: float
    quantity: float
    initial_quantity: float
    cost_basis_usdc: float  # cost of the REMAINING quantity (fees included)
    total_invested_usdc: float
    realized_pnl_usdc: float = 0.0
    peak_price: float
    last_price: float
    last_new_high_at: datetime
    tiers_hit: list[int] = Field(default_factory=list)
    status: str = "OPEN"
    strategy_id: str = ""
    strategy_version: int = 0
    decision_id: str = ""
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis_usdc

    @property
    def gain_pct(self) -> float:
        return (self.last_price / self.entry_price - 1.0) * 100.0

    @property
    def peak_gain_pct(self) -> float:
        return (self.peak_price / self.entry_price - 1.0) * 100.0

    @property
    def drawdown_from_peak_pct(self) -> float:
        return max(0.0, (self.peak_price - self.last_price) / self.peak_price * 100.0)

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"


class PortfolioState:
    """Single source of truth for balances/positions. Used identically by PAPER and LIVE."""

    NEW_HIGH_EPS = 0.01  # 1% above previous peak counts as real progress

    def __init__(self, cash_usdc: float, mode: TradingMode = TradingMode.PAPER):
        self.mode = mode
        self.starting_cash = cash_usdc
        self.cash_usdc = cash_usdc
        self.positions: dict[str, Position] = {}
        self.closed: list[Position] = []
        self.realized_today = 0.0
        self.realized_total = 0.0
        self.day: date | None = None
        self.last_trade_at: dict[str, datetime] = {}
        self.pending_tokens: set[str] = set()

    # ---- queries -----------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def open_count(self) -> int:
        return len(self.open_positions())

    def position_for_token(self, token: str) -> Position | None:
        t = token.lower()
        for p in self.open_positions():
            if p.token_address.lower() == t:
                return p
        return None

    def has_open_position(self, token: str) -> bool:
        return self.position_for_token(token) is not None

    def total_exposure(self) -> float:
        return sum(p.market_value for p in self.open_positions())

    def token_exposure(self, token: str) -> float:
        p = self.position_for_token(token)
        return p.market_value if p else 0.0

    def chain_exposure(self, chain: str) -> float:
        return sum(p.market_value for p in self.open_positions() if p.chain == chain)

    def launchpad_exposure(self, launchpad: str) -> float:
        return sum(p.market_value for p in self.open_positions() if p.launchpad == launchpad)

    def total_value(self) -> float:
        return self.cash_usdc + self.total_exposure()

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions())

    def daily_pnl(self, now: datetime) -> float:
        """Realized today + unrealized LOSSES only, so open gains cannot mask a loss limit breach."""
        self._roll(now)
        return self.realized_today + sum(min(p.unrealized_pnl, 0.0) for p in self.open_positions())

    def _roll(self, now: datetime) -> None:
        if self.day != now.date():
            self.day = now.date()
            self.realized_today = 0.0

    # ---- mutations ---------------------------------------------------
    def apply_buy(self, *, chain: str, launchpad: str | None, token: str, symbol: str | None,
                  qty: float, price: float, cost_usdc: float, now: datetime,
                  strategy_id: str = "", strategy_version: int = 0, decision_id: str = "") -> Position:
        self._roll(now)
        if cost_usdc > self.cash_usdc + 1e-9:
            raise ValueError("insufficient cash")
        self.cash_usdc -= cost_usdc
        pos = Position(mode=self.mode, chain=chain, launchpad=launchpad, token_address=token,
                       symbol=symbol, opened_at=now, entry_price=price, quantity=qty,
                       initial_quantity=qty, cost_basis_usdc=cost_usdc, total_invested_usdc=cost_usdc,
                       peak_price=price, last_price=price, last_new_high_at=now,
                       strategy_id=strategy_id, strategy_version=strategy_version, decision_id=decision_id)
        self.positions[pos.id] = pos
        self.last_trade_at[f"{chain}:{token.lower()}"] = now
        return pos

    def apply_sell(self, position_id: str, qty: float, price: float, proceeds_usdc: float,
                   now: datetime, reason: ExitReason | None = None) -> float:
        self._roll(now)
        pos = self.positions[position_id]
        qty = min(qty, pos.quantity)
        portion = qty / pos.quantity if pos.quantity > 0 else 1.0
        cost_portion = pos.cost_basis_usdc * portion
        pnl = proceeds_usdc - cost_portion
        pos.quantity -= qty
        pos.cost_basis_usdc -= cost_portion
        pos.realized_pnl_usdc += pnl
        self.cash_usdc += proceeds_usdc
        self.realized_today += pnl
        self.realized_total += pnl
        self.last_trade_at[f"{pos.chain}:{pos.token_address.lower()}"] = now
        if pos.quantity <= 1e-12:
            pos.quantity = 0.0
            pos.status = "CLOSED"
            pos.closed_at = now
            pos.exit_reason = reason
            self.closed.append(pos)
        return pnl

    def mark(self, position_id: str, price: float, now: datetime) -> None:
        pos = self.positions[position_id]
        pos.last_price = price
        if price > pos.peak_price:
            if price >= pos.peak_price * (1 + self.NEW_HIGH_EPS):
                pos.last_new_high_at = now
            pos.peak_price = price

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "starting_cash": self.starting_cash, "cash_usdc": self.cash_usdc,
                "realized_today": self.realized_today, "realized_total": self.realized_total,
                "day": self.day.isoformat() if self.day else None,
                "positions": {i: p.model_dump(mode="json") for i, p in self.positions.items() if p.is_open},
                "last_trade_at": {k: v.isoformat() for k, v in self.last_trade_at.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        pf = cls(d["cash_usdc"], TradingMode(d["mode"]))
        pf.starting_cash, pf.realized_today, pf.realized_total = d["starting_cash"], d["realized_today"], d["realized_total"]
        pf.day = date.fromisoformat(d["day"]) if d.get("day") else None
        pf.positions = {i: Position.model_validate(p) for i, p in d.get("positions", {}).items()}
        pf.last_trade_at = {k: datetime.fromisoformat(v) for k, v in d.get("last_trade_at", {}).items()}
        return pf

    def snapshot(self, now: datetime) -> dict:
        return {
            "mode": self.mode.value, "cash_usdc": round(self.cash_usdc, 6),
            "exposure_usdc": round(self.total_exposure(), 6), "total_value_usdc": round(self.total_value(), 6),
            "open_positions": self.open_count(), "daily_pnl_usdc": round(self.daily_pnl(now), 6),
            "realized_total_usdc": round(self.realized_total, 6),
        }
