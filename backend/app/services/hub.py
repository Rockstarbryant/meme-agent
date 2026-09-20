from __future__ import annotations

import asyncio
from collections import defaultdict

from app.events.bus import Event


class EventHub:
    """Fan-out of runtime events to connected SSE clients, per user. Slow clients drop old events, never block trading."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[user_id].add(q)
        return q

    def unsubscribe(self, user_id: str, q: asyncio.Queue) -> None:
        self._subs[user_id].discard(q)

    def publish(self, user_id: str, ev: dict) -> None:
        for q in list(self._subs.get(user_id, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)

    def handler_for(self, user_id: str):
        async def _h(ev: Event) -> None:
            p = {k: v for k, v in ev.payload.items() if k != "decision"}  # full decision is fetched via REST
            self.publish(user_id, {"id": ev.id, "type": ev.type.value, "at": ev.at.isoformat(),
                                   "correlation_id": ev.correlation_id, "payload": p})
        return _h
