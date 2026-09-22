"""Control-plane protocol: pairing, credentials, config versioning, heartbeats, commands and event ingestion."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.db import models as M
from tests.api_fixtures import pair_runner
from tests.test_api import POLICY


async def code(env, h):
    return (await env.client.post("/runners/pairing-codes", headers=h)).json()["code"]


def rh(token):
    return {"Authorization": f"Bearer {token}"}


async def pair_raw(env, h, name="r1"):
    r = await env.client.post("/runner/pair", json={"code": await code(env, h), "name": name, "version": "t"})
    assert r.status_code == 200, r.text
    return r.json()


def ev(seq, type_="RISK_ALERT", payload=None, id_=None, corr=""):
    return {"seq": seq, "id": id_ or f"e{seq:04d}{'x' * 20}", "type": type_, "at": datetime.now(timezone.utc).isoformat(), "correlation_id": corr, "payload": payload or {}}


# ------------------------------------------------------------------ pairing & credentials
async def test_pairing_code_is_single_use_and_stores_only_a_token_hash(env):
    h = await env.register()
    c = await code(env, h)
    assert len(c.replace("-", "")) == 12
    r = await env.client.post("/runner/pair", json={"code": c, "name": "laptop", "version": "0.1"})
    assert r.status_code == 200 and r.json()["token"].startswith("rt_")
    again = await env.client.post("/runner/pair", json={"code": c, "name": "x", "version": ""})
    assert again.status_code == 400
    async with env.app.state.c.sf() as db:
        row = (await db.execute(select(M.Runner))).scalar_one()
    assert r.json()["token"] not in (row.token_hash, row.name) and len(row.token_hash) == 64


async def test_bad_and_expired_codes_and_one_runner_per_account(env):
    h = await env.register()
    assert (await env.client.post("/runner/pair", json={"code": "AAAA-BBBB-CCCC", "name": "x", "version": ""})).status_code == 400
    c = await code(env, h)
    import hashlib
    await env.app.state.c.redis.delete("pair:" + hashlib.sha256(c.replace("-", "").encode()).hexdigest())   # simulate expiry
    assert (await env.client.post("/runner/pair", json={"code": c, "name": "x", "version": ""})).status_code == 400
    await pair_raw(env, h)
    r = await env.client.post("/runners/pairing-codes", headers=h)
    assert r.status_code == 409 and "already have a paired runner" in r.json()["detail"]


async def test_pair_endpoint_is_rate_limited(env):
    codes = [(await env.client.post("/runner/pair", json={"code": f"BAD{i}-BADX-BADX", "name": "x", "version": ""})).status_code for i in range(12)]
    assert 429 in codes


async def test_runner_and_user_credentials_are_not_interchangeable(env):
    h = await env.register()
    tok = (await pair_raw(env, h))["token"]
    assert (await env.client.get("/auth/me", headers=rh(tok))).status_code == 401              # runner token is useless on user routes
    assert (await env.client.get("/agent", headers=rh(tok))).status_code == 401
    assert (await env.client.get("/runner/config", headers=h)).status_code == 401              # user JWT is useless on runner routes
    assert (await env.client.get("/runner/config")).status_code == 401
    assert (await env.client.get("/runner/config", headers=rh("rt_" + "x" * 43))).status_code == 401
    assert (await env.client.get("/runner/config", headers=rh(tok))).status_code == 200


async def test_revoking_a_runner_locks_it_out_immediately(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    assert (await env.client.delete(f"/runners/{pr['runner_id']}", headers=h)).status_code == 200
    assert (await env.client.get("/runner/config", headers=rh(pr["token"]))).status_code == 401
    assert (await env.client.post("/runner/heartbeat", json={"state": "RUNNING", "mode": "PAPER"}, headers=rh(pr["token"]))).status_code == 401
    cfg = (await env.client.get("/agent", headers=h)).json()
    assert cfg["desired_state"] == "STOPPED" and cfg["runner"] is None
    other = await env.register("b@example.com")
    assert (await env.client.delete(f"/runners/{pr['runner_id']}", headers=other)).status_code == 404   # cannot revoke someone else's


# ------------------------------------------------------------------ config bundle & control
async def test_bundle_carries_policy_strategy_limits_and_no_secrets(env):
    h = await env.register()
    await env.client.post("/wallet/policy", json=POLICY, headers=h)
    pr = await pair_raw(env, h)
    b = (await env.client.get("/runner/config", headers=rh(pr["token"]))).json()["bundle"]
    assert b["mode"] == "PAPER" and b["desired_state"] == "STOPPED" and b["emergency_stop"] is False
    assert b["strategy"]["strategy_id"] == "traction_momentum" and b["strategy"]["min_score"] == 70
    assert b["risk_limits"]["max_trade_usdc"] == 25 and b["wallet_policy"]["allocated_capital_usdc"] == 500
    assert b["strategies_enabled"] == ["traction_momentum"] and b["runner_id"] == pr["runner_id"]
    text = json.dumps(b).lower()
    assert not any(w in text for w in ("private", "mnemonic", "otp", "api_key", "secret", "password"))


async def test_every_control_change_bumps_the_version_and_long_poll_returns_it(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    v0 = (await env.client.get("/runner/config", headers=rh(pr["token"]))).json()["bundle"]["version"]
    same = (await env.client.get("/runner/config", params={"since": v0, "wait": 1}, headers=rh(pr["token"]))).json()
    assert same["changed"] is False and same["bundle"] is None                          # nothing new: long-poll times out
    for call in (lambda: env.client.post("/agent/start", headers=h), lambda: env.client.post("/agent/pause", headers=h),
                 lambda: env.client.post("/agent/emergency-stop", json={"enabled": True}, headers=h),
                 lambda: env.client.put("/risk/limits", json={"max_trade_usdc": 10, "max_position_usdc": 20}, headers=h),
                 lambda: env.client.post("/controls/blacklist", json={"kind": "token", "value": "0xBAD"}, headers=h),
                 lambda: env.client.put("/strategies/traction_momentum/enabled", json={"enabled": False}, headers=h),
                 lambda: env.client.post("/strategies/traction_momentum/versions", json={"config": {"min_score": 80}}, headers=h)):
        before = (await env.client.get("/runner/config", headers=rh(pr["token"]))).json()["bundle"]["version"]
        r = await call()
        if r.status_code == 409 and "emergency" in r.text:
            continue
        assert r.status_code in (200, 201), r.text
        after = (await env.client.get("/runner/config", headers=rh(pr["token"]))).json()["bundle"]["version"]
        assert after > before, r.request.url


async def test_long_poll_wakes_up_quickly_when_the_user_hits_emergency_stop(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    v = (await env.client.get("/runner/config", headers=rh(pr["token"]))).json()["bundle"]["version"]

    async def poll():
        t0 = asyncio.get_event_loop().time()
        r = (await env.client.get("/runner/config", params={"since": v, "wait": 20}, headers=rh(pr["token"]))).json()
        return r, asyncio.get_event_loop().time() - t0
    task = asyncio.create_task(poll())
    await asyncio.sleep(0.5)
    await env.client.post("/agent/emergency-stop", json={"enabled": True}, headers=h)
    r, took = await task
    assert r["changed"] and r["bundle"]["emergency_stop"] is True and took < 3


async def test_start_needs_a_runner_and_emergency_stop_blocks_start(env):
    h = await env.register()
    r = await env.client.post("/agent/start", headers=h)
    assert r.status_code == 409 and "Pair a Local Runner" in r.json()["detail"]
    await pair_raw(env, h)
    assert (await env.client.post("/agent/emergency-stop", json={"enabled": True}, headers=h)).json()["emergency_stop"] is True
    assert (await env.client.post("/agent/start", headers=h)).status_code == 409
    assert (await env.client.post("/agent/emergency-stop", json={"enabled": False}, headers=h)).json()["emergency_stop"] is False
    assert (await env.client.post("/agent/start", headers=h)).json()["desired_state"] == "RUNNING"


# ------------------------------------------------------------------ heartbeat, status, commands
def hb(**o):
    d = {"version": "t", "state": "RUNNING", "mode": "PAPER", "applied_config_version": 3, "strategy_version": 1, "data_source": "DEMO DATA", "data_status": "ok",
         "ai": {"mode": "DISABLED", "provider": None, "model": None}, "open_positions": 0, "wallet_provider": None,
         "live": {"available": False, "reasons": ["LIVE is disabled locally on this runner"], "locally_enabled": False}, "local_ceilings": {"max_trade_usdc": 25}}
    d.update(o)
    return d


async def test_heartbeat_drives_agent_status_and_online_state(env):
    h = await env.register()
    assert (await env.client.get("/agent", headers=h)).json()["state"] == "OFFLINE"
    pr = await pair_raw(env, h)
    assert (await env.client.get("/agent", headers=h)).json()["state"] == "OFFLINE"        # paired but never seen
    r = await env.client.post("/runner/heartbeat", json=hb(), headers=rh(pr["token"]))
    assert r.status_code == 200 and r.json()["desired_config_version"] >= 1
    st = (await env.client.get("/agent", headers=h)).json()
    assert st["state"] == "RUNNING" and st["data_source"] == "DEMO DATA" and st["runner"]["online"] and st["runner"]["local_ceilings"]["max_trade_usdc"] == 25
    async with env.app.state.c.sf() as db:
        await db.execute(update(M.Runner).values(last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        await db.commit()
    st = (await env.client.get("/agent", headers=h)).json()
    assert st["state"] == "OFFLINE" and st["runner"]["online"] is False                    # stale heartbeat => offline
    runners = (await env.client.get("/runners", headers=h)).json()
    assert runners[0]["online"] is False and runners[0]["version"] == "t"


async def test_successful_cloud_heartbeat_clears_stale_control_plane_suspension(env):
    # The shared cloud worker sends its heartbeat payload before the HTTP call
    # succeeds, so the payload can legitimately contain the previous outage
    # message. A successful receipt must clear that stale dead-man reason.
    h = await env.register()
    async with env.app.state.c.sf() as db:
        uid = (await db.execute(select(M.User.id))).scalar_one()
        cfg = (await db.execute(select(M.AgentConfig).where(M.AgentConfig.user_id == uid))).scalar_one()
        cfg.execution_mode = "cloud_managed"
        await db.commit()

    env.app.state.c.settings.cloud_managed_enabled = True
    env.app.state.c.settings.platform_worker_token = "worker-test-token"
    headers = {"Authorization": "Bearer worker-test-token"}
    r = await env.client.post(
        f"/platform/tenants/{uid}/heartbeat",
        json=hb(entries_suspended_reason="control plane unreachable for more than 60s: no NEW entries until it is back"),
        headers=headers,
    )
    assert r.status_code == 200, r.text

    async with env.app.state.c.sf() as db:
        runner = (await db.execute(select(M.Runner).where(M.Runner.user_id == uid))).scalar_one()
        assert runner.last_seen_at is not None
        assert (runner.status or {}).get("entries_suspended_reason") is None


async def test_close_commands_are_queued_once_delivered_and_acknowledged(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    async with env.app.state.c.sf() as db:
        uid = (await db.execute(select(M.User.id))).scalar_one()
        db.add(M.Position(id="p1", user_id=uid, mode="PAPER", chain="arc", token_address="0x1", symbol="T", status="OPEN", entry_price=1, quantity=1,
                          initial_quantity=1, cost_basis_usdc=1, total_invested_usdc=1, peak_price=1, last_price=1, opened_at=datetime.now(timezone.utc),
                          last_new_high_at=datetime.now(timezone.utc)))
        await db.commit()
    assert (await env.client.post("/agent/close/nope", headers=h)).status_code == 404
    a = (await env.client.post("/agent/close/p1", headers=h)).json()
    b = (await env.client.post("/agent/close/p1", headers=h)).json()
    assert a["queued"] and a["command_id"] == b["command_id"]                                # idempotent
    resp = (await env.client.post("/runner/heartbeat", json=hb(), headers=rh(pr["token"]))).json()
    assert [c["type"] for c in resp["commands"]] == ["CLOSE_POSITION"] and resp["commands"][0]["payload"] == {"position_id": "p1"}
    await env.client.post(f"/runner/commands/{a['command_id']}/ack", json={"status": "DONE", "detail": "close executed"}, headers=rh(pr["token"]))
    assert (await env.client.post("/runner/heartbeat", json=hb(), headers=rh(pr["token"]))).json()["commands"] == []
    other = await env.register("b@example.com")
    assert (await env.client.post("/agent/close/p1", headers=other)).status_code == 404       # not your position


# ------------------------------------------------------------------ LIVE gating is driven by what the RUNNER reports
async def test_live_blockers_come_from_the_runner_and_never_from_hope(env):
    h = await env.register()
    r = await env.client.post("/agent/mode", json={"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}, headers=h)
    b = " | ".join(r.json()["detail"]["blockers"])
    assert r.status_code == 409 and "LIVE_TRADING_ENABLED is false" in b and "No Local Runner is paired" in b
    pr = await pair_raw(env, h)
    await env.client.post("/runner/heartbeat", json=hb(live={"available": False, "reasons": ["Circle CLI JSON output schemas are unverified", "LIVE is disabled locally"]}), headers=rh(pr["token"]))
    r = await env.client.post("/agent/mode", json={"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}, headers=h)
    b = " | ".join(r.json()["detail"]["blockers"])
    assert "schemas are unverified" in b and "LIVE is disabled locally" in b
    assert (await env.client.get("/auth/me", headers=h)).json()["mode"] == "PAPER"
    assert "MODE_SWITCH_REFUSED" in [a["action"] for a in (await env.client.get("/audit-logs", headers=h)).json()]


async def test_live_needs_server_flag_runner_readiness_and_the_typed_phrase(env):
    import httpx
    from httpx import ASGITransport
    from app.main import create_app
    from tests.api_fixtures import make_test_settings
    app2 = create_app(make_test_settings(live_trading_enabled=True))
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=ASGITransport(app=app2), base_url="http://t") as c:
            tok = (await c.post("/auth/register", json={"email": "live@example.com", "password": "correct horse battery"})).json()["access_token"]
            h = {"Authorization": f"Bearer {tok}"}
            code_ = (await c.post("/runners/pairing-codes", headers=h)).json()["code"]
            rt = (await c.post("/runner/pair", json={"code": code_, "name": "r", "version": ""})).json()["token"]
            ready = hb(mode="LIVE", live={"available": True, "reasons": [], "locally_enabled": True, "provider": "fake", "session_authorized": True})
            assert (await c.post("/agent/mode", json={"mode": "LIVE", "confirmation": "wrong"}, headers=h)).json()["detail"]["error"] == "CONFIRMATION_REQUIRED"
            assert (await c.post("/agent/mode", json={"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}, headers=h)).status_code == 409   # runner not ready yet
            await c.post("/runner/heartbeat", json=ready, headers=rh(rt))
            r = await c.post("/agent/mode", json={"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}, headers=h)
            assert r.status_code == 200 and r.json()["mode"] == "LIVE" and r.json()["desired_state"] == "STOPPED"   # explicit START still required
            b = (await c.get("/runner/config", headers=rh(rt))).json()["bundle"]
            assert b["mode"] == "LIVE"


# ------------------------------------------------------------------ event ingestion
async def test_ingestion_is_replay_safe_ordered_and_skips_poison(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    T = rh(pr["token"])
    batch = {"events": [ev(1), ev(2), ev(3, type_="NOT_A_REAL_EVENT"), ev(4, payload={"blob": "x" * 300_000})]}
    a = (await env.client.post("/runner/events", json=batch, headers=T)).json()
    assert a == {"last_seq": 4, "accepted": 2, "skipped": 2}                                  # unknown type + oversize skipped, seq still advances
    again = (await env.client.post("/runner/events", json=batch, headers=T)).json()
    assert again["accepted"] == 0 and again["skipped"] == 4 and again["last_seq"] == 4        # replay changes nothing
    old = (await env.client.post("/runner/events", json={"events": [ev(2, id_="zz" + "y" * 20)]}, headers=T)).json()
    assert old["accepted"] == 0                                                                # out-of-order/older seq ignored
    acts = (await env.client.get("/activity?limit=50", headers=h)).json()
    assert len([e for e in acts if e["type"] == "RISK_ALERT"]) == 2
    assert (await env.client.post("/runner/events", json={"events": [{**ev(5), "surprise": 1}]}, headers=T)).status_code == 422   # strict schema
    assert (await env.client.post("/runner/events", json={"events": [ev(i) for i in range(6, 6 + 201)]}, headers=T)).status_code == 422   # batch cap


async def test_a_runner_cannot_overwrite_another_users_rows(env):
    a, b = await env.register("a@example.com"), await env.register("b@example.com")
    ta = (await pair_raw(env, a))["token"]
    async with env.app.state.c.sf() as db:
        uid_b = (await db.execute(select(M.User.id).where(M.User.email == "b@example.com"))).scalar_one()
        now = datetime.now(timezone.utc)
        db.add(M.Position(id="victim", user_id=uid_b, mode="PAPER", chain="arc", token_address="0xb", status="OPEN", entry_price=1, quantity=5, initial_quantity=5,
                          cost_basis_usdc=5, total_invested_usdc=5, peak_price=1, last_price=1, opened_at=now, last_new_high_at=now))
        await db.commit()
    pos = {"id": "victim", "mode": "PAPER", "chain": "arc", "token_address": "0xa", "status": "CLOSED", "entry_price": 1, "quantity": 0, "initial_quantity": 5,
           "cost_basis_usdc": 0, "total_invested_usdc": 5, "peak_price": 1, "last_price": 9, "opened_at": now.isoformat(), "last_new_high_at": now.isoformat()}
    r = (await env.client.post("/runner/events", json={"events": [ev(1, "POSITION_CLOSED", {"position": pos}, corr="victim")]}, headers=rh(ta))).json()
    assert r["accepted"] == 0
    async with env.app.state.c.sf() as db:
        row = await db.get(M.Position, "victim")
    assert row.status == "OPEN" and row.quantity == 5 and row.token_address == "0xb"


async def test_ingested_events_are_streamed_to_that_users_ui_only(env):
    a, b = await env.register("a@example.com"), await env.register("b@example.com")
    ta = (await pair_raw(env, a))["token"]
    async with env.app.state.c.sf() as db:
        uid_a = (await db.execute(select(M.User.id).where(M.User.email == "a@example.com"))).scalar_one()
        uid_b = (await db.execute(select(M.User.id).where(M.User.email == "b@example.com"))).scalar_one()
    qa, qb = env.app.state.c.hub.subscribe(uid_a), env.app.state.c.hub.subscribe(uid_b)
    await env.client.post("/runner/events", json={"events": [ev(1, "RISK_ALERT", {"kind": "X"})]}, headers=rh(ta))
    assert (await asyncio.wait_for(qa.get(), 1))["type"] == "RISK_ALERT" and qb.empty()


# ------------------------------------------------------------------ concurrency (found by running real processes)
async def test_config_row_creation_and_version_bumps_are_race_safe(env):
    from app.services import control
    uid = "u" + "0" * 31
    async with env.app.state.c.sf() as db:
        db.add(M.User(id=uid, email="race@example.com", password_hash="x"))
        await db.commit()

    async def touch():
        async with env.app.state.c.sf() as db:
            cfg = await control.get_config(db, uid)                       # 20 first-touches at the same instant: none may 500
            await db.commit()
            return cfg.version
    assert len(await asyncio.gather(*[touch() for _ in range(20)])) == 20

    async def bump():
        async with env.app.state.c.sf() as db:
            cfg = await control.bump(db, uid)
            await db.commit()
            return cfg.version
    versions = await asyncio.gather(*[bump() for _ in range(20)])
    assert sorted(versions) == list(range(2, 22))                          # every concurrent change got its own version: no lost increments
    async with env.app.state.c.sf() as db:
        assert (await control.get_config(db, uid)).version == 21


async def test_concurrent_first_contact_from_runner_and_ui_never_errors(env):
    h = await env.register()
    pr = await pair_raw(env, h)
    T = rh(pr["token"])
    async with env.app.state.c.sf() as db:
        await db.execute(M.AgentConfig.__table__.delete())                # simulate a user whose config row does not exist yet
        await db.commit()
    rs = await asyncio.gather(env.client.get("/runner/config", headers=T), env.client.post("/runner/heartbeat", json={"state": "STOPPED", "mode": "PAPER"}, headers=T),
                              env.client.get("/agent", headers=h), env.client.get("/runner/config", headers=T), env.client.post("/agent/pause", headers=h))
    assert [r.status_code for r in rs] == [200] * 5
