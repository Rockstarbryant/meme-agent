"""Ingest events reported by a user's Local Runner.

Guarantees: replay-safe (per-runner monotonic sequence), one bad event never blocks the outbox (poison events are
skipped), a runner can only ever write rows for ITS OWN user, and payloads are size-limited.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as M
from app.db import repo
from app.domain.runner_protocol import EventAck, EventBatch, RunnerEvent
from app.events.bus import EventType as E
from app.services.hub import EventHub

log = logging.getLogger(__name__)
MAX_EVENT_BYTES = 262_144
NOT_STORED = {E.POSITION_UPDATED, E.TOKEN_ACTIVITY_UPDATED, E.MARKET_SNAPSHOT, E.PORTFOLIO_SNAPSHOT}
# Only user-relevant events go to the live UI stream (the rest are stored, not pushed): a runner burst must not flood browsers.
STREAMED = {E.DECISION_RECORDED, E.BUY_APPROVED, E.BUY_REJECTED, E.ORDER_SUBMITTED, E.ORDER_FILLED, E.ORDER_FAILED, E.POSITION_OPENED,
            E.POSITION_UPDATED, E.POSITION_CLOSED, E.TAKE_PROFIT_TRIGGERED, E.STOP_LOSS_TRIGGERED, E.TRAILING_STOP_TRIGGERED,
            E.EMERGENCY_STOP_CHANGED, E.RISK_ALERT, E.AGENT_ERROR}
HEAVY = {"decision", "market", "position", "portfolio"}


async def _handle(db: AsyncSession, user_id: str, ev: RunnerEvent) -> bool:
    try:
        t = E(ev.type)
    except ValueError:
        return False
    if len(json.dumps(ev.payload, default=str)) > MAX_EVENT_BYTES:
        return False
    p = ev.payload
    if t not in NOT_STORED:
        if await db.get(M.EventRow, ev.id) is not None:
            return False
        db.add(M.EventRow(id=ev.id, user_id=user_id, type=t.value, at=ev.at, correlation_id=ev.correlation_id, payload=p))
    if t == E.DECISION_RECORDED:
        await repo.save_decision(db, user_id, p["decision"])
    elif t == E.MARKET_SNAPSHOT:
        m = p["market"]
        db.add(M.MarketSnapshot(token_key=f"{m['chain']}:{m['token_address'].lower()}", is_demo=bool(m.get("is_demo")), data=m, at=ev.at))
    elif t == E.PORTFOLIO_SNAPSHOT:
        pid = await repo.save_portfolio_dict(db, user_id, p["portfolio"], ev.at)
        s = p["summary"]
        db.add(M.PortfolioSnapshot(portfolio_id=pid, at=ev.at, cash_usdc=s["cash_usdc"], exposure_usdc=s["exposure_usdc"],
                                   total_value_usdc=s["total_value_usdc"], daily_pnl_usdc=s["daily_pnl_usdc"], open_positions=s["open_positions"]))
    else:
        if t in (E.ORDER_FILLED, E.ORDER_FAILED) and "order" in p:
            if not await repo.save_order(db, user_id, p["order"], decision_id=ev.correlation_id):
                return False
        if "position" in p and not await repo.upsert_position_dict(db, user_id, p["position"]):
            return False
        if "portfolio" in p:
            await repo.save_portfolio_dict(db, user_id, p["portfolio"], ev.at)
    return True


async def ingest(db: AsyncSession, hub: EventHub, runner: M.Runner, batch: EventBatch) -> EventAck:
    accepted = skipped = 0
    last = runner.last_seq
    streamed: list[RunnerEvent] = []
    for ev in sorted(batch.events, key=lambda e: e.seq):
        if ev.seq <= last:
            skipped += 1  # replay of something already ingested
            continue
        try:
            async with db.begin_nested():
                ok = await _handle(db, runner.user_id, ev)
        except Exception:  # noqa: BLE001 - a poison event must not wedge the runner's outbox
            log.exception("skipping unprocessable runner event %s", ev.id)
            ok = False
        if ok:
            accepted += 1
            if E(ev.type) in STREAMED:
                streamed.append(ev)
        else:
            skipped += 1
        last = ev.seq
    runner.last_seq = last
    runner.last_seen_at = datetime.now(timezone.utc)
    db.add(runner)
    await db.commit()
    for ev in streamed:
        hub.publish(runner.user_id, {"id": ev.id, "type": ev.type, "at": ev.at.isoformat(), "correlation_id": ev.correlation_id,
                                     "payload": {k: v for k, v in ev.payload.items() if k not in HEAVY}})
    return EventAck(last_seq=last, accepted=accepted, skipped=skipped)
