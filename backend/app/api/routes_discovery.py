"""Historical discovery, token search, bookmarks — read from global registry.

Opening these endpoints MUST NOT trigger a new launchpad scan.
Monitoring (not discovery) is what keeps price/mcap/liquidity/holders fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_db
from app.core.clock import utcnow
from app.db import models as M
from app.db.base import uid

router = APIRouter(prefix="/discovery", tags=["discovery"])


class TokenCard(BaseModel):
    chain: str
    token_address: str
    symbol: str | None = None
    name: str | None = None
    launchpad: str | None = None
    launched_at: datetime | None = None
    discovered_at: datetime | None = None
    age_seconds: float | None = None
    market_cap: float | None = None
    liquidity: float | None = None
    volume: float | None = None
    holders: int | None = None
    score: float | None = None
    score_delta: float | None = None
    status: str | None = None
    priority: str | None = None
    price: float | None = None
    performance_since_launch: float | None = None
    performance_since_discovery: float | None = None
    global_screening_passed: bool = False
    user_strategy_passed: bool | None = None
    last_monitored_at: datetime | None = None


class HistoricalResponse(BaseModel):
    window: str
    tokens: list[TokenCard]
    count: int


class BookmarkIn(BaseModel):
    chain: str = "arc"
    token_address: str
    note: str | None = None


class BookmarkOut(BaseModel):
    id: str
    chain: str
    token_address: str
    note: str | None = None
    created_at: datetime
    token: TokenCard | None = None


def _row_to_card(r: M.LaunchpadTokenRow, now: datetime) -> TokenCard:
    snap = r.last_snapshot or {}
    launched = r.launched_at
    age = (now - launched).total_seconds() if launched else None
    return TokenCard(
        chain=r.chain,
        token_address=r.token_address,
        symbol=r.symbol,
        name=r.name,
        launchpad=r.launchpad,
        launched_at=r.launched_at,
        discovered_at=r.discovered_at,
        age_seconds=age,
        market_cap=snap.get("market_cap"),
        liquidity=snap.get("liquidity"),
        volume=snap.get("volume_5m") or snap.get("volume_15m"),
        holders=snap.get("holder_count"),
        score=r.current_score,
        score_delta=r.score_delta,
        status=r.status,
        priority=r.priority,
        price=snap.get("price"),
        global_screening_passed=r.status in ("WATCHING", "IMPROVING", "QUALIFIED", "SIGNAL", "SCREENING"),
        last_monitored_at=r.last_monitored_at,
    )


@router.get("/historical/24h", response_model=HistoricalResponse)
async def historical_24h(
    launchpad: str | None = None,
    min_score: float | None = None,
    status: str | None = None,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _historical(db, hours=24, launchpad=launchpad, min_score=min_score, status=status)


@router.get("/historical/72h", response_model=HistoricalResponse)
async def historical_72h(
    launchpad: str | None = None,
    min_score: float | None = None,
    status: str | None = None,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _historical(db, hours=72, launchpad=launchpad, min_score=min_score, status=status)


async def _historical(
    db: AsyncSession,
    hours: int,
    launchpad: str | None,
    min_score: float | None,
    status: str | None,
) -> HistoricalResponse:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    tokens: list[TokenCard] = []
    q = (
        select(M.LaunchpadTokenRow)
        .where(M.LaunchpadTokenRow.discovered_at >= since)
        .order_by(M.LaunchpadTokenRow.discovered_at.desc())
        .limit(300)
    )
    rows = (await db.execute(q)).scalars().all()
    for r in rows:
        if launchpad and r.launchpad != launchpad:
            continue
        if status and r.status != status:
            continue
        if min_score is not None and (r.current_score or 0) < min_score:
            continue
        tokens.append(_row_to_card(r, now))
    return HistoricalResponse(window=f"{hours}h", tokens=tokens, count=len(tokens))


@router.get("/search")
async def search_tokens(
    q: str = Query(..., min_length=1),
    launchpad: str | None = None,
    min_score: float | None = None,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search global registry. Does NOT trigger launchpad discovery."""
    q_lower = q.lower().strip()
    results = []
    rows = (
        await db.execute(
            select(M.LaunchpadTokenRow).order_by(M.LaunchpadTokenRow.discovered_at.desc()).limit(500)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        hay = " ".join(filter(None, [r.token_address, r.symbol, r.name, r.launchpad])).lower()
        if q_lower not in hay and not r.token_address.lower().startswith(q_lower):
            continue
        if launchpad and r.launchpad != launchpad:
            continue
        if min_score is not None and (r.current_score or 0) < min_score:
            continue
        results.append(_row_to_card(r, now).model_dump(mode="json"))
        if len(results) >= 50:
            break
    return {"query": q, "results": results, "count": len(results)}


@router.get("/tokens/{chain}/{address}")
async def token_detail(
    chain: str,
    address: str,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Token detail with latest snapshot (updated by monitoring, not frozen at discovery)."""
    row = (
        await db.execute(
            select(M.LaunchpadTokenRow).where(
                M.LaunchpadTokenRow.chain == chain,
                M.LaunchpadTokenRow.token_address == address.lower(),
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "token not in global registry")
    card = _row_to_card(row, datetime.now(timezone.utc))
    snaps = (
        await db.execute(
            select(M.MarketSnapshot)
            .where(M.MarketSnapshot.token_key == f"{chain}:{address.lower()}")
            .order_by(M.MarketSnapshot.at.desc())
            .limit(48)
        )
    ).scalars().all()
    history = [{"at": s.at.isoformat(), "data": s.data} for s in reversed(list(snaps))]
    return {"token": card.model_dump(mode="json"), "history": history}


@router.get("/tokens/{chain}/{address}/candles")
async def token_candles(
    chain: str,
    address: str,
    request: Request,
    tf: str = Query("15m", pattern="^(1m|5m|15m|1h|4h|1d)$"),
    limit: int = Query(200, ge=20, le=500),
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """OHLCV candles for the token page. ``provider`` says who produced them (codex | geckoterminal | snapshots)."""
    from app.services.charts import ChartService
    row = (await db.execute(select(M.LaunchpadTokenRow).where(
        M.LaunchpadTokenRow.chain == chain, M.LaunchpadTokenRow.token_address == address.lower()))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "token not in global registry")
    snaps = (await db.execute(select(M.MarketSnapshot).where(M.MarketSnapshot.token_key == f"{chain}:{address.lower()}")
                              .order_by(M.MarketSnapshot.at.desc()).limit(500))).scalars().all()
    history = [{"at": s.at.isoformat(), "data": s.data} for s in reversed(list(snaps))]
    last = (row.last_snapshot or {}) if isinstance(row.last_snapshot, dict) else {}
    pool = last.get("pool_address") or last.get("pool_id") or (history[-1]["data"].get("pool_address") if history else None) \
        or (history[-1]["data"].get("pool_id") if history else None)
    svc = getattr(request.app.state, "chart_service", None)
    if svc is None:
        from app.integrations.codex import CodexClient
        from app.api.deps import C
        st = C(request).settings
        key = st.codex_api_key.get_secret_value() if st.codex_api_key else ""
        svc = ChartService(st, codex_client=CodexClient(key, url=st.codex_url) if key else None)
        request.app.state.chart_service = svc
    out = await svc.candles(address.lower(), pool, tf, limit, history)
    return {**out, "token": address.lower(), "pool": pool}


@router.get("/status")
async def scanner_status(
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    premium = bool(getattr(user, "premium_scanner", False))
    cp = (
        await db.execute(select(M.DiscoveryCheckpointRow).where(M.DiscoveryCheckpointRow.name == "global"))
    ).scalar_one_or_none()
    active = (
        await db.execute(
            select(M.LaunchpadTokenRow).where(
                M.LaunchpadTokenRow.status.in_(["WATCHING", "IMPROVING", "QUALIFIED", "SCREENING"])
            )
        )
    ).scalars().all()
    return {
        "premium_scanner_enabled": premium,
        "discovery_interval_hours": 3,
        "discovery_is_global": True,
        "discovery_runs_on_trade_cycle": False,
        "last_success_at": cp.last_success_at.isoformat() if cp and cp.last_success_at else None,
        "last_attempt_at": cp.last_attempt_at.isoformat() if cp and cp.last_attempt_at else None,
        "tokens_found_last_run": cp.tokens_found if cp else 0,
        "active_candidates": len(active),
        "message": (
            "Global discovery runs on its own schedule. Opening this page does not trigger a scan. "
            "Market data (price, mcap, liquidity, holders) is refreshed by the monitoring job."
            if premium else
            "Premium scanner is off. You can still search, bookmark, and view historical tokens. "
            "Automatic global discovery requires premium."
        ),
    }


@router.post("/premium")
async def set_premium(
    enabled: bool = True,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle premium scanner for the current user (admin/billing integration point)."""
    u = await db.get(M.User, user.id)
    if not u:
        raise HTTPException(404, "user not found")
    u.premium_scanner = bool(enabled)
    await db.commit()
    return {"premium_scanner_enabled": bool(enabled)}


@router.get("/bookmarks", response_model=list[BookmarkOut])
async def list_bookmarks(
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    out: list[BookmarkOut] = []
    bms = (
        await db.execute(
            select(M.TokenBookmark).where(M.TokenBookmark.user_id == user.id).order_by(M.TokenBookmark.created_at.desc())
        )
    ).scalars().all()
    for b in bms:
        row = (
            await db.execute(
                select(M.LaunchpadTokenRow).where(
                    M.LaunchpadTokenRow.chain == b.chain,
                    M.LaunchpadTokenRow.token_address == b.token_address.lower(),
                )
            )
        ).scalar_one_or_none()
        card = _row_to_card(row, now) if row else None
        out.append(BookmarkOut(
            id=b.id, chain=b.chain, token_address=b.token_address,
            note=b.note, created_at=b.created_at, token=card,
        ))
    return out


@router.post("/bookmarks", response_model=BookmarkOut)
async def add_bookmark(
    body: BookmarkIn,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    addr = body.token_address.lower()
    existing = (
        await db.execute(
            select(M.TokenBookmark).where(
                M.TokenBookmark.user_id == user.id,
                M.TokenBookmark.chain == body.chain,
                M.TokenBookmark.token_address == addr,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "already bookmarked")
    bm = M.TokenBookmark(
        id=uid(), user_id=user.id, chain=body.chain, token_address=addr,
        note=body.note, created_at=utcnow(),
    )
    db.add(bm)
    await db.commit()
    row = (
        await db.execute(
            select(M.LaunchpadTokenRow).where(
                M.LaunchpadTokenRow.chain == body.chain,
                M.LaunchpadTokenRow.token_address == addr,
            )
        )
    ).scalar_one_or_none()
    card = _row_to_card(row, datetime.now(timezone.utc)) if row else None
    return BookmarkOut(
        id=bm.id, chain=bm.chain, token_address=bm.token_address,
        note=bm.note, created_at=bm.created_at, token=card,
    )


@router.delete("/bookmarks/{chain}/{address}")
async def remove_bookmark(
    chain: str,
    address: str,
    user: M.User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(M.TokenBookmark).where(
            M.TokenBookmark.user_id == user.id,
            M.TokenBookmark.chain == chain,
            M.TokenBookmark.token_address == address.lower(),
        )
    )
    await db.commit()
    return {"ok": True}