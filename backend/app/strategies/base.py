from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.market import MarketState


class StrategySignal(BaseModel):
    strategy_id: str
    strategy_version: int
    score: float
    qualified: bool
    components: dict[str, float | None]
    weights: dict[str, float]
    gates: dict[str, bool]
    reasons: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    config_snapshot: dict


class Strategy(ABC):
    strategy_id: str
    version: int

    @abstractmethod
    def score(self, market: MarketState, now: datetime) -> StrategySignal: ...

    @abstractmethod
    def config_snapshot(self) -> dict: ...
