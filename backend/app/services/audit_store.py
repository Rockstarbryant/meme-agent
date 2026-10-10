"""Persistence + queries for the operational audit trail (``ops_audit_log``). Admin-only surface; see routes_admin."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.db import models as M
from app.observability.audit import AuditKind, AuditRecord, AuditSeverity, AuditStatus, scrub, scrub_text

log = logging.getLogger("audit.store")

_KINDS = {k.value for k in AuditKind}
_STATUSES = {k.value for k in AuditStatus}
_SEV = {k.value for k in AuditSeverity}


def row_from_record(r: AuditRecord, *, user_id: str | None = None) -> M.OpsAuditLog:
    return M.OpsAuditLog(
        id=r.id[:32], at=r.at, kind=r.kind.value, status=r.status.value, severity=r.severity.value, component=r.component[:64],
        provider=r.provider[:64], model=r.model[:96], operation=r.operation[:48], latency_ms=r.latency_ms, error=r.error[:2000],
        token_key=r.token_key[:96], user_id=(user_id if user_id is not None else r.user_id)[:32], decision_id=r.decision_id[:64],
        repeat=max(1, int(r.repeat)), detail=scrub(r.detail))


def rows_from_runner_payload(user_id: str, payload: dict[str, Any]) -> list[M.OpsAuditLog]:
    """A runner may only write audit rows for ITS OWN user; everything is re-validated and re-scrubbed server side."""
    out: list[M.OpsAuditLog] = []
    for raw in list(payload.get("records") or [])[:100]:
        try:
            rec = AuditRecord.model_validate(raw)
        except Exception:  # noqa: BLE001 - one bad record never blocks the batch
            continue
        rec.error = scrub_text(rec.error)
        out.append(row_from_record(rec, user_id=user_id))
    return out


def make_db_sink(sf):
    async def sink(batch: list[AuditRecord]) -> None:
        async with sf() as db:
            for r in batch:
                if await db.get(M.OpsAuditLog, r.id[:32]) is None:
                    db.add(row_from_record(r))
            await db.commit()
    return sink


async def prune(sf, days: float = 14.0) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1.0, days))
    async with sf() as db:
        r = await db.execute(delete(M.OpsAuditLog).where(M.OpsAuditLog.at < cutoff))
        await db.commit()
        return int(r.rowcount or 0)


def row_out(r: M.OpsAuditLog) -> dict:
    return {"id": r.id, "at": r.at.isoformat(), "kind": r.kind, "status": r.status, "severity": r.severity,
            "component": r.component, "provider": r.provider, "model": r.model, "operation": r.operation,
            "latency_ms": r.latency_ms, "error": r.error, "token_key": r.token_key, "user_id": r.user_id,
            "decision_id": r.decision_id, "repeat": r.repeat, "detail": r.detail}


async def query(db, *, kind: str | None = None, status: str | None = None, severity: str | None = None,
                provider: str | None = None, component: str | None = None, token_key: str | None = None,
                user_id: str | None = None, decision_id: str | None = None, since: datetime | None = None,
                until: datetime | None = None, text: str | None = None, before: datetime | None = None,
                limit: int = 100) -> list[M.OpsAuditLog]:
    q = select(M.OpsAuditLog)
    if kind and kind.upper() in _KINDS:
        q = q.where(M.OpsAuditLog.kind == kind.upper())
    if status and status.upper() in _STATUSES:
        q = q.where(M.OpsAuditLog.status == status.upper())
    if severity and severity.upper() in _SEV:
        q = q.where(M.OpsAuditLog.severity == severity.upper())
    if provider:
        q = q.where(M.OpsAuditLog.provider == provider.lower())
    if component:
        q = q.where(M.OpsAuditLog.component == component)
    if token_key:
        q = q.where(M.OpsAuditLog.token_key == token_key.lower())
    if user_id:
        q = q.where(M.OpsAuditLog.user_id == user_id)
    if decision_id:
        q = q.where(M.OpsAuditLog.decision_id == decision_id)
    if since:
        q = q.where(M.OpsAuditLog.at >= since)
    if until:
        q = q.where(M.OpsAuditLog.at <= until)
    if before:
        q = q.where(M.OpsAuditLog.at < before)
    if text:
        q = q.where(M.OpsAuditLog.error.ilike(f"%{text[:80]}%"))
    return list((await db.execute(q.order_by(M.OpsAuditLog.at.desc()).limit(max(1, min(limit, 500))))).scalars().all())


async def provider_board(db, *, window_h: float = 24.0) -> list[dict]:
    """Per (kind, provider): counts in the window, last OK / last failure and the latest state, so an operator sees at
    a glance WHICH provider is down right now and since when."""
    since = datetime.now(timezone.utc) - timedelta(hours=window_h)
    rows = (await db.execute(
        select(M.OpsAuditLog.kind, M.OpsAuditLog.provider, M.OpsAuditLog.status, func.count(), func.sum(M.OpsAuditLog.repeat),
               func.max(M.OpsAuditLog.at), func.avg(M.OpsAuditLog.latency_ms))
        .where(M.OpsAuditLog.at >= since, M.OpsAuditLog.provider != "")
        .group_by(M.OpsAuditLog.kind, M.OpsAuditLog.provider, M.OpsAuditLog.status))).all()
    board: dict[tuple[str, str], dict] = {}
    for kind, prov, status, n, rep, last, lat in rows:
        b = board.setdefault((kind, prov), {"kind": kind, "provider": prov, "ok": 0, "failed": 0, "skipped": 0, "degraded": 0,
                                            "last_ok_at": None, "last_failure_at": None, "avg_latency_ms": None})
        events = int(rep or n)
        if status in ("OK", "RECOVERED"):
            b["ok"] += events
            if b["last_ok_at"] is None or last > b["last_ok_at"]:
                b["last_ok_at"] = last
            if lat is not None:
                b["avg_latency_ms"] = round(float(lat), 1)
        elif status == "FAILED":
            b["failed"] += events
            if b["last_failure_at"] is None or last > b["last_failure_at"]:
                b["last_failure_at"] = last
        elif status == "SKIPPED":
            b["skipped"] += events
        elif status == "DEGRADED":
            b["degraded"] += events
    out = []
    for b in board.values():
        lo, lf = b["last_ok_at"], b["last_failure_at"]
        # "down" = the most recent thing we saw from it was a failure/skip, not a success
        b["state"] = "DOWN" if lf is not None and (lo is None or lf > lo) else ("DEGRADED" if b["failed"] or b["degraded"] else "OK")
        b["last_ok_at"] = lo.isoformat() if lo else None
        b["last_failure_at"] = lf.isoformat() if lf else None
        out.append(b)
    order = {"DOWN": 0, "DEGRADED": 1, "OK": 2}
    return sorted(out, key=lambda b: (order[b["state"]], b["kind"], b["provider"]))
