from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.types import Action

Short = Annotated[str, StringConstraints(max_length=300)]


class AIDecision(BaseModel):
    """Strict schema. Unknown fields are rejected, so the model cannot smuggle in calldata/addresses."""

    model_config = ConfigDict(extra="forbid")
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = Field(max_length=1500)
    positive_signals: list[Short] = Field(default_factory=list, max_length=10)
    negative_signals: list[Short] = Field(default_factory=list, max_length=10)
    risk_flags: list[Short] = Field(default_factory=list, max_length=10)
    strategy_score: float = Field(ge=0.0, le=100.0)
    recommended_position_percent: float = Field(ge=0.0, le=100.0)
    # Dollar size the model wants, inside the min/max band it is shown in the prompt. 0 = not given (legacy replies,
    # models that ignore it): the pipeline then falls back to recommended_position_percent. Always clamped in code.
    recommended_order_usdc: float = Field(default=0.0, ge=0.0, le=1_000_000.0)


class AIOutcome(BaseModel):
    status: str  # OK | UNAVAILABLE | INVALID | DISABLED
    decision: AIDecision | None = None
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    raw: str = ""
    error: str = ""
