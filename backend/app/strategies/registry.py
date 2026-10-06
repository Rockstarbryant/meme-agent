"""One place that knows every strategy. Everything else (seeding, bundles, the runner, the API, screening) asks here.

Adding a strategy = write its config + class, add one ``StrategySpec`` below. Nothing else needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.portfolio.exits import ExitConfig
from app.strategies.base import Strategy
from app.strategies.liquidity_trend import LiquidityTrend, LiquidityTrendConfig
from app.strategies.more import (LiquidityGrowth, LiquidityGrowthConfig, TrendPullback, TrendPullbackConfig, VolumeBreakout,
                                 VolumeBreakoutConfig)
from app.strategies.traction_momentum import TractionMomentum, TractionMomentumConfig


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    description: str
    profile: Literal["launch", "established"]
    config_cls: type
    strategy_cls: Callable[[Any], Strategy]
    best_for: str

    def default_config(self):
        return self.config_cls()

    def default_exit(self) -> ExitConfig:
        return self.config_cls().exit

    def build(self, raw: dict | None = None) -> tuple[Strategy, Any]:
        cfg = self.config_cls(**{**(raw or {}), "strategy_id": self.id})
        return self.strategy_cls(cfg), cfg


SPECS: dict[str, StrategySpec] = {s.id: s for s in (
    StrategySpec("traction_momentum", "Traction Momentum",
                 "Buys only tokens showing measurable, sustained traction; never blind launch sniping.",
                 "launch", TractionMomentumConfig, TractionMomentum, "Fresh launches: minutes-long trades, buyer growth and buy pressure."),
    StrategySpec("liquidity_trend", "Liquidity Trend",
                 "Scores high-liquidity / established Arc tokens from Gecko trending; does not require fresh-launch buyer gates.",
                 "established", LiquidityTrendConfig, LiquidityTrend, "Established liquid tokens: steady two-way trading over hours."),
    StrategySpec("liquidity_growth", "Liquidity Growth",
                 "Buys tokens whose pool liquidity is growing (money flowing in) while price confirms but is not yet parabolic.",
                 "established", LiquidityGrowthConfig, LiquidityGrowth, "Tokens being accumulated: liquidity and market cap rising together."),
    StrategySpec("trend_pullback", "Trend Pullback",
                 "Buys a dip inside an established uptrend, once the dip stops accelerating.",
                 "established", TrendPullbackConfig, TrendPullback, "Strong 6h/24h uptrend with a modest 1h pullback."),
    StrategySpec("volume_breakout", "Volume Breakout",
                 "Buys a volume surge with buyers in control, before the move is exhausted.",
                 "established", VolumeBreakoutConfig, VolumeBreakout, "Sudden volume expansion with net buying."),
)}
DEFAULT_ID = "traction_momentum"


def known_ids() -> tuple[str, ...]:
    return tuple(SPECS)


def get(sid: str) -> StrategySpec | None:
    return SPECS.get(sid)


def select_active(enabled: list[str] | None, preferred: str | None = None) -> str:
    """Which enabled strategy the agent runs. An explicit choice wins; otherwise the original rule (liquidity_trend
    when enabled, else the first enabled known strategy) so existing accounts keep behaving the same."""
    en = [s for s in (enabled or []) if s in SPECS]
    if preferred and preferred in en:
        return preferred
    if "liquidity_trend" in en:
        return "liquidity_trend"
    return en[0] if en else DEFAULT_ID


def build_strategy(sid: str, raw: dict | None = None) -> tuple[Strategy, Any]:
    spec = SPECS.get(sid) or SPECS[DEFAULT_ID]
    return spec.build(raw)


def exit_configs_for_positions(active_id: str, active_exit: ExitConfig) -> dict[str, ExitConfig]:
    """Exit rules per strategy id: the active strategy uses the user's saved exit settings, every other strategy its
    own default profile, so a position keeps the rules of the strategy that opened it after the user switches."""
    out = {sid: spec.default_exit() for sid, spec in SPECS.items()}
    out[active_id] = active_exit
    return out
