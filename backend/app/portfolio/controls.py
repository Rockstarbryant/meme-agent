from __future__ import annotations

from pydantic import BaseModel, Field


class ControlState(BaseModel):
    """Operator-level switches. Only entries are blocked; position protection always continues."""

    emergency_stop: bool = False
    global_pause: bool = False
    paused_strategies: set[str] = Field(default_factory=set)
    paused_chains: set[str] = Field(default_factory=set)
    blacklisted_tokens: set[str] = Field(default_factory=set)
    blacklisted_creators: set[str] = Field(default_factory=set)
    blacklisted_launchpads: set[str] = Field(default_factory=set)

    def snapshot(self) -> dict:
        return {
            "emergency_stop": self.emergency_stop,
            "global_pause": self.global_pause,
            "paused_strategies": sorted(self.paused_strategies),
            "paused_chains": sorted(self.paused_chains),
            "blacklisted_tokens": len(self.blacklisted_tokens),
            "blacklisted_creators": len(self.blacklisted_creators),
            "blacklisted_launchpads": sorted(self.blacklisted_launchpads),
        }
