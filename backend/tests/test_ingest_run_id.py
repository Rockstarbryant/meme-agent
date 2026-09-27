"""Regression test for the "events silently stop after a runner/worker restart" bug.

The runner's event `seq` is a local AUTOINCREMENT counter (runner/store.py's
`outbox` table) -- monotonic only within one continuously-running local
outbox. A cloud-worker container restart (redeploy, crash, ephemeral disk
reset) recreates that local database, so the counter restarts at 1, while the
server's `Runner.last_seq` watermark stays at whatever high number the
previous run reached. Before the `run_id` fix, every event from the new run
looked like an old replay (`seq <= last_seq`) and was silently dropped
forever -- even though `_handle()`'s own per-event id-based idempotency check
would have accepted it fine. This test uses an in-memory SQLite DB directly
(no Postgres/Redis required) so it can run anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as M
from app.db.base import Base
from app.domain.runner_protocol import EventBatch, RunnerEvent
from app.services.hub import EventHub
from app.services.ingest import ingest

NOW = datetime.now(timezone.utc)


def _ev(seq: int, id_: str) -> RunnerEvent:
    return RunnerEvent(seq=seq, id=id_, type="RISK_ALERT", at=NOW, correlation_id="", payload={"kind": "x"})


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        yield db
    await engine.dispose()


async def _make_runner(db: AsyncSession) -> M.Runner:
    user = M.User(email="t@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    runner = M.Runner(user_id=user.id, name="platform-shared-worker", token_hash="abc", version="cloud")
    db.add(runner)
    await db.commit()
    return runner


async def test_events_are_not_lost_after_a_runner_restart_resets_local_seq(session):
    db = session
    runner = await _make_runner(db)
    hub = EventHub()

    # Run A: normal operation, local outbox accumulates seq 1..5.
    batch_a = EventBatch(events=[_ev(i, f"a{i:04d}" + "x" * 20) for i in range(1, 6)], run_id="run-A")
    ack1 = await ingest(db, hub, runner, batch_a)
    assert ack1.accepted == 5 and ack1.skipped == 0
    assert runner.last_seq == 5 and runner.last_run_id == "run-A"

    # Cloud worker container restarts: fresh ephemeral disk, local outbox
    # recreated -> local seq counter restarts at 1, with a NEW run_id.
    batch_b = EventBatch(events=[_ev(i, f"b{i:04d}" + "x" * 20) for i in range(1, 4)], run_id="run-B")
    ack2 = await ingest(db, hub, runner, batch_b)
    assert ack2.accepted == 3, "BUG: post-restart events were silently dropped"
    assert ack2.skipped == 0
    assert runner.last_run_id == "run-B" and runner.last_seq == 3

    # A genuine replay within the SAME run must still be skipped.
    ack3 = await ingest(db, hub, runner, batch_b)
    assert ack3.accepted == 0 and ack3.skipped == 3

    rows = (await db.execute(M.EventRow.__table__.select())).fetchall()
    assert len(rows) == 8  # 5 from run A + 3 from run B; none lost, none duplicated


async def test_legacy_runner_without_run_id_keeps_old_replay_behaviour(session):
    """A runner that hasn't upgraded yet (empty run_id) must not get special-cased."""
    db = session
    runner = await _make_runner(db)
    hub = EventHub()

    batch = EventBatch(events=[_ev(i, f"c{i:04d}" + "x" * 20) for i in range(1, 4)])  # run_id defaults to ""
    ack1 = await ingest(db, hub, runner, batch)
    assert ack1.accepted == 3 and runner.last_seq == 3 and runner.last_run_id is None

    replay = await ingest(db, hub, runner, batch)
    assert replay.accepted == 0 and replay.skipped == 3
