"""Ranking / rotation / retention / heartbeat-state behaviour added for the cloud worker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.discovery.ranking import RankingConfig, balance_score, rank_candidates
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken
from app.domain.runner_protocol import Heartbeat
from app.core.types import TradingMode
from runner.cloud_worker import CloudWorker

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
CFG = RankingConfig(max_per_cycle=3, eval_min_interval_s=300, max_age_s=24 * 3600)


def tok(addr: str, *, seen_min_ago: float = 1, priority: str = "WARM", **snap) -> LaunchpadToken:
    t = LaunchpadToken(chain="arc", token_address=addr, discovered_at=NOW - timedelta(minutes=seen_min_ago),
                       priority=priority)
    t.last_monitored_at = NOW - timedelta(minutes=seen_min_ago)
    t.meta = {"last_snapshot": snap}
    return t


BALANCED = dict(liquidity=120_000, market_cap=600_000, holder_count=900, volume_5m=9_000, buys_5m=30, sells_5m=20,
                price_change_5m=2.0, price_change_15m=4.0)
THIN = dict(liquidity=6_000, market_cap=900_000, holder_count=40, volume_5m=0, buys_5m=0, sells_5m=0)


def test_balanced_trending_beats_thin_pool_with_huge_cap():
    assert balance_score(BALANCED) > balance_score(THIN) + 25


def test_unknown_holders_do_not_exclude_a_token():
    snap = {k: v for k, v in BALANCED.items() if k != "holder_count"}
    assert balance_score(snap) > 40


def test_older_than_24h_is_dropped_and_dead_is_skipped():
    old = tok("0xold", seen_min_ago=25 * 60, **BALANCED)
    dead = tok("0xdead", priority="DEAD", **BALANCED)
    fresh = tok("0xfresh", **BALANCED)
    assert rank_candidates([old, dead, fresh], {}, NOW, CFG) == ["0xfresh"]


def test_floors_filter_dust_but_unknown_values_pass():
    dust = tok("0xdust", liquidity=300, market_cap=900)
    unknown = tok("0xunknown")  # empty snapshot: still gets a turn
    assert rank_candidates([dust, unknown], {}, NOW, CFG) == ["0xunknown"]


def test_recently_evaluated_are_skipped_so_rotation_reaches_everyone():
    toks = [tok(f"0x{i}", **BALANCED) for i in range(6)]
    eval_at: dict = {}
    seen: list[str] = []
    for cycle in range(2):
        batch = rank_candidates(toks, eval_at, NOW + timedelta(seconds=cycle * 60), CFG)
        assert len(batch) == 3
        assert not set(batch) & set(seen)          # nobody repeated within the interval
        for a in batch:
            eval_at[a] = NOW + timedelta(seconds=cycle * 60)
        seen += batch
    assert set(seen) == {f"0x{i}" for i in range(6)}


def test_stale_token_eventually_returns():
    t = tok("0xa", **BALANCED)
    ea = {"0xa": NOW - timedelta(seconds=301)}
    assert rank_candidates([t], ea, NOW, CFG) == ["0xa"]


def test_replayed_heartbeat_follows_current_desired_state():
    hb = Heartbeat(state="PAUSED", mode=TradingMode.PAPER)
    assert CloudWorker._with_current_state(hb, {"desired_state": "RUNNING"}).state == "RUNNING"
    hb = Heartbeat(state="RUNNING", mode=TradingMode.PAPER)
    assert CloudWorker._with_current_state(hb, {"desired_state": "PAUSED"}).state == "PAUSED"
    assert CloudWorker._with_current_state(hb, {"desired_state": "RUNNING", "emergency_stop": True}).state == "PAUSED"
    blocked = Heartbeat(state="LIVE_BLOCKED", mode=TradingMode.LIVE)
    assert CloudWorker._with_current_state(blocked, {"desired_state": "RUNNING"}).state == "LIVE_BLOCKED"


def test_registry_remove():
    r = GlobalTokenRegistry()
    t = tok("0xabc", **BALANCED)
    r.upsert(t)
    assert r.remove(t.token_key) is t and r.get("arc", "0xabc") is None


@pytest.mark.asyncio
async def test_retention_prune_deletes_old_untraded_decisions_only():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.db import models as M
    from app.db.base import Base, uid
    from app.services.retention import prune_old

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    def dec(id_: str, age_h: float) -> M.Decision:
        return M.Decision(id=id_, user_id="u1", mode="PAPER", token_key="arc:" + id_, symbol="X", is_demo=False,
                          final_action="REJECT", final_reason="r", strategy_id="s", strategy_version=1, score=1.0,
                          risk_score=1.0, qualified=False, strategy_config={}, risk_limits={}, controls={},
                          market={}, created_at=NOW - timedelta(hours=age_h))

    async with sf() as db:
        db.add(M.User(id="u1", email="a@b.c", password_hash="x")) if hasattr(M.User, "password_hash") else None
        await db.commit()
        db.add_all([dec("new", 1), dec("old", 30)])
        await db.commit()
    res = await prune_old(sf, 24, now=NOW)
    async with sf() as db:
        ids = {d for d in (await db.execute(select(M.Decision.id))).scalars().all()}
    assert ids == {"new"} and res["decisions"] == 1


@pytest.mark.asyncio
async def test_runtime_close_never_closes_the_shared_market_data():
    """Regression: rt.aclose() closed the cloud worker's shared providers after the first tenant cycle."""
    from runner.runtime import RunnerRuntime

    class Closable:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    rt = object.__new__(RunnerRuntime)
    own, shared = Closable(), Closable()
    rt.market_data, rt._md_shared, rt._own_market_data = own, False, None
    rt._enrichment, rt._rpc, rt.engine = None, None, None
    rt.use_shared_market_data(shared)
    assert rt.market_data is shared
    await rt.aclose()
    assert shared.closed is False and own.closed is True
