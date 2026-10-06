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
    # When True, a flat position is NOT closed as "stagnant" while the market around it is improving (price trending
    # up over the last hours, or liquidity growing). Needed for established tokens, whose moves play out over hours:
    # closing them after 30 flat minutes sold positions that rose strongly a few hours later.
    stagnation_trend_guard: bool = False
    momentum_exit_price_change_5m_pct: float = -8.0
    momentum_exit_max_buy_sell_ratio: float = 1.0
    liquidity_drop_exit_pct: float = 40.0
    exit_max_slippage_pct: float = 15.0


    # ---- exit profiles: how long a trade is given to work depends on the kind of token the strategy buys ----
    @classmethod
    def launch(cls) -> "ExitConfig":
        """Fresh launches: fast and violent. Minutes, wide targets, quick stagnation exit (the original defaults)."""
        return cls()

    @classmethod
    def swing(cls) -> "ExitConfig":
        """Established, liquid tokens (liquidity_trend / liquidity_growth): moves take hours and are smaller, so the
        stop is tighter, targets are closer, and a flat position gets 12 hours (and is kept while the trend is up)."""
        return cls(hard_stop_pct=15.0,
                   tiers=[TakeProfitTier(gain_pct=10, sell_pct_of_initial=20), TakeProfitTier(gain_pct=25, sell_pct_of_initial=25),
                          TakeProfitTier(gain_pct=50, sell_pct_of_initial=25), TakeProfitTier(gain_pct=100, sell_pct_of_initial=15)],
                   trailing_stop_pct=12.0, trailing_activation_gain_pct=12.0,
                   stagnation_seconds=12 * 3600, stagnation_min_gain_pct=3.0, stagnation_trend_guard=True,
                   momentum_exit_price_change_5m_pct=-10.0, liquidity_drop_exit_pct=35.0, exit_max_slippage_pct=10.0)

    @classmethod
    def pullback(cls) -> "ExitConfig":
        """Buying a dip in an uptrend: small, quick targets and a tight stop (a failed dip should be cut early)."""
        return cls(hard_stop_pct=10.0,
                   tiers=[TakeProfitTier(gain_pct=6, sell_pct_of_initial=25), TakeProfitTier(gain_pct=12, sell_pct_of_initial=25),
                          TakeProfitTier(gain_pct=25, sell_pct_of_initial=25), TakeProfitTier(gain_pct=50, sell_pct_of_initial=15)],
                   trailing_stop_pct=7.0, trailing_activation_gain_pct=8.0,
                   stagnation_seconds=8 * 3600, stagnation_min_gain_pct=2.0, stagnation_trend_guard=True,
                   momentum_exit_price_change_5m_pct=-8.0, liquidity_drop_exit_pct=35.0, exit_max_slippage_pct=10.0)

    @classmethod
    def breakout(cls) -> "ExitConfig":
        """Volume breakouts: ride the move, protect gains quickly, leave if it does not follow through within hours."""
        return cls(hard_stop_pct=12.0,
                   tiers=[TakeProfitTier(gain_pct=10, sell_pct_of_initial=20), TakeProfitTier(gain_pct=20, sell_pct_of_initial=25),
                          TakeProfitTier(gain_pct=40, sell_pct_of_initial=25), TakeProfitTier(gain_pct=80, sell_pct_of_initial=15)],
                   trailing_stop_pct=10.0, trailing_activation_gain_pct=10.0,
                   stagnation_seconds=3 * 3600, stagnation_min_gain_pct=3.0, stagnation_trend_guard=False,
                   momentum_exit_price_change_5m_pct=-8.0, liquidity_drop_exit_pct=35.0, exit_max_slippage_pct=10.0)


# The pre-profile defaults. A saved strategy config whose exit settings still equal these was never customised, so
# it is upgraded to the strategy's own profile instead of keeping launch-token rules (see LiquidityTrendConfig).
LEGACY_LAUNCH_EXIT = {
    "hard_stop_pct": 20.0, "trailing_stop_pct": 20.0, "trailing_activation_gain_pct": 30.0, "stagnation_seconds": 1800,
    "stagnation_min_gain_pct": 10.0, "momentum_exit_price_change_5m_pct": -8.0, "momentum_exit_max_buy_sell_ratio": 1.0,
    "liquidity_drop_exit_pct": 40.0, "exit_max_slippage_pct": 15.0,
    "tiers": [(30.0, 15.0), (60.0, 20.0), (100.0, 25.0), (200.0, 25.0)],
}


def is_legacy_launch_exit(raw: object) -> bool:
    """True when ``raw`` (a stored ExitConfig dict) still holds exactly the old launch-token defaults."""
    if not isinstance(raw, dict):
        return False
    for k, want in LEGACY_LAUNCH_EXIT.items():
        if k not in raw:
            continue                      # a missing key means "default", which is the legacy value
        got = raw[k]
        if k == "tiers":
            try:
                if [(float(t["gain_pct"]), float(t["sell_pct_of_initial"])) for t in got] != want:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        elif got != want:
            return False
    return True


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
            if c.stagnation_trend_guard and market is not None and self._improving(market):
                return []   # flat, but the market around it is improving: keep giving it time
            return close(ExitReason.STAGNATION,
                         f"no new high for {since_high:.0f}s, gain {pos.gain_pct:.1f}% < {c.stagnation_min_gain_pct}%")
        return []

    @staticmethod
    def _improving(m: MarketState) -> bool:
        """Is the token's own trend or liquidity turning up? (any one signal is enough to keep waiting)"""
        for w in ("4h", "6h"):
            ws = m.windows.get(w)
            if ws is not None and ws.price_change_pct is not None and ws.price_change_pct > 0:
                return True
        w1 = m.windows.get("1h")
        if w1 is not None and w1.price_change_pct is not None and w1.price_change_pct > 1.0:
            return True
        for g in (m.liquidity_growth.get("1h"), m.liquidity_growth.get("6h")):
            if g is not None and g > 0:
                return True
        return False
