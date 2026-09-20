import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass

import asyncpg
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from app.config import Settings
from app.main import create_app

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://agent:agent@localhost:5432/agent_test")
TEST_REDIS = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_settings(**over) -> Settings:
    d = dict(_env_file=None, database_url=TEST_DB, redis_url=TEST_REDIS, jwt_secret="j" * 40, secret_key="s" * 40,
             arc_rpc_url="")
    d.update(over)
    return Settings(**d)


async def _reset():
    conn = await asyncpg.connect(TEST_DB.replace("+asyncpg", ""))
    try:
        names = [r["tablename"] for r in await conn.fetch("select tablename from pg_tables where schemaname='public'")]
        keep = {"alembic_version", "chains", "strategies", "strategy_versions"}
        await conn.execute("TRUNCATE " + ", ".join(f'"{n}"' for n in names if n not in keep) + " RESTART IDENTITY CASCADE")
        await conn.execute("DELETE FROM strategy_versions WHERE user_id IS NOT NULL")
    finally:
        await conn.close()
    import redis.asyncio as aioredis
    r = aioredis.Redis.from_url(TEST_REDIS)
    await r.flushdb()
    await r.aclose()


@pytest.fixture(scope="session")
def migrated_db():
    try:
        asyncio.run(asyncpg.connect(TEST_DB.replace("+asyncpg", ""))).close  # noqa: B018
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable for API tests: {type(e).__name__}")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True,
                   env={**os.environ, "ALEMBIC_DATABASE_URL": TEST_DB})


@dataclass
class Env:
    app: object
    client: httpx.AsyncClient
    settings: Settings

    async def register(self, email="a@example.com", password="correct horse battery"):
        r = await self.client.post("/auth/register", json={"email": email, "password": password})
        assert r.status_code == 201, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest_asyncio.fixture
async def env(migrated_db):
    await _reset()
    app = create_app(make_test_settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            yield Env(app, client, app.state.c.settings)


# ---------------------------------------------------------------- runner harness
from runner.client import ControlPlaneClient  # noqa: E402
from runner.runtime import RunnerRuntime  # noqa: E402
from runner.settings import RunnerSettings  # noqa: E402
from runner.store import LocalStore  # noqa: E402
from app.chains.demo import DemoMarketData  # noqa: E402


def runner_settings(tmp_path, **over) -> RunnerSettings:
    d = dict(_env_file=None, state_dir=tmp_path, data_source="demo", reevaluate_after_s=0.0, snapshot_interval_s=0.0, market_snapshot_every_s=0.0, max_offline_s=60.0)
    d.update(over)
    return RunnerSettings(**d)


async def pair_runner(env: Env, headers: dict, tmp_path, *, client_wrapper=None, **kw) -> RunnerRuntime:
    """A REAL runner (real runtime, real local store) talking to the real control plane in-process."""
    code = (await env.client.post("/runners/pairing-codes", headers=headers)).json()["code"]
    http = httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://t")
    client = ControlPlaneClient("http://t", http=http)
    pr = await client.pair(code, "test-runner", "test")
    client.token = pr.token
    if client_wrapper:
        client = client_wrapper(client)
    settings = kw.pop("settings", None) or runner_settings(tmp_path)
    store = kw.pop("store", None) or LocalStore(tmp_path / "state.db")
    rt = RunnerRuntime(settings, client, store, market_data=kw.pop("market_data", DemoMarketData(seed=1)),
                       wallet=kw.pop("wallet", None), chain=kw.pop("chain", None), llm=kw.pop("llm", None), **kw)
    rt.runner_id = pr.runner_id  # type: ignore[attr-defined]
    return rt


async def drive(rt: RunnerRuntime, steps: int = 1, heartbeat_every: int = 5, config_every: int = 1):
    for i in range(steps):
        if i % config_every == 0:
            await rt.poll_config_once()
        await rt.discover_once()
        await rt.monitor_once()
        await rt.upload_once()
        if i % heartbeat_every == 0:
            await rt.heartbeat_once()
