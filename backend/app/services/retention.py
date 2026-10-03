"""Retention: nothing older than ``retention_hours`` is shown or kept (except what a position/order still needs).

Deletes, in dependency order:
  * strategy_signals / risk_assessments / ai_analyses of old decisions
  * events correlated to those decisions
  * the old decisions themselves
  * old market snapshots and stale global-registry tokens

A decision is KEPT if an order or position references it, so "why did the agent buy this?" keeps working
for anything that was actually traded.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.db import models as M

log = logging.getLogger("retention")


async def prune_old(sf, retention_hours: float = 24.0, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1.0, float(retention_hours)))
    out = {"decisions": 0, "events": 0, "snapshots": 0, "tokens": 0}
    async with sf() as db:
        traded = select(M.Order.decision_id).where(M.Order.decision_id != "").union(
            select(M.Position.decision_id).where(M.Position.decision_id.is_not(None))
        )
        old_ids = select(M.Decision.id).where(M.Decision.created_at < cutoff, M.Decision.id.not_in(traded))
        # children first (FKs), then events, then the decisions
        for model in (M.StrategySignalRow, M.RiskAssessmentRow, M.AIAnalysisRow):
            await db.execute(delete(model).where(model.decision_id.in_(old_ids)))
        r = await db.execute(delete(M.EventRow).where(M.EventRow.correlation_id.in_(old_ids)))
        out["events"] = int(r.rowcount or 0)
        r = await db.execute(delete(M.Decision).where(M.Decision.id.in_(old_ids)))
        out["decisions"] = int(r.rowcount or 0)
        r = await db.execute(delete(M.MarketSnapshot).where(M.MarketSnapshot.at < cutoff))
        out["snapshots"] = int(r.rowcount or 0)
        r = await db.execute(delete(M.LaunchpadTokenRow).where(
            (M.LaunchpadTokenRow.last_monitored_at < cutoff)
            | (M.LaunchpadTokenRow.last_monitored_at.is_(None) & (M.LaunchpadTokenRow.discovered_at < cutoff))
        ))
        out["tokens"] = int(r.rowcount or 0)
        await db.commit()
    return out


async def retention_loop(sf, retention_hours: float, every_s: float = 3600.0, first_delay_s: float = 90.0) -> None:
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            res = await prune_old(sf, retention_hours)
            if any(res.values()):
                log.info("retention pruned %s (older than %.0fh)", res, retention_hours)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention prune failed")
        await asyncio.sleep(every_s)
