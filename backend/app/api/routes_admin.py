"""Admin-only operational audit log: which provider failed, where, when, which AI answered, which source produced data.

Everything here requires ``require_admin`` (users.is_admin or ADMIN_EMAILS). Non-admins get a plain 404.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.db import models as M
from app.observability.audit import RECORDER
from app.services import audit_store

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("/audit")
async def audit_list(kind: str | None = None, status: str | None = None, severity: str | None = None,
                     provider: str | None = None, component: str | None = None, token: str | None = None,
                     user_id: str | None = None, decision_id: str | None = None, q: str | None = None,
                     since: str | None = None, until: str | None = None, before: str | None = None, hours: float | None = Query(None, ge=0.1, le=720),
                     limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    start = _dt(since) or (datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None)
    rows = await audit_store.query(db, kind=kind, status=status, severity=severity, provider=provider, component=component,
                                   token_key=token, user_id=user_id, decision_id=decision_id, text=q, since=start,
                                   until=_dt(until), before=_dt(before), limit=limit)
    items = [audit_store.row_out(r) for r in rows]
    return {"items": items, "next_before": items[-1]["at"] if len(items) >= limit else None}


@router.get("/audit/providers")
async def audit_providers(window_h: float = Query(24.0, ge=0.25, le=168), db: AsyncSession = Depends(get_db)):
    """Provider health board: who is DOWN right now, since when, and what the failure rate was in the window."""
    return {"window_h": window_h, "providers": await audit_store.provider_board(db, window_h=window_h)}


@router.get("/audit/summary")
async def audit_summary(window_h: float = Query(24.0, ge=0.25, le=168), db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=window_h)
    rows = (await db.execute(select(M.OpsAuditLog.kind, M.OpsAuditLog.status, func.count())
                             .where(M.OpsAuditLog.at >= since).group_by(M.OpsAuditLog.kind, M.OpsAuditLog.status))).all()
    out: dict[str, dict[str, int]] = {}
    for kind, status, n in rows:
        out.setdefault(kind, {})[status] = int(n)
    return {"window_h": window_h, "by_kind": out, "buffered_unsent": RECORDER.pending(), "dropped": RECORDER.dropped}


@router.get("/audit/decision/{decision_id}")
async def audit_for_decision(decision_id: str, db: AsyncSession = Depends(get_db)):
    """Everything recorded while one decision was being made: provider calls, AI attempts, agent tool calls, venue quotes."""
    rows = await audit_store.query(db, decision_id=decision_id, limit=500)
    return {"decision_id": decision_id, "items": [audit_store.row_out(r) for r in reversed(rows)]}
