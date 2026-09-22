"""Local Runner behaviour that must hold with NO server at all."""
import json
import os
import stat
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from app.core.errors import IntegrationNotVerified
from app.domain.runner_protocol import Command, ConfigBundle, ConfigPoll, EventAck, HeartbeatResponse
from app.domain.trade import UnsignedTransaction
from app.core.types import Side, TradingMode, WalletCapability
from app.portfolio.controls import ControlState
from app.portfolio.state import PortfolioState
from app.risk.engine import RiskLimits
from app.strategies.traction_momentum import TractionMomentumConfig
from app.wallets.base import LiveReadiness
from runner.client import ControlPlaneError, Revoked
from runner.runtime import RunnerRuntime
from runner.settings import RunnerSettings, apply_ceilings, approval_secret, load_credentials, save_credentials
from runner.store import LocalStore
from runner.wallets import CircleAgentWalletProvider, CircleCli, CliResult, build_wallet_provider, redact, register_wallet_provider
from tests.conftest import NOW, Clock, StaticMarketData, StubProvider, good_market
from tests.test_wallet_live import FakeAutonomousWallet, FakeChain, auth


def bundle(**o) -> ConfigBundle:
    d = dict(version=1, runner_id="r1", mode="PAPER", desired_state="RUNNING", emergency_stop=False, strategy=TractionMomentumConfig().model_dump(mode="json"),
             strategies_enabled=["traction_momentum"], risk_limits=RiskLimits().model_dump(mode="json"), wallet_policy=None,
             controls=ControlState().model_dump(mode="json"), issued_at=datetime.now(timezone.utc))
    d.update(o)
    return ConfigBundle(**d)


class FakeClient:
    def __init__(self, b: ConfigBundle):
        self.bundle, self.fail, self.revoked, self.events, self.acks, self.hbs, self.commands, self.token = b, False, False, [], [], [], [], "rt_x"

    def _check(self):
        if self.revoked:
            raise Revoked(401, "revoked")
        if self.fail:
            raise ControlPlaneError(0, "down")

    async def get_config(self, since, wait):
        self._check()
        new = self.bundle.version > since
        return ConfigPoll(changed=new, bundle=self.bundle if new else None)

    async def heartbeat(self, hb):
        self._check()
        self.hbs.append(hb)
        cmds, self.commands = self.commands, []
        return HeartbeatResponse(server_time=datetime.now(timezone.utc), desired_config_version=self.bundle.version, commands=cmds)

    async def post_events(self, batch):
        self._check()
        self.events += batch.events
        return EventAck(last_seq=max(e.seq for e in batch.events), accepted=len(batch.events), skipped=0)

    async def ack_command(self, cid, status, detail=""):
        self._check()
        self.acks.append((cid, status, detail))


class Mono:
    t = 1000.0

    def __call__(self):
        return self.t


def make_rt(tmp_path, client, *, market=None, settings=None, store=None, **kw):
    s = settings or RunnerSettings(_env_file=None, state_dir=tmp_path, reevaluate_after_s=0.0, snapshot_interval_s=0.0, market_snapshot_every_s=0.0, max_offline_s=60.0)
    clock, mono = kw.pop("clock", Clock()), kw.pop("mono", Mono())
    rt = RunnerRuntime(s, client, store or LocalStore(tmp_path / "state.db"), market_data=market or StaticMarketData(good_market()),
                       wallet=kw.pop("wallet", None), chain=kw.pop("chain", None), llm=kw.pop("llm", None), clock=clock, mono=mono, **kw)
    return rt


# ------------------------------------------------------------------ local ceilings
async def test_local_ceilings_beat_a_hostile_control_plane(tmp_path):
    hostile = RiskLimits(max_trade_usdc=10_000, max_position_usdc=10_000, max_daily_loss_usdc=1e6, max_total_exposure_usdc=1e7, max_open_positions=999,
                         max_slippage_pct=20, min_liquidity_usdc=1, allowed_chains={"arc", "evil"})
    rt = make_rt(tmp_path, FakeClient(bundle(risk_limits=hostile.model_dump(mode="json"))))
    await rt.poll_config_once()
    L = rt.effective_limits
    assert (L.max_trade_usdc, L.max_position_usdc, L.max_daily_loss_usdc, L.max_open_positions, L.max_slippage_pct) == (25, 50, 50, 5, 2)
    assert L.min_liquidity_usdc == 10_000 and L.allowed_chains == {"arc"} and L.max_total_exposure_usdc == 250
    assert await rt.discover_once() == 1
    orders = rt.engine.executor.orders                                       # type: ignore[union-attr]
    assert orders[0].requested_amount_usdc == pytest.approx(25)                # never the 10,000 the server asked for


def test_apply_ceilings_is_a_pure_min_max():
    s = RunnerSettings(_env_file=None, ceiling_max_trade_usdc=5, ceiling_min_liquidity_usdc=50_000, ceiling_allowed_chains="arc,bnb")
    out = apply_ceilings(RiskLimits(max_trade_usdc=25, min_liquidity_usdc=10_000, allowed_chains={"arc"}), s.ceilings)
    assert out.max_trade_usdc == 5 and out.min_liquidity_usdc == 50_000 and out.allowed_chains == {"arc"}
    tight = apply_ceilings(RiskLimits(max_trade_usdc=1), s.ceilings)
    assert tight.max_trade_usdc == 1                                           # a stricter server value is kept


# ------------------------------------------------------------------ LIVE needs BOTH keys and never falls back to paper
async def test_live_is_blocked_locally_even_when_the_server_asks_for_it(tmp_path):
    rt = make_rt(tmp_path, FakeClient(bundle(mode="LIVE")))
    await rt.poll_config_once()
    assert rt.state == "LIVE_BLOCKED" and rt.engine is None and rt.portfolio is None       # no silent paper fallback
    assert await rt.discover_once() == 0
    await rt.heartbeat_once()
    hb = rt.client.hbs[-1]                                                                  # type: ignore[attr-defined]
    assert hb.state == "LIVE_BLOCKED" and not hb.live.available
    assert any("disabled locally" in r for r in hb.live.reasons) and any("not verified" in r for r in hb.live.reasons)


async def test_live_stays_blocked_with_circle_provider_because_it_is_unverified(tmp_path):
    cli = CircleCli(exec_fn=lambda argv: (_ for _ in ()).throw(AssertionError("must not shell out during readiness")))
    wallet = CircleAgentWalletProvider(cli, "0x" + "ab" * 20, "ARC")
    s = RunnerSettings(_env_file=None, state_dir=tmp_path, live_enabled=True)
    rt = make_rt(tmp_path, FakeClient(bundle(mode="LIVE")), settings=s, wallet=wallet, llm=StubProvider(), chain=FakeChain())
    await rt.poll_config_once()
    live = rt.live
    assert rt.state == "LIVE_BLOCKED" and not live.available and live.locally_enabled
    joined = " | ".join(live.reasons)
    assert "schemas are unverified" in joined and "transaction id to a chain hash" in joined and "spending caps" in joined


async def test_the_execution_layer_is_provider_agnostic(tmp_path):
    """A new wallet provider plugs in via the registry; strategy/risk/engine classes are untouched."""
    class FutureWallet(FakeAutonomousWallet):
        name = "future_wallet"
        async def live_readiness(self):
            return LiveReadiness(available=True, provider=self.name, session_authorized=True, address="0xW")
    register_wallet_provider("future_wallet", lambda s: FutureWallet(auth()))
    s = RunnerSettings.model_construct(**{**RunnerSettings(_env_file=None, state_dir=tmp_path).model_dump(), "wallet_provider": "future_wallet"})
    assert build_wallet_provider(s).name == "future_wallet"                              # type: ignore[union-attr]
    paper = make_rt(tmp_path / "p", FakeClient(bundle()))
    (tmp_path / "p").mkdir(exist_ok=True)
    live = make_rt(tmp_path, FakeClient(bundle(mode="LIVE")), settings=RunnerSettings(_env_file=None, state_dir=tmp_path, live_enabled=True),
                   wallet=FutureWallet(auth()), llm=StubProvider(), chain=FakeChain())
    await paper.poll_config_once()
    await live.poll_config_once()
    assert live.state != "LIVE_BLOCKED" and type(live.engine.executor).__name__ == "ArcExecutionEngine"     # type: ignore[union-attr]
    assert type(paper.engine.executor).__name__ == "PaperExecutionEngine"                                     # type: ignore[union-attr]
    for attr in ("pipeline", "exits"):
        assert type(getattr(live.engine, attr)) is type(getattr(paper.engine, attr))                          # same strategy/risk/exit code


# ------------------------------------------------------------------ cloud-worker first heartbeat
async def test_first_heartbeat_after_direct_bundle_apply_reports_running(tmp_path):
    # Shared cloud workers build a fresh Runtime per tenant cycle and call
    # apply_bundle() directly, so last_contact is initially None. The
    # heartbeat itself must establish contact rather than being reported as
    # PAUSED for one whole cycle.
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client)
    await rt.apply_bundle(client.bundle)
    assert rt.last_contact is None
    assert rt.state == "PAUSED"  # fail-closed before any successful contact

    await rt.heartbeat_once()

    assert client.hbs[-1].state == "RUNNING"
    assert rt.state == "RUNNING"
    assert not rt.controls.global_pause
    assert rt.entries_suspended_reason is None


# ------------------------------------------------------------------ dead-man switch
async def test_no_new_entries_when_the_control_plane_is_unreachable_but_exits_keep_working(tmp_path):
    clock, mono, market = Clock(), Mono(), StaticMarketData(good_market())
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client, market=market, clock=clock, mono=mono)
    await rt.poll_config_once()
    assert rt.state == "RUNNING" and await rt.discover_once() == 1
    pos = rt.portfolio.open_positions()[0]                                                                    # type: ignore[union-attr]
    client.fail = True
    mono.t += 61                                                                                              # control plane silent > max_offline_s
    assert await rt.poll_config_once() is False
    assert rt.controls.global_pause and "unreachable" in (rt.entries_suspended_reason or "") and rt.state == "PAUSED"
    market.set(good_market(token_address="0x2222222222222222222222222222222222222222"))
    assert await rt.discover_once() == 0                                                                      # no NEW entries
    market.set(good_market(price=0.5))                                                                        # crash: hard stop must still fire offline
    await rt.monitor_once()
    assert not pos.is_open and pos.exit_reason.value == "HARD_STOP"
    client.fail = False
    await rt.poll_config_once()
    await rt.heartbeat_once()
    assert not rt.controls.global_pause and rt.entries_suspended_reason is None                               # back online: entries allowed again


async def test_pause_stop_emergency_and_disabled_strategy_all_halt_entries(tmp_path):
    for over, expect_state in ((dict(desired_state="PAUSED"), "PAUSED"), (dict(desired_state="STOPPED"), "STOPPED"),
                               (dict(strategies_enabled=[]), "PAUSED")):
        sub = tmp_path / str(sorted(over)[0]) ; sub.mkdir(exist_ok=True)
        rt = make_rt(sub, FakeClient(bundle(**over)))
        await rt.poll_config_once()
        assert rt.state == expect_state and await rt.discover_once() == 0
    sub = tmp_path / "em"; sub.mkdir()
    rt = make_rt(sub, FakeClient(bundle(emergency_stop=True)))
    await rt.poll_config_once()
    assert await rt.discover_once() == 1 and rt.engine.executor.orders == []                                  # type: ignore[union-attr]  # evaluated, but vetoed


# ------------------------------------------------------------------ durable outbox, restart safety, command dedupe
async def test_outbox_coalesces_updates_survives_restart_and_drains_after_an_outage(tmp_path):
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client)
    await rt.poll_config_once()
    await rt.discover_once()
    for _ in range(3):
        await rt.monitor_once()
    kinds = [e["type"] for _, e in rt.store.outbox_pending(500)]
    assert kinds.count("POSITION_UPDATED") == 1 and "DECISION_RECORDED" in kinds and "ORDER_FILLED" in kinds       # coalesced
    pending = rt.store.outbox_size()
    client.fail = True
    assert await rt.upload_once() == 0 and rt.store.outbox_size() == pending                                      # outage: nothing lost
    rt.store.close()
    reopened = LocalStore(tmp_path / "state.db")                                                                  # process restart
    assert reopened.outbox_size() == pending
    rt2 = make_rt(tmp_path, client, store=reopened)
    client.fail = False
    assert await rt2.upload_once() == pending and reopened.outbox_size() == 0
    seqs = [e.seq for e in client.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_restart_restores_the_portfolio_and_never_buys_twice(tmp_path):
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client)
    await rt.poll_config_once()
    await rt.discover_once()
    cash, pid = rt.portfolio.cash_usdc, rt.portfolio.open_positions()[0].id                                        # type: ignore[union-attr]
    rt.store.close()
    rt2 = make_rt(tmp_path, client, store=LocalStore(tmp_path / "state.db"))
    await rt2.poll_config_once()
    assert rt2.portfolio.cash_usdc == pytest.approx(cash) and [p.id for p in rt2.portfolio.open_positions()] == [pid]   # type: ignore[union-attr]
    assert await rt2.discover_once() == 0                                                                          # already holds it
    assert len(rt2.engine.executor.orders) == 0                                                                    # type: ignore[union-attr]


async def test_daily_loss_survives_restart(tmp_path):
    pf = PortfolioState(900, TradingMode.PAPER)
    pf.day, pf.realized_today, pf.realized_total = NOW.date(), -49.0, -49.0
    back = PortfolioState.from_dict(pf.to_dict())
    assert back.daily_pnl(NOW) == pytest.approx(-49.0) and back.cash_usdc == 900


async def test_remote_close_commands_execute_exactly_once(tmp_path):
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client)
    await rt.poll_config_once()
    await rt.discover_once()
    pid = rt.portfolio.open_positions()[0].id                                                                      # type: ignore[union-attr]
    client.commands = [Command(id="c1", type="CLOSE_POSITION", payload={"position_id": pid})]
    await rt.heartbeat_once()
    assert rt.portfolio.positions[pid].exit_reason.value == "MANUAL" and client.acks[-1][:2] == ("c1", "DONE")     # type: ignore[union-attr]
    n_orders = len(rt.engine.executor.orders)                                                                      # type: ignore[union-attr]
    client.commands = [Command(id="c1", type="CLOSE_POSITION", payload={"position_id": pid})]                      # redelivered (ack was lost)
    await rt.heartbeat_once()
    assert len(rt.engine.executor.orders) == n_orders and client.acks[-1][2] == "already executed"                 # type: ignore[union-attr]
    client.commands = [Command(id="c2", type="CLOSE_POSITION", payload={"position_id": "nope"})]
    await rt.heartbeat_once()
    assert client.acks[-1][:2] == ("c2", "DONE") and "already closed" in client.acks[-1][2]


async def test_revoked_runner_stops_taking_new_positions(tmp_path):
    client = FakeClient(bundle())
    rt = make_rt(tmp_path, client)
    await rt.poll_config_once()
    client.revoked = True
    with pytest.raises(Revoked):
        await rt.heartbeat_once()
    assert rt.revoked and rt.controls.global_pause and await rt.discover_once() == 0
    await rt.monitor_once()                                                                                        # protection still runs


def test_store_idempotency_is_atomic_and_credential_files_are_private(tmp_path):
    st = LocalStore(tmp_path / "s.db")
    assert st.idem_claim("k") and not st.idem_claim("k")
    st.idem_release("k")
    assert st.idem_claim("k")
    p = save_credentials(tmp_path, "https://x", "r1", "rt_secret")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and load_credentials(tmp_path)["token"] == "rt_secret"
    k = approval_secret(tmp_path)
    assert len(k) == 64 and approval_secret(tmp_path) == k and stat.S_IMODE(os.stat(tmp_path / "approval.key").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "s.db").st_mode) == 0o600


# ------------------------------------------------------------------ Circle CLI adapter (argv verified against `circle ... --help`, CLI 1.1.3)
ADDR, USDC = "0x" + "ab" * 20, "0x3600000000000000000000000000000000000000"


def test_circle_cli_argv_matches_the_documented_syntax():
    c = CircleCli()
    assert c.status() == ["wallet", "status", "--type", "agent", "--output", "json"]
    assert c.wallet_list("ARC") == ["wallet", "list", "--chain", "ARC", "--type", "agent", "--output", "json"]
    assert c.balance(ADDR, "ARC") == ["wallet", "balance", "--address", ADDR, "--chain", "ARC", "--output", "json"]
    assert c.limit(ADDR, "ARC") == ["wallet", "limit", "--address", ADDR, "--chain", "ARC", "--output", "json"]
    assert c.budget(ADDR) == ["wallet", "limit", "budget", "--address", ADDR, "--output", "json"]
    assert c.execute("approve(address,uint256)", ["0x" + "cd" * 20, "1000000"], USDC, ADDR, "ARC", idempotency_key="k-1", estimate=True) == [
        "wallet", "execute", "approve(address,uint256)", "0x" + "cd" * 20, "1000000", "--contract", USDC, "--address", ADDR, "--chain", "ARC",
        "--amount", "0", "--idempotency-key", "k-1", "--estimate", "--output", "json"]


@pytest.mark.parametrize("call", [
    lambda c: c.execute("approve(address,uint256); rm -rf /", [], USDC, ADDR, "ARC"),
    lambda c: c.execute("approve(address,uint256)", ["--contract"], USDC, ADDR, "ARC"),          # option injection
    lambda c: c.execute("approve(address,uint256)", ["a\nb"], USDC, ADDR, "ARC"),
    lambda c: c.execute("approve(address,uint256)", [""], USDC, ADDR, "ARC"),
    lambda c: c.execute("f()", [], "0xnothex", ADDR, "ARC"),
    lambda c: c.execute("f()", [], USDC, ADDR, "ARC;ls"),
    lambda c: c.execute("f()", [], USDC, ADDR, "ARC", amount="1e18"),
    lambda c: c.execute("f()", [], USDC, ADDR, "ARC", idempotency_key="bad key!"),
    lambda c: c.balance("0x12", "ARC"),
])
def test_circle_cli_rejects_unsafe_arguments(call):
    with pytest.raises(ValueError):
        call(CircleCli())


def test_circle_cli_cannot_log_in_accept_terms_or_change_limits():
    for forbidden in ("login", "logout", "terms", "accept_terms", "limit_set", "limit_reset", "set_limit", "reset_limit", "otp"):
        assert not hasattr(CircleCli, forbidden)


async def test_circle_cli_run_is_shell_free_and_never_accepts_terms(tmp_path, monkeypatch):
    fake = tmp_path / "circle"
    fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({'argv': sys.argv[1:], 'terms': os.environ.get('CIRCLE_ACCEPT_TERMS')}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CIRCLE_ACCEPT_TERMS", "1")
    r = await CircleCli(str(fake)).run(CircleCli().execute("f(bytes)", ["0x11; echo pwned $(id)"], USDC, ADDR, "ARC"))
    assert r.returncode == 0 and r.json["terms"] is None
    assert "0x11; echo pwned $(id)" in r.json["argv"]                                       # one literal argument: no shell
    assert (await CircleCli(str(tmp_path / "missing")).run(["x"])).returncode == 127


async def test_circle_provider_is_disabled_and_lists_why():
    w = CircleAgentWalletProvider(CircleCli(exec_fn=lambda a: (_ for _ in ()).throw(AssertionError("no shelling out"))), ADDR, "ARC")
    assert w.capabilities() == set() and not w.verified and WalletCapability.AUTONOMOUS_DELEGATED not in w.capabilities()
    r = await w.live_readiness()
    assert not r.available and len(r.reasons) >= 3
    tx = UnsignedTransaction(chain="arc", chain_id=5042, to=USDC, data="0x", token_address=USDC, side=Side.BUY, amount_usdc=1, min_out=1, slippage_pct=1,
                             deadline=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        w.build_execute_argv(tx)                                                            # raw calldata is not enough for Circle
    tx2 = tx.model_copy(update={"function_signature": "transfer(address,uint256)", "params": [ADDR, "5"]})
    assert w.build_execute_argv(tx2, estimate=True)[:3] == ["wallet", "execute", "transfer(address,uint256)"]
    with pytest.raises(IntegrationNotVerified):
        await w._submit(tx2, auth())
    with pytest.raises(IntegrationNotVerified):
        await w.revoke()


async def test_circle_verify_captures_real_outputs_read_only_and_redacts_secrets(tmp_path, monkeypatch):
    import runner.cli as cli_mod
    seen = []

    async def fake_exec(argv):
        seen.append(argv)
        return CliResult(0, "{}", "", {"status": "ok", "session_token": "SUPERSECRET", "nested": {"otp": "123456", "address": ADDR}})
    monkeypatch.setattr(cli_mod, "CircleCli", lambda path: CircleCli(path, exec_fn=fake_exec))
    s = RunnerSettings(_env_file=None, state_dir=tmp_path, circle_wallet_address=ADDR)
    assert await cli_mod.cmd_circle_verify(s, Namespace(include_estimate=True)) == 0
    files = list(tmp_path.glob("circle-capture-*.json"))
    assert len(files) == 1 and stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    text = files[0].read_text()
    assert "SUPERSECRET" not in text and "123456" not in text and "[REDACTED]" in text and ADDR in text
    flat = [tok for argv in seen for tok in argv]
    assert not {"login", "logout", "terms", "set", "reset", "accept"} & set(flat)            # read-only: never touches auth or limits
    assert any("--estimate" in a for a in seen) and not any(a[1:3] == ["wallet", "execute"] and "--estimate" not in a for a in seen)   # execute only ever with --estimate
    assert redact({"a": {"private_key": "x", "ok": 1}}) == {"a": {"private_key": "[REDACTED]", "ok": 1}}


# ------------------------------------------------------------------ architecture guarantees
def test_the_runner_imports_without_any_server_only_dependency():
    code = ("import sys, importlib\n"
            "class B:\n"
            "    BAD=('fastapi','sqlalchemy','asyncpg','alembic','redis','bcrypt','jwt','eth_account','uvicorn')\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] in self.BAD: raise ImportError(n)\n"
            "sys.meta_path.insert(0, B())\n"
            "for m in ('runner.runtime','runner.cli','runner.wallets'): importlib.import_module(m)\n"
            "leaked=[m for m in sys.modules if m.startswith(('app.db','app.api','app.infra','app.services.control','app.services.ingest'))]\n"
            "assert not leaked, leaked\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.returncode == 0, r.stderr


def test_the_runner_never_listens_for_inbound_connections():
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runner")
    src = "\n".join(open(os.path.join(root, f)).read() for f in os.listdir(root) if f.endswith(".py"))
    for needle in ("uvicorn", "start_server", ".bind(", "socketserver", "http.server", "FastAPI("):
        assert needle not in src


def test_the_control_plane_holds_no_trading_or_signing_code():
    app = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app")
    routes = "\n".join(open(os.path.join(app, "api", f)).read() for f in os.listdir(os.path.join(app, "api")) if f.endswith(".py"))
    for needle in ("PaperExecutionEngine", "ArcExecutionEngine", "TradingEngine", "TradeApprover", "sign_and_send", "private_key", "personal_sign("):
        assert needle not in routes, needle
    assert not os.path.exists(os.path.join(app, "services", "runtime.py")) and not os.path.exists(os.path.join(app, "services", "manager.py"))
