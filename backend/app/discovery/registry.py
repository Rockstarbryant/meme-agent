"""Persistent global token registry. Unique identity: (chain, token_address)."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

class TokenStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    SCREENING = "SCREENING"
    WATCHING = "WATCHING"
    IMPROVING = "IMPROVING"
    QUALIFIED = "QUALIFIED"
    SIGNAL = "SIGNAL"
    EXECUTING = "EXECUTING"
    REJECTED = "REJECTED"
    DROPPED = "DROPPED"
    LOST_MOMENTUM = "LOST_MOMENTUM"
    EXPIRED = "EXPIRED"

class LaunchpadToken(BaseModel):
    id: str | None = None
    chain: str
    token_address: str
    launchpad: str | None = None
    launchpad_contract: str | None = None
    launch_event: str | None = None
    launch_tx_hash: str | None = None
    creator_address: str | None = None
    launched_at: datetime | None = None
    discovered_at: datetime
    status: TokenStatus = TokenStatus.DISCOVERED
    symbol: str | None = None
    name: str | None = None
    initial_score: float | None = None
    current_score: float | None = None
    score_delta: float | None = None
    priority: str = "WARM"
    last_monitored_at: datetime | None = None
    last_score_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    @property
    def token_key(self) -> str:
        return f"{self.chain}:{self.token_address.lower()}"

class GlobalTokenRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, LaunchpadToken] = {}
    def get(self, chain: str, address: str) -> LaunchpadToken | None:
        return self._by_key.get(f"{chain}:{address.lower()}")
    def upsert(self, token: LaunchpadToken) -> LaunchpadToken:
        key = token.token_key
        existing = self._by_key.get(key)
        if existing is token:
            # Same object already live in the registry (e.g. monitoring mutated it in place before calling
            # upsert). Re-deriving score_delta etc. here would double-apply the update the caller already made
            # and silently zero it out (current_score - current_score). Nothing more to merge.
            self._by_key[key] = token
            return token
        if existing:
            for field in ("launchpad", "launchpad_contract", "launch_event", "launch_tx_hash", "creator_address", "launched_at", "symbol", "name"):
                val = getattr(token, field)
                if val is not None and getattr(existing, field) is None:
                    setattr(existing, field, val)
            if token.current_score is not None:
                existing.score_delta = (token.current_score - (existing.current_score or existing.initial_score or 0.0)) if (existing.current_score is not None or existing.initial_score is not None) else None
                existing.current_score = token.current_score
                existing.last_score_at = token.last_score_at or existing.last_score_at
            if token.status != TokenStatus.DISCOVERED:
                existing.status = token.status
            if token.priority:
                existing.priority = token.priority
            existing.meta.update(token.meta)
            return existing
        self._by_key[key] = token
        return token
    def list_by_status(self, *statuses: TokenStatus) -> list[LaunchpadToken]:
        sset = set(statuses)
        return [t for t in self._by_key.values() if t.status in sset]
    def list_recent(self, since: datetime) -> list[LaunchpadToken]:
        return [t for t in self._by_key.values() if t.discovered_at >= since or (t.launched_at and t.launched_at >= since)]
    def all(self) -> list[LaunchpadToken]:
        return list(self._by_key.values())
    def remove(self, key: str) -> LaunchpadToken | None:
        """Drop a token from the in-memory registry (used by retention pruning)."""
        return self._by_key.pop(key, None)
