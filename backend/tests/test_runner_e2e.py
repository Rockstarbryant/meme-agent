"""End to end: a REAL Local Runner (runtime + local store) driving a REAL control plane (FastAPI + Postgres + Redis)."""
import asyncio

import pytest
from sqlalchemy import func, select

from app.db import models as M
from runner.client import ControlPlaneClient, ControlPlaneError, Revoked
from runner.runtime import RunnerRuntime
from runner.store import LocalStore
from runner.wallets import CircleAgentWalletProvider, CircleCli
from tests.api_fixtures import drive, pair_runner, runner_settings
from tests.test_api import POLICY

pytestmark = pytest.mark.timeout(120)


async def get(env, h, path):
    r = await env.client.get(path, headers=h)
    assert r.status_code == 200, (path, r.text)
    return r.json()


async def setup(env, tmp_path, **kw):
    h = await env.register()
    await env.client.post("/wallet/policy", json=POLICY, headers=h)
    rt = await pair_runner(env, h, tmp_path, **kw)
    return h, rt


class Counting:
    """Proxy that records every ack the control plane returns, so tests can assert nothing was silently skipped."""
    def __init__(self, inner):
        self.inner, self.skipped, self.accepted = inner, 0, 0

    @property
    def token(self): return self.inner.token
    @token.setter
    def token(self, v): self.inner.token = v

    def __getattr__(self, name):
        fn = getattr(self.inner, name)
        if name != "post_events":
            return fn

        async def call(batch):
            ack = await fn(batch)
            self.skipped += ack.skipped
            self.accepted += ack.accepted
            return ack
        return call


async def test_full_paper_lifecycle_runner_executes_and_control_plane_shows_everything(env, tmp_path):
    holder = {}
    h, rt = await setup(env, tmp_path, client_wrapper=lambda c: holder.setdefault("c", Counting(c)))
    assert (await get(env, h, "/agent"))["state"] == "OFFLINE"
    assert (await env.client.post("/agent/start", headers=h)).json()["desired_state"] == "RUNNING"
    await drive(rt, 45)

    st = await get(env, h, "/agent")
    assert st["state"] == "RUNNING" and st["data_source"] == "DEMO DATA" and st["mode_label"] == "PAPER MODE"
    assert st["runner"]["online"] and st["applied_config_version"] == st["config_version"] and st["strategy"]["version"] == 1

    opps = {o["symbol"]: o for o in await get(env, h, "/opportunities?limit=100")}
    assert opps["DEMO-RUG"]["final_action"] == "REJECT" and "TOP10_CONCENTRATION" in opps["DEMO-RUG"]["final_reason"]
    assert "LIQUIDITY_BELOW_MIN" in opps["DEMO-THIN"]["final_reason"] and "SELL_SIMULATION_FAILED" in opps["DEMO-HONEYPOT"]["final_reason"]
    assert opps["DEMO-CHOP"]["final_action"] in ("WATCH", "REJECT") and "gate failed" in opps["DEMO-CHOP"]["final_reason"]   # weak traction is never bought
    assert all(o["data_label"] == "DEMO DATA" for o in opps.values())

    pos = {p["symbol"]: p for p in await get(env, h, "/positions")}
    assert pos["DEMO-STRONG"]["status"] == "CLOSED" and pos["DEMO-STRONG"]["tiers_hit"] == [0, 1, 2] and pos["DEMO-STRONG"]["exit_reason"] == "TRAILING_STOP"
    assert pos["DEMO-STRONG"]["realized_pnl_usdc"] > 0 and pos["DEMO-CRASH"]["status"] == "CLOSED"

    orders = await get(env, h, "/orders?limit=100")
    assert {o["side"] for o in orders} == {"BUY", "SELL"} and all(o["simulated"] and o["tx_hash"] is None and o["label"] == "PAPER (SIMULATED)" for o in orders)

    buy = next(o for o in orders if o["side"] == "BUY")
    d = await get(env, h, f"/decisions/{buy['decision_id']}")
    assert d["decision"]["final_action"] == "BUY" and d["strategy"]["version"] == 1 and d["strategy"]["config_snapshot"]["min_score"] == 70
    assert {a["stage"] for a in d["risk"]["assessments"]} == {"PRE", "FINAL"} and d["risk"]["wallet_policy"]["allocated_capital_usdc"] == 500
    assert d["execution"][0]["simulated"] and d["what_the_agent_saw"]["is_demo"] and "ORDER_FILLED" in {e["type"] for e in d["events"]}

    pf = await get(env, h, "/portfolio")
    assert pf["label"] == "PAPER" and pf["reported_by_runner"] and pf["starting_cash_usdc"] == 500 and pf["open_positions"] == 0 and pf["realized_pnl_usdc"] != 0
    assert len(pf["snapshots"]) >= 1
    tok = await get(env, h, f"/tokens/{__import__('urllib.parse').parse.quote(opps['DEMO-STRONG']['token_key'], safe='')}")
    assert len(tok["price_series"]) >= 2 and tok["data_label"] == "DEMO DATA"          # MARKET_SNAPSHOT events reached the control plane
    assert rt.store.outbox_size() == 0                                                  # everything was delivered and acknowledged
    assert holder["c"].skipped == 0 and holder["c"].accepted > 100                      # a normal run never has an event rejected by ingestion
    assert (await get(env, h, "/portfolio"))["cash_usdc"] == pytest.approx(rt.portfolio.cash_usdc, abs=1e-6)   # control plane mirrors the runner's cash


async def test_pause_emergency_stop_and_strategy_toggle_reach_the_runner(env, tmp_path):
    h, rt = await setup(env, tmp_path)
    await env.client.post("/agent/start", headers=h)
    await drive(rt, 6)
    assert rt.state == "RUNNING"
    await env.client.post("/agent/pause", headers=h)
    await rt.poll_config_once()
    assert rt.state == "PAUSED" and rt.controls.global_pause and await rt.discover_once() == 0
    await rt.heartbeat_once()
    assert (await get(env, h, "/agent"))["state"] == "PAUSED"

    await env.client.post("/agent/start", headers=h)
    await env.client.put("/strategies/traction_momentum/enabled", json={"enabled": False}, headers=h)
    await rt.poll_config_once()
    assert rt.controls.global_pause and await rt.discover_once() == 0                     # disabled strategy => no entries
    await env.client.put("/strategies/traction_momentum/enabled", json={"enabled": True}, headers=h)

    async with env.app.state.c.sf() as db:
        buys_before = (await db.execute(select(func.count()).select_from(M.Order).where(M.Order.side == "BUY"))).scalar_one()
    await env.client.post("/agent/emergency-stop", json={"enabled": True}, headers=h)
    await rt.poll_config_once()
    assert rt.controls.emergency_stop
    await drive(rt, 45)                                                                    # positions keep being protected, nothing new is bought
    async with env.app.state.c.sf() as db:
        buys_after = (await db.execute(select(func.count()).select_from(M.Order).where(M.Order.side == "BUY"))).scalar_one()
        open_n = (await db.execute(select(func.count()).select_from(M.Position).where(M.Position.status == "OPEN"))).scalar_one()
    assert buys_after == buys_before and open_n == 0
    reasons = [o["final_reason"] for o in await get(env, h, "/opportunities?limit=100")]
    assert any("EMERGENCY_STOP" in r for r in reasons)


async def test_manual_close_from_the_ui_is_executed_by_the_runner(env, tmp_path):
    h, rt = await setup(env, tmp_path)
    await env.client.post("/agent/start", headers=h)
    for _ in range(12):
        await drive(rt, 1)
        if rt.portfolio.open_count():                                                      # type: ignore[union-attr]
            break
    await drive(rt, 1)
    open_pos = [p for p in await get(env, h, "/positions?status=OPEN")]
    assert open_pos
    pid = open_pos[0]["id"]
    q = (await env.client.post(f"/agent/close/{pid}", headers=h)).json()
    assert q["queued"]
    await rt.heartbeat_once()
    await rt.upload_once()
    p = next(x for x in await get(env, h, "/positions") if x["id"] == pid)
    assert p["status"] == "CLOSED" and p["exit_reason"] == "MANUAL"
    async with env.app.state.c.sf() as db:
        assert (await db.get(M.RunnerCommand, q["command_id"])).status == "DONE"
    assert (await env.client.post("/agent/close-all", headers=h)).json()["queued"]


class Flaky:
    """Proxy that can simulate the control plane going away."""
    def __init__(self, inner):
        self.inner, self.down = inner, False

    @property
    def token(self): return self.inner.token
    @token.setter
    def token(self, v): self.inner.token = v

    def __getattr__(self, name):
        fn = getattr(self.inner, name)

        async def call(*a, **k):
            if self.down:
                raise ControlPlaneError(0, "simulated outage")
            return await fn(*a, **k)
        return call


async def test_control_plane_outage_buffers_events_then_delivers_without_duplicates(env, tmp_path):
    holder = {}
    h, rt = await setup(env, tmp_path, client_wrapper=lambda c: holder.setdefault("f", Flaky(c)))
    await env.client.post("/agent/start", headers=h)
    await drive(rt, 5)
    holder["f"].down = True
    await drive(rt, 12)                                                                    # trading continues inside the offline window
    assert rt.store.outbox_size() > 0
    async with env.app.state.c.sf() as db:
        during = (await db.execute(select(func.count()).select_from(M.Decision))).scalar_one()
    holder["f"].down = False
    await drive(rt, 3)
    assert rt.store.outbox_size() == 0
    async with env.app.state.c.sf() as db:
        after = (await db.execute(select(func.count()).select_from(M.Decision))).scalar_one()
        orders = (await db.execute(select(M.Order.idempotency_key))).scalars().all()
        events = (await db.execute(select(M.EventRow.id))).scalars().all()
    assert after > during and len(orders) == len(set(orders)) and len(events) == len(set(events))


async def test_dead_man_switch_end_to_end(env, tmp_path):
    holder = {}
    s = runner_settings(tmp_path, max_offline_s=0.2)
    h, rt = await setup(env, tmp_path, settings=s, client_wrapper=lambda c: holder.setdefault("f", Flaky(c)))
    await env.client.post("/agent/start", headers=h)
    await drive(rt, 4)
    assert rt.state == "RUNNING"
    holder["f"].down = True
    await asyncio.sleep(0.4)
    await rt.poll_config_once()
    assert rt.controls.global_pause and await rt.discover_once() == 0 and "unreachable" in rt.entries_suspended_reason
    holder["f"].down = False
    await rt.poll_config_once()
    await rt.heartbeat_once()
    assert (await get(env, h, "/agent"))["runner"]["entries_suspended_reason"] is None and not rt.controls.global_pause


async def test_runner_restart_resumes_without_double_buying_then_revocation_stops_entries(env, tmp_path):
    h, rt = await setup(env, tmp_path)
    await env.client.post("/agent/start", headers=h)
    for _ in range(12):
        await drive(rt, 1)
        if rt.portfolio.open_count():                                                      # type: ignore[union-attr]
            break
    assert rt.portfolio.open_count() >= 1                                                  # type: ignore[union-attr]
    token = rt.client.token
    rt.store.close()                                                                       # simulate the process dying

    import httpx
    from httpx import ASGITransport
    client2 = ControlPlaneClient("http://t", token, http=httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://t"))
    from app.chains.demo import DemoMarketData
    rt2 = RunnerRuntime(runner_settings(tmp_path), client2, LocalStore(tmp_path / "state.db"), market_data=DemoMarketData(seed=1), wallet=None, chain=None, llm=None)
    await drive(rt2, 45)
    async with env.app.state.c.sf() as db:
        buys = (await db.execute(select(M.Order.token_address).where(M.Order.side == "BUY"))).scalars().all()
    assert len(buys) >= 1 and len(buys) == len(set(buys))                                  # no token was ever bought twice, restart included
    assert all(p["status"] == "CLOSED" for p in await get(env, h, "/positions"))          # ...and finished managing it

    runners = await get(env, h, "/runners")
    assert (await env.client.delete(f"/runners/{runners[0]['id']}", headers=h)).status_code == 200
    with pytest.raises(Revoked):
        await rt2.heartbeat_once()
    assert rt2.revoked and rt2.controls.global_pause and await rt2.discover_once() == 0


async def test_live_cannot_be_enabled_and_the_runner_reports_exactly_why(env, tmp_path):
    cli = CircleCli(exec_fn=lambda argv: (_ for _ in ()).throw(AssertionError("readiness must not shell out")))
    wallet = CircleAgentWalletProvider(cli, "0x" + "ab" * 20, "ARC")
    s = runner_settings(tmp_path, live_enabled=True, wallet_provider="circle_agent_wallet")
    h, rt = await setup(env, tmp_path, settings=s, wallet=wallet)
    await rt.heartbeat_once()
    st = await get(env, h, "/agent")
    live = st["runner"]["live"]
    assert live["available"] is False and live["locally_enabled"] is True and st["runner"]["wallet_provider"] == "circle_agent_wallet"
    joined = " | ".join(live["reasons"])
    assert "schemas are unverified" in joined and "not verified" in joined and "LIVE entries require an AI provider" in joined
    r = await env.client.post("/agent/mode", json={"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}, headers=h)
    blockers = " | ".join(r.json()["detail"]["blockers"])
    assert r.status_code == 409 and "LIVE_TRADING_ENABLED is false" in blockers and "schemas are unverified" in blockers
    assert (await get(env, h, "/auth/me"))["mode"] == "PAPER"
    await env.client.post("/agent/start", headers=h)
    await drive(rt, 6)
    assert rt.state == "RUNNING" and rt.mode_eff.value == "PAPER"                          # PAPER needs no wallet authority at all
