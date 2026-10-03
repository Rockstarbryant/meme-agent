"""Durable store for the global token registry: Postgres + Redis cache.

Monitoring writes update last_snapshot so price/mcap/liquidity/holders are
never frozen at discovery time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.db import models as M
from app.db.base import uid
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus

log = logging.getLogger("discovery.store")


async def _with_db_retry(coro_factory, *, attempts: int = 2, label: str = "db"):
    """Retry once on dropped asyncpg connections (common after idle / pool recycle)."""
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return await coro_factory()
        except Exception as e:
            last = e
            name = type(e).__name__
            msg = str(e).lower()
            transient = (
                "connectiondoesnotexist" in name.lower()
                or "connection was closed" in msg
                or "connectiondoesnotexisterror" in msg
                or "server closed the connection" in msg
            )
            if not transient or i + 1 >= attempts:
                raise
            log.warning("%s transient DB error (%s); retrying", label, name)
            await asyncio.sleep(0.3 * (i + 1))
    raise last  # pragma: no cover



REDIS_TOKEN_PREFIX = "gtoken:"
REDIS_SNAPSHOT_PREFIX = "gsnap:"
REDIS_TOKEN_TTL = 6 * 3600
REDIS_SNAPSHOT_TTL = 120  # hot market data short TTL; monitoring refreshes


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def token_to_dict(t: LaunchpadToken) -> dict:
    return {
        "id": t.id,
        "chain": t.chain,
        "token_address": t.token_address,
        "launchpad": t.launchpad,
        "launchpad_contract": t.launchpad_contract,
        "launch_event": t.launch_event,
        "launch_tx_hash": t.launch_tx_hash,
        "creator_address": t.creator_address,
        "launched_at": _dt(t.launched_at),
        "discovered_at": _dt(t.discovered_at) or utcnow().isoformat(),
        "status": t.status.value if isinstance(t.status, TokenStatus) else str(t.status),
        "symbol": t.symbol,
        "name": t.name,
        "initial_score": t.initial_score,
        "current_score": t.current_score,
        "score_delta": t.score_delta,
        "priority": t.priority,
        "last_monitored_at": _dt(t.last_monitored_at),
        "last_score_at": _dt(t.last_score_at),
        "meta": t.meta or {},
        "last_snapshot": (t.meta or {}).get("last_snapshot"),
    }


def _parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def dict_to_token(d: dict) -> LaunchpadToken:
    status = d.get("status") or "DISCOVERED"
    try:
        st = TokenStatus(status)
    except Exception:
        st = TokenStatus.DISCOVERED
    meta = dict(d.get("meta") or {})
    if d.get("last_snapshot") and "last_snapshot" not in meta:
        meta["last_snapshot"] = d["last_snapshot"]
    return LaunchpadToken(
        id=d.get("id"),
        chain=d["chain"],
        token_address=d["token_address"],
        launchpad=d.get("launchpad"),
        launchpad_contract=d.get("launchpad_contract"),
        launch_event=d.get("launch_event"),
        launch_tx_hash=d.get("launch_tx_hash"),
        creator_address=d.get("creator_address"),
        launched_at=_parse_dt(d.get("launched_at")),
        discovered_at=_parse_dt(d.get("discovered_at")) or utcnow(),
        status=st,
        symbol=d.get("symbol"),
        name=d.get("name"),
        initial_score=d.get("initial_score"),
        current_score=d.get("current_score"),
        score_delta=d.get("score_delta"),
        priority=d.get("priority") or "WARM",
        last_monitored_at=_parse_dt(d.get("last_monitored_at")),
        last_score_at=_parse_dt(d.get("last_score_at")),
        meta=meta,
    )


class PersistentTokenStore:
    """Load/save LaunchpadToken to Postgres and mirror hot state in Redis."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis: Any = None,
    ) -> None:
        self.sf = session_factory
        self.redis = redis

    async def upsert(self, token: LaunchpadToken) -> LaunchpadToken:
        """Write-through: memory caller still owns registry; we persist."""
        snap = (token.meta or {}).get("last_snapshot")
        if self.sf is not None:
            async def _write():
                async with self.sf() as db:
                    row = (
                        await db.execute(
                            select(M.LaunchpadTokenRow).where(
                                M.LaunchpadTokenRow.chain == token.chain,
                                M.LaunchpadTokenRow.token_address == token.token_address.lower(),
                            )
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        row = M.LaunchpadTokenRow(
                            id=token.id or uid(),
                            chain=token.chain,
                            token_address=token.token_address.lower(),
                            discovered_at=token.discovered_at or utcnow(),
                        )
                        db.add(row)
                        token.id = row.id
                    else:
                        token.id = row.id
                    row.launchpad = token.launchpad
                    row.launchpad_contract = token.launchpad_contract
                    row.launch_event = token.launch_event
                    row.launch_tx_hash = token.launch_tx_hash
                    row.creator_address = token.creator_address
                    row.launched_at = token.launched_at
                    row.status = token.status.value if isinstance(token.status, TokenStatus) else str(token.status)
                    row.symbol = token.symbol
                    row.name = token.name
                    row.initial_score = token.initial_score
                    row.current_score = token.current_score
                    row.score_delta = token.score_delta
                    row.priority = token.priority
                    row.last_monitored_at = token.last_monitored_at
                    row.last_score_at = token.last_score_at
                    row.meta = token.meta or {}
                    row.last_snapshot = snap
                    await db.commit()

            try:
                await _with_db_retry(_write, attempts=2, label=f"upsert:{token.token_key}")
            except Exception:
                log.exception("persist launchpad_token failed for %s", token.token_key)

        # Always try Redis mirror
        await self._redis_set_token(token)
        if snap is not None:
            await self._redis_set_snapshot(token.chain, token.token_address, snap)
        return token

    async def write_market_snapshot(self, token: LaunchpadToken, snapshot: dict, *, is_demo: bool = False) -> None:
        """Append historical MarketSnapshot and refresh Redis + row.last_snapshot."""
        token.meta = dict(token.meta or {})
        token.meta["last_snapshot"] = snapshot
        token.last_monitored_at = utcnow()
        await self.upsert(token)
        if self.sf is None:
            return
        try:
            async with self.sf() as db:
                db.add(M.MarketSnapshot(
                    id=uid(),
                    token_key=token.token_key,
                    at=utcnow(),
                    is_demo=is_demo,
                    data=snapshot,
                ))
                # Also ensure Token row exists for control-plane compatibility
                existing = (
                    await db.execute(
                        select(M.Token).where(
                            M.Token.chain_id == token.chain,
                            M.Token.address == token.token_address.lower(),
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(M.Token(
                        id=uid(),
                        chain_id=token.chain,
                        address=token.token_address.lower(),
                        symbol=token.symbol,
                        creator_address=token.creator_address,
                        launchpad=token.launchpad,
                        first_seen_at=token.discovered_at or utcnow(),
                    ))
                else:
                    if token.symbol and not existing.symbol:
                        existing.symbol = token.symbol
                    if token.launchpad and not existing.launchpad:
                        existing.launchpad = token.launchpad
                await db.commit()
        except Exception:
            log.exception("write_market_snapshot failed for %s", token.token_key)

    async def load_all_active(self, limit: int = 2000) -> list[LaunchpadToken]:
        if self.sf is None:
            return []
        try:
            async with self.sf() as db:
                rows = (
                    await db.execute(
                        select(M.LaunchpadTokenRow)
                        .where(M.LaunchpadTokenRow.status.notin_(["EXPIRED", "DROPPED", "REJECTED"]))
                        .order_by(M.LaunchpadTokenRow.discovered_at.desc())
                        .limit(limit)
                    )
                ).scalars().all()
                out = []
                for r in rows:
                    d = {
                        "id": r.id, "chain": r.chain, "token_address": r.token_address,
                        "launchpad": r.launchpad, "launchpad_contract": r.launchpad_contract,
                        "launch_event": r.launch_event, "launch_tx_hash": r.launch_tx_hash,
                        "creator_address": r.creator_address, "launched_at": r.launched_at,
                        "discovered_at": r.discovered_at, "status": r.status,
                        "symbol": r.symbol, "name": r.name,
                        "initial_score": r.initial_score, "current_score": r.current_score,
                        "score_delta": r.score_delta, "priority": r.priority,
                        "last_monitored_at": r.last_monitored_at, "last_score_at": r.last_score_at,
                        "meta": r.meta or {}, "last_snapshot": r.last_snapshot,
                    }
                    out.append(dict_to_token(d))
                return out
        except Exception:
            log.exception("load_all_active failed")
            return []

    async def list_recent(self, since: datetime, limit: int = 500) -> list[LaunchpadToken]:
        if self.sf is None:
            return []
        try:
            async with self.sf() as db:
                rows = (
                    await db.execute(
                        select(M.LaunchpadTokenRow)
                        .where(M.LaunchpadTokenRow.discovered_at >= since)
                        .order_by(M.LaunchpadTokenRow.discovered_at.desc())
                        .limit(limit)
                    )
                ).scalars().all()
                return [
                    dict_to_token({
                        "id": r.id, "chain": r.chain, "token_address": r.token_address,
                        "launchpad": r.launchpad, "launchpad_contract": r.launchpad_contract,
                        "launch_event": r.launch_event, "launch_tx_hash": r.launch_tx_hash,
                        "creator_address": r.creator_address, "launched_at": r.launched_at,
                        "discovered_at": r.discovered_at, "status": r.status,
                        "symbol": r.symbol, "name": r.name,
                        "initial_score": r.initial_score, "current_score": r.current_score,
                        "score_delta": r.score_delta, "priority": r.priority,
                        "last_monitored_at": r.last_monitored_at, "last_score_at": r.last_score_at,
                        "meta": r.meta or {}, "last_snapshot": r.last_snapshot,
                    })
                    for r in rows
                ]
        except Exception:
            log.exception("list_recent failed")
            return []

    async def delete_stale(self, cutoff: datetime) -> dict[str, int]:
        """Retention: delete registry rows and market snapshots not refreshed since ``cutoff``.

        A token row counts as stale when its last monitoring time (or, if it was never monitored,
        its discovery time) is older than the cutoff.
        """
        out = {"tokens": 0, "snapshots": 0}
        if self.sf is None:
            return out
        from sqlalchemy import and_, delete, or_

        cutoff_naive = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)  # columns are tz-aware
        try:
            async with self.sf() as db:
                r1 = await db.execute(
                    delete(M.LaunchpadTokenRow).where(
                        or_(
                            M.LaunchpadTokenRow.last_monitored_at < cutoff_naive,
                            and_(M.LaunchpadTokenRow.last_monitored_at.is_(None),
                                 M.LaunchpadTokenRow.discovered_at < cutoff_naive),
                        )
                    )
                )
                r2 = await db.execute(delete(M.MarketSnapshot).where(M.MarketSnapshot.at < cutoff_naive))
                await db.commit()
                out["tokens"] = int(r1.rowcount or 0)
                out["snapshots"] = int(r2.rowcount or 0)
        except Exception:
            log.exception("delete_stale failed")
        return out

    async def get(self, chain: str, address: str) -> LaunchpadToken | None:
        # Redis first
        if self.redis is not None:
            try:
                raw = await self.redis.get(f"{REDIS_TOKEN_PREFIX}{chain}:{address.lower()}")
                if raw:
                    return dict_to_token(json.loads(raw))
            except Exception:
                pass
        if self.sf is None:
            return None
        try:
            async with self.sf() as db:
                row = (
                    await db.execute(
                        select(M.LaunchpadTokenRow).where(
                            M.LaunchpadTokenRow.chain == chain,
                            M.LaunchpadTokenRow.token_address == address.lower(),
                        )
                    )
                ).scalar_one_or_none()
                if not row:
                    return None
                t = dict_to_token({
                    "id": row.id, "chain": row.chain, "token_address": row.token_address,
                    "launchpad": row.launchpad, "launchpad_contract": row.launchpad_contract,
                    "launch_event": row.launch_event, "launch_tx_hash": row.launch_tx_hash,
                    "creator_address": row.creator_address, "launched_at": row.launched_at,
                    "discovered_at": row.discovered_at, "status": row.status,
                    "symbol": row.symbol, "name": row.name,
                    "initial_score": row.initial_score, "current_score": row.current_score,
                    "score_delta": row.score_delta, "priority": row.priority,
                    "last_monitored_at": row.last_monitored_at, "last_score_at": row.last_score_at,
                    "meta": row.meta or {}, "last_snapshot": row.last_snapshot,
                })
                await self._redis_set_token(t)
                return t
        except Exception:
            log.exception("get token failed")
            return None

    async def get_snapshot(self, chain: str, address: str) -> dict | None:
        if self.redis is not None:
            try:
                raw = await self.redis.get(f"{REDIS_SNAPSHOT_PREFIX}{chain}:{address.lower()}")
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        t = await self.get(chain, address)
        if t and t.meta:
            return t.meta.get("last_snapshot")
        return None

    async def _redis_set_token(self, token: LaunchpadToken) -> None:
        if self.redis is None:
            return
        try:
            key = f"{REDIS_TOKEN_PREFIX}{token.token_key}"
            await self.redis.set(key, json.dumps(token_to_dict(token), default=str), ex=REDIS_TOKEN_TTL)
        except Exception:
            log.debug("redis set token failed", exc_info=True)

    async def _redis_set_snapshot(self, chain: str, address: str, snap: dict) -> None:
        if self.redis is None:
            return
        try:
            key = f"{REDIS_SNAPSHOT_PREFIX}{chain}:{address.lower()}"
            await self.redis.set(key, json.dumps(snap, default=str), ex=REDIS_SNAPSHOT_TTL)
        except Exception:
            log.debug("redis set snapshot failed", exc_info=True)

    # ---- checkpoints (Postgres primary, Redis optional mirror) ----
    async def load_checkpoint(self, name: str = "global") -> dict | None:
        if self.sf is None:
            return None
        try:
            async with self.sf() as db:
                row = (
                    await db.execute(select(M.DiscoveryCheckpointRow).where(M.DiscoveryCheckpointRow.name == name))
                ).scalar_one_or_none()
                if not row:
                    return None
                return {
                    "last_success_at": _dt(row.last_success_at),
                    "last_attempt_at": _dt(row.last_attempt_at),
                    "last_window_start": _dt(row.last_window_start),
                    "last_window_end": _dt(row.last_window_end),
                    "tokens_found": row.tokens_found,
                    "error": row.error,
                }
        except Exception:
            log.exception("load_checkpoint failed")
            return None

    async def save_checkpoint(self, name: str, data: dict) -> None:
        if self.sf is None:
            return
        try:
            async with self.sf() as db:
                row = (
                    await db.execute(select(M.DiscoveryCheckpointRow).where(M.DiscoveryCheckpointRow.name == name))
                ).scalar_one_or_none()
                if row is None:
                    row = M.DiscoveryCheckpointRow(id=uid(), name=name, updated_at=utcnow())
                    db.add(row)
                row.last_success_at = _parse_dt(data.get("last_success_at"))
                row.last_attempt_at = _parse_dt(data.get("last_attempt_at"))
                row.last_window_start = _parse_dt(data.get("last_window_start"))
                row.last_window_end = _parse_dt(data.get("last_window_end"))
                row.tokens_found = int(data.get("tokens_found") or 0)
                row.error = data.get("error")
                row.updated_at = utcnow()
                await db.commit()
        except Exception:
            log.exception("save_checkpoint failed")


async def hydrate_registry(registry: GlobalTokenRegistry, store: PersistentTokenStore) -> int:
    """Load active tokens from DB into the in-memory registry at worker start."""
    tokens = await store.load_all_active()
    for t in tokens:
        registry.upsert(t)
    return len(tokens)
