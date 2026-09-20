"""Deterministic position management. The LLM has NO role here."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.types import ExitReason
from app.domain.market import MarketState
from app.portfolio.state import Position


class TakeProfitTier(BaseModel):
    gain_pct: float
    sell_pct_of_initial: float


class ExitConfig(BaseModel):
    hard_stop_pct: float = 20.0
    tiers: list[TakeProfitTier] = Field(default_factory=lambda: [
        TakeProfitTier(gain_pct=30, sell_pct_of_initial=15),
        TakeProfitTier(gain_pct=60, sell_pct_of_initial=20),
        TakeProfitTier(gain_pct=100, sell_pct_of_initial=25),
        TakeProfitTier(gain_pct=200, sell_pct_of_initial=25),
    ])
    trailing_stop_pct: float = 20.0           # drawdown from peak that closes the remainder
    trailing_activation_gain_pct: float = 30.0  # armed once first tier is hit or peak gain reaches this
    stagnation_seconds: int = 1800
    stagnation_min_gain_pct: float = 10.0
    momentum_exit_price_change_5m_pct: float = -8.0
    momentum_exit_max_buy_sell_ratio: float = 1.0
    liquidity_drop_exit_pct: float = 40.0
    exit_max_slippage_pct: float = 15.0


class ExitDecision(BaseModel):
    position_id: str
    reason: ExitReason
    close_all: bool
    quantity: float
    tier_index: int | None = None
    detail: str = ""


class PositionManager:
    def __init__(self, config: ExitConfig):
        self.cfg = config

    def is_trailing_armed(self, pos: Position) -> bool:
        return bool(pos.tiers_hit) or pos.peak_gain_pct >= self.cfg.trailing_activation_gain_pct

    def evaluate(self, pos: Position, now: datetime, market: MarketState | None = None, *,
                 manual_close: bool = False, emergency: bool = False,
                 risk_escalation: str | None = None) -> list[ExitDecision]:
        if not pos.is_open:
            return []
        c = self.cfg

        def close(reason: ExitReason, detail: str) -> list[ExitDecision]:
            return [ExitDecision(position_id=pos.id, reason=reason, close_all=True,
                                 quantity=pos.quantity, detail=detail)]

        if emergency:
            return close(ExitReason.EMERGENCY, "emergency close")
        if manual_close:
            return close(ExitReason.MANUAL, "manual close")
        if pos.gain_pct <= -c.hard_stop_pct:
            return close(ExitReason.HARD_STOP, f"gain {pos.gain_pct:.1f}% <= -{c.hard_stop_pct}%")
        if market is not None:
            chg = market.liquidity_change_5m_pct
            if chg is not None and chg <= -c.liquidity_drop_exit_pct:
                return close(ExitReason.LIQUIDITY_DETERIORATION, f"liquidity {chg:.1f}% in 5m")
        if risk_escalation:
            return close(ExitReason.RISK_ESCALATION, risk_escalation)
        if self.is_trailing_armed(pos) and pos.drawdown_from_peak_pct >= c.trailing_stop_pct:
            return close(ExitReason.TRAILING_STOP,
                         f"drawdown {pos.drawdown_from_peak_pct:.1f}% from peak >= {c.trailing_stop_pct}%")

        out: list[ExitDecision] = []
        remaining = pos.quantity
        for i, tier in enumerate(c.tiers):
            if i in pos.tiers_hit or pos.gain_pct < tier.gain_pct:
                continue
            qty = min(remaining, pos.initial_quantity * tier.sell_pct_of_initial / 100.0)
            if qty <= 0:
                continue
            remaining -= qty
            out.append(ExitDecision(position_id=pos.id, reason=ExitReason.TAKE_PROFIT, close_all=False,
                                    quantity=qty, tier_index=i,
                                    detail=f"gain {pos.gain_pct:.1f}% >= {tier.gain_pct}%"))
        if out:
            return out

        if market is not None:
            pc5, ratio = market.price_change_5m, market.buy_sell_volume_ratio()
            if (pc5 is not None and ratio is not None and pc5 <= c.momentum_exit_price_change_5m_pct
                    and ratio < c.momentum_exit_max_buy_sell_ratio):
                return close(ExitReason.MOMENTUM_DETERIORATION, f"5m {pc5:.1f}%, buy/sell {ratio:.2f}")

        held = (now - pos.opened_at).total_seconds()
        since_high = (now - pos.last_new_high_at).total_seconds()
        if (held >= c.stagnation_seconds and since_high >= c.stagnation_seconds
                and pos.gain_pct < c.stagnation_min_gain_pct):
            return close(ExitReason.STAGNATION,
                         f"no new high for {since_high:.0f}s, gain {pos.gain_pct:.1f}% < {c.stagnation_min_gain_pct}%")
        return []
