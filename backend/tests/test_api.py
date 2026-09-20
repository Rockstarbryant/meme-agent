import asyncio
import json
import subprocess
import sys

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import text

from tests.api_fixtures import BACKEND, TEST_DB, drive, make_test_settings, pair_runner, runner_settings

POLICY = dict(allocated_capital_usdc=500, max_trade_usdc=25, max_position_usdc=50, max_daily_loss_usdc=50,
              max_open_positions=5, max_slippage_pct=2, min_liquidity_usdc=10000)


# ---------------------------------------------------------------- health / auth
async def test_health_reports_db_and_redis(env):
    r = await env.client.get("/health")
    j = r.json()
    assert r.status_code == 200 and j["database"]["ok"] and j["redis"]["ok"]
    assert j["arc_rpc"]["ok"] is False
    assert j["live_trading"] == {"enabled": False, "arc_integration_verified": False}


async def test_auth_flow_hashing_and_revocation(env):
    c = env.client
    assert (await c.post("/auth/register", json={"email": "bad", "password": "long enough pass"})).status_code == 422
    assert (await c.post("/auth/register", json={"email": "a@example.com", "password": "short"})).status_code == 422
    h = await env.register()
    assert (await c.post("/auth/register", json={"email": "A@example.com", "password": "correct horse battery"})).status_code == 409
    assert (await c.get("/auth/me")).status_code == 401
    me = (await c.get("/auth/me", headers=h)).json()
    assert me["mode"] == "PAPER" and "password" not in json.dumps(me)
    assert (await c.post("/auth/login", json={"email": "a@example.com", "password": "wrong password!!"})).status_code == 401
    assert (await c.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong password!!"})).status_code == 401
    assert (await c.post("/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})).status_code == 200
    async with env.app.state.c.sf() as db:
        stored = (await db.execute(text("select password_hash from users"))).scalar_one()
    assert stored.startswith("$2") and "correct horse" not in stored
    assert (await c.post("/auth/logout", headers=h)).status_code == 200
    assert (await c.get("/auth/me", headers=h)).status_code == 401


async def test_login_is_rate_limited(env):
    await env.register()
    codes = [(await env.client.post("/auth/login", json={"email": "a@example.com", "password": "nope nope nope"})).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


async def test_tampered_or_foreign_token_rejected(env):
    h = await env.register()
    assert (await env.client.get("/auth/me", headers={"Authorization": h["Authorization"][:-3] + "abc"})).status_code == 401


# ---------------------------------------------------------------- browser wallet (optional manual signing; NOT the autonomous path)
def sign(acct, message):
    return acct.sign_message(encode_defunct(text=message)).signature.hex()


async def connect_wallet(env, h, acct):
    ch = await env.client.post("/wallet/challenge", json={"address": acct.address}, headers=h)
    msg = ch.json()["message"]
    return await env.client.post("/wallet/connect", json={"address": acct.address, "signature": "0x" + sign(acct, msg).removeprefix("0x")}, headers=h), msg


async def test_wallet_ownership_policy_authorization_and_revocation(env):
    c, h = env.client, await env.register()
    acct, other = Account.create(), Account.create()  # throwaway TEST keys
    ch = await c.post("/wallet/challenge", json={"address": acct.address}, headers=h)
    forged = await c.post("/wallet/connect", json={"address": acct.address, "signature": "0x" + sign(other, ch.json()["message"]).removeprefix("0x")}, headers=h)
    assert forged.status_code == 400
    r, msg = await connect_wallet(env, h, acct)
    assert r.status_code == 201 and r.json()["ownership_verified"]
    replay = await c.post("/wallet/connect", json={"address": acct.address, "signature": "0x" + sign(acct, msg).removeprefix("0x")}, headers=h)
    assert replay.status_code == 400

    w = (await c.get("/wallet", headers=h)).json()
    assert w["execution_capability"]["label"] == "Paper trading only" and w["agent_wallet"]["available"] is False and w["runner_wallet"] is None
    assert (await c.post("/wallet/authorize", json={"capability": "PER_TRADE_SIGNING"}, headers=h)).status_code == 409
    assert (await c.post("/wallet/policy", json={**POLICY, "max_trade_usdc": 100, "max_position_usdc": 50}, headers=h)).status_code == 422
    pol = await c.post("/wallet/policy", json=POLICY, headers=h)
    assert pol.status_code == 200 and pol.json()["version"] == 1
    a = await c.post("/wallet/authorize", json={"capability": "AUTONOMOUS_DELEGATED"}, headers=h)
    assert a.status_code == 501 and "Local Runner" in a.json()["detail"]["detail"]
    assert (await c.post("/wallet/authorize", json={"capability": "PER_TRADE_SIGNING"}, headers=h)).status_code == 201
    w = (await c.get("/wallet", headers=h)).json()
    assert w["execution_capability"]["label"] == "Explicit per-trade signing" and not w["execution_capability"]["autonomous"]
    await c.post("/wallet/policy", json={**POLICY, "max_trade_usdc": 30}, headers=h)
    assert (await c.get("/wallet", headers=h)).json()["execution_capability"]["label"] == "Paper trading only"
    await c.post("/wallet/authorize", json={"capability": "PER_TRADE_SIGNING"}, headers=h)
    assert (await c.post("/wallet/revoke", headers=h)).json()["revoked"] == 1
    acts = [x["action"] for x in (await c.get("/wallet/activity", headers=h)).json()]
    assert {"WALLET_CONNECTED", "WALLET_POLICY_SET", "WALLET_AUTHORIZED", "WALLET_REVOKED"} <= set(acts)


# ---------------------------------------------------------------- control-plane configuration
async def test_risk_limits_validation_confirmation_and_audit(env):
    c, h = env.client, await env.register()
    assert (await c.put("/risk/limits", json={"max_trade_usdc": 100, "max_position_usdc": 50, "max_total_exposure_usdc": 250}, headers=h)).status_code == 422
    assert (await c.put("/risk/limits", json={"max_trade_usdc": 10, "max_position_usdc": 20}, headers=h)).status_code == 200
    lim = (await c.get("/risk/limits", headers=h)).json()
    assert lim["limits"]["max_trade_usdc"] == 10 and lim["effective_limits"]["max_trade_usdc"] == 10
    log = [a for a in (await c.get("/audit-logs", headers=h)).json() if a["action"] == "RISK_LIMITS_CHANGED"]
    assert log and log[0]["detail"]["after"]["max_trade_usdc"] == 10


async def test_strategy_versions_are_immutable_and_toggleable(env):
    c, h = env.client, await env.register()
    v = await c.post("/strategies/traction_momentum/versions", json={"config": {"min_score": 80}}, headers=h)
    assert v.status_code == 201 and v.json()["version"] == 2
    vs = (await c.get("/strategies/traction_momentum/versions", headers=h)).json()
    assert [x["version"] for x in vs] == [2, 1] and vs[0]["config"]["min_score"] == 80 and vs[1]["config"]["min_score"] == 70
    assert (await c.post("/strategies/traction_momentum/versions", json={"config": {"min_score": "abc"}}, headers=h)).status_code == 422
    assert (await c.get("/strategies", headers=h)).json()[0]["enabled"] is True
    assert (await c.put("/strategies/traction_momentum/enabled", json={"enabled": False}, headers=h)).json()["enabled"] is False
    assert (await c.get("/strategies", headers=h)).json()[0]["enabled"] is False
    assert (await c.put("/strategies/nope/enabled", json={"enabled": True}, headers=h)).status_code == 404


async def test_blacklist_persists_and_validates(env):
    c, h = env.client, await env.register()
    assert (await c.post("/controls/blacklist", json={"kind": "token", "value": "0xABC"}, headers=h)).status_code == 201
    assert (await c.post("/controls/blacklist", json={"kind": "nope", "value": "x"}, headers=h)).status_code == 422
    assert (await c.get("/controls/blacklist", headers=h)).json()["tokens"] == ["0xabc"]
    await c.request("DELETE", "/controls/blacklist", json={"kind": "token", "value": "0xabc"}, headers=h)
    assert (await c.get("/controls/blacklist", headers=h)).json()["tokens"] == []


async def test_settings_say_that_ai_and_keys_live_on_the_runner(env):
    h = await env.register()
    s = (await env.client.get("/settings", headers=h)).json()
    assert "never holds them" in s["ai"]["note"] and "runner" in s["market_data"].lower()


# ---------------------------------------------------------------- isolation & the control plane's read model
async def test_users_are_isolated(env, tmp_path):
    a, b = await env.register("a@example.com"), await env.register("b@example.com")
    rt = await pair_runner(env, a, tmp_path)
    await env.client.post("/agent/start", headers=a)
    await drive(rt, 8)
    did = (await env.client.get("/opportunities", headers=a)).json()[0]["decision_id"]
    assert (await env.client.get(f"/decisions/{did}", headers=b)).status_code == 404
    assert (await env.client.get("/opportunities", headers=b)).json() == []
    assert (await env.client.get("/positions", headers=b)).json() == []
    assert (await env.client.get("/agent", headers=b)).json()["runner"] is None


async def test_control_plane_never_produces_trades_by_itself(env):
    """With no runner connected, starting is refused and nothing is ever executed server-side."""
    h = await env.register()
    assert (await env.client.post("/agent/start", headers=h)).status_code == 409
    await asyncio.sleep(1.0)
    assert (await env.client.get("/orders", headers=h)).json() == [] and (await env.client.get("/positions", headers=h)).json() == []
    st = (await env.client.get("/agent", headers=h)).json()
    assert st["state"] == "OFFLINE" and st["runner"] is None and st["mode_label"] == "PAPER MODE"


# ---------------------------------------------------------------- DB backstops & Redis primitives
async def test_db_blocks_duplicate_active_orders_but_allows_retry_after_failure(env):
    from sqlalchemy.exc import IntegrityError
    from app.db import models as M
    h = await env.register()
    uid = (await env.client.get("/auth/me", headers=h)).json()["id"]

    def order(oid, status):
        return M.Order(id=oid, user_id=uid, idempotency_key="K1", mode="PAPER", side="BUY", token_address="0x1", status=status, simulated=True, data={})
    async with env.app.state.c.sf() as db:
        db.add(order("o1", "FAILED"))
        db.add(order("o2", "FILLED"))
        await db.commit()
    async with env.app.state.c.sf() as db:
        db.add(order("o3", "FILLED"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_redis_idempotency_lease_and_ratelimit(env):
    from app.infra.redis import RateLimiter, RedisIdempotencyStore, RedisLease
    r = env.app.state.c.redis
    s = RedisIdempotencyStore(r, prefix="t:")
    assert await s.claim("k") and not await s.claim("k")
    await s.complete("k", {"ok": 1})
    assert await s.get("k") == {"ok": 1}
    await s.release("k")
    assert await s.claim("k")
    a, b = RedisLease(r, "x"), RedisLease(r, "x")
    assert await a.acquire() and not await b.acquire() and await a.renew() and not await b.renew()
    await a.release()
    assert await b.acquire()
    rl = RateLimiter(r)
    assert [(await rl.hit("k", 3, 60))[0] for _ in range(5)] == [True, True, True, False, False]
    local = RateLimiter(None)  # Redis down => still limited, never unlimited
    assert [(await local.hit("k", 2, 60))[0] for _ in range(3)] == [True, True, False]


# ---------------------------------------------------------------- SSE + a real runner over real HTTP
async def test_sse_stream_delivers_a_real_runners_events_over_real_sockets(migrated_db, tmp_path):
    import httpx
    import uvicorn
    from app.chains.demo import DemoMarketData
    from app.main import create_app
    from runner.client import ControlPlaneClient
    from runner.runtime import RunnerRuntime
    from runner.store import LocalStore
    from tests.api_fixtures import _reset
    await _reset()
    app = create_app(make_test_settings())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    base = "http://127.0.0.1:8765"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            for _ in range(50):
                try:
                    if (await c.get("/health/live")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
            tok = (await c.post("/auth/register", json={"email": "s@example.com", "password": "correct horse battery"})).json()["access_token"]
            h = {"Authorization": f"Bearer {tok}"}
            assert (await c.get("/stream", params={"ticket": "bogus"})).status_code == 401
            t1 = (await c.post("/stream/ticket", headers=h)).json()["ticket"]
            code = (await c.post("/runners/pairing-codes", headers=h)).json()["code"]
            client = ControlPlaneClient(base)
            pr = await client.pair(code, "sock-runner", "t")
            client.token = pr.token
            rt = RunnerRuntime(runner_settings(tmp_path), client, LocalStore(tmp_path / "state.db"), market_data=DemoMarketData(seed=1), wallet=None, chain=None, llm=None)
            seen = []
            async with c.stream("GET", "/stream", params={"ticket": t1}) as resp:
                assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
                assert (await c.get("/stream", params={"ticket": t1})).status_code == 401
                await c.post("/agent/start", headers=h)

                async def run_runner():
                    await drive(rt, 12)
                runner_task = asyncio.create_task(run_runner())
                async with asyncio.timeout(30):
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            seen.append(line[7:])
                        if "DECISION_RECORDED" in seen and "ORDER_FILLED" in seen:
                            break
                await runner_task
            await c.post("/agent/stop", headers=h)
            assert seen[0] == "hello" and "DECISION_RECORDED" in seen and "ORDER_FILLED" in seen
            await client.aclose()
    finally:
        server.should_exit = True
        await task


# ---------------------------------------------------------------- schema drift
def test_migration_matches_models(migrated_db):
    import os
    r = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=BACKEND, capture_output=True, text=True, env={**os.environ, "ALEMBIC_DATABASE_URL": TEST_DB})
    assert r.returncode == 0, r.stdout + r.stderr
