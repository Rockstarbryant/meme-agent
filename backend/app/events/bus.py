from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class EventType(str, Enum):
    TOKEN_LAUNCHED = "TOKEN_LAUNCHED"
    TRADE_DETECTED = "TRADE_DETECTED"
    TOKEN_ACTIVITY_UPDATED = "TOKEN_ACTIVITY_UPDATED"
    RISK_ASSESSMENT_CREATED = "RISK_ASSESSMENT_CREATED"
    STRATEGY_SIGNAL_CREATED = "STRATEGY_SIGNAL_CREATED"
    AI_ANALYSIS_COMPLETED = "AI_ANALYSIS_COMPLETED"
    DECISION_RECORDED = "DECISION_RECORDED"
    BUY_APPROVED = "BUY_APPROVED"
    BUY_REJECTED = "BUY_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_FAILED = "ORDER_FAILED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    TAKE_PROFIT_TRIGGERED = "TAKE_PROFIT_TRIGGERED"
    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    TRAILING_STOP_TRIGGERED = "TRAILING_STOP_TRIGGERED"
    POSITION_CLOSED = "POSITION_CLOSED"
    EMERGENCY_STOP_CHANGED = "EMERGENCY_STOP_CHANGED"
    RISK_ALERT = "RISK_ALERT"
    MARKET_SNAPSHOT = "MARKET_SNAPSHOT"
    PORTFOLIO_SNAPSHOT = "PORTFOLIO_SNAPSHOT"
    AGENT_ERROR = "AGENT_ERROR"


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: EventType
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""  # decision id / position id, so history can be reconstructed
    payload: dict = Field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


class AuditSink(ABC):
    @abstractmethod
    async def write(self, event: Event) -> None: ...


class InMemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    def for_correlation(self, cid: str) -> list[Event]:
        return [e for e in self.events if e.correlation_id == cid]

    def of_type(self, t: EventType) -> list[Event]:
        return [e for e in self.events if e.type == t]


class EventBus:
    """Every event is written to the audit sink FIRST, then fanned out; a bad subscriber cannot lose audit data."""

    def __init__(self, sinks: list[AuditSink] | None = None):
        self.sinks = sinks or []
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, type_: EventType, correlation_id: str = "", **payload) -> Event:
        ev = Event(type=type_, correlation_id=correlation_id, payload=payload)
        for s in self.sinks:
            try:
                await s.write(ev)
            except Exception:  # noqa: BLE001
                log.exception("audit sink failed")
        await asyncio.gather(*(self._safe(h, ev) for h in self._handlers))
        return ev

    @staticmethod
    async def _safe(h: Handler, ev: Event) -> None:
        try:
            await h(ev)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")


class IdempotencyStore(ABC):
    @abstractmethod
    async def claim(self, key: str) -> bool: ...
    @abstractmethod
    async def release(self, key: str) -> None: ...
    @abstractmethod
    async def complete(self, key: str, result: dict) -> None: ...
    @abstractmethod
    async def get(self, key: str) -> dict | None: ...


class InMemoryIdempotencyStore(IdempotencyStore):
    """Redis/DB-backed store implements the same atomic claim (SET NX / unique constraint)."""

    def __init__(self) -> None:
        self._claimed: dict[str, dict | None] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str) -> bool:
        async with self._lock:
            if key in self._claimed:
                return False
            self._claimed[key] = None
            return True

    async def release(self, key: str) -> None:
        async with self._lock:
            self._claimed.pop(key, None)

    async def complete(self, key: str, result: dict) -> None:
        self._claimed[key] = result

    async def get(self, key: str) -> dict | None:
        return self._claimed.get(key)
