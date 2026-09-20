from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.chains.arc.adapter import ArcAdapter, ArcMarketData
from app.chains.base import ChainAdapter, ChainHealth, FillDetails, SimulationResult, TxReceipt
from app.chains.evm import EvmRpcClient
from app.core.errors import (ApprovalError, DataUnavailable, IntegrationNotVerified, PaperWalletCannotSubmit,
                             PolicyViolationError)
from app.core.types import OrderStatus, Side, TradingMode, WalletCapability
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction
from app.events.bus import InMemoryIdempotencyStore
from app.execution.live import ArcExecutionEngine, LiveSettings
from app.launchpads.registry import LaunchpadDescriptor, LaunchpadRegistry
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover
from app.wallets.base import SubmissionResult, WalletAccount, WalletAuthorization, WalletPolicy, WalletProvider
from app.wallets.providers import AgentWalletProvider, BrowserWalletProvider, PaperWalletProvider
from tests.conftest import SECRET, TOKEN

ROUTER = "0xRouterAllowed000000000000000000000000000001"


def now():
    return datetime.now(timezone.utc)


def policy(**o):
    d = dict(allocated_capital_usdc=500, max_trade_usdc=25, max_position_usdc=50, max_daily_loss_usdc=50,
             max_open_positions=5, max_slippage_pct=2, min_liquidity_usdc=10_000, allowed_chains={"arc"},
             allowed_routers={ROUTER})
    d.update(o)
    return WalletPolicy(**d)


def auth(cap=WalletCapability.AUTONOMOUS_DELEGATED, **o):
    d = dict(wallet_id="w1", provider="fake", capability=cap, policy=policy(), granted_at=now() - timedelta(hours=1))
    d.update(o)
    return WalletAuthorization(**d)


def tx(**o):
    d = dict(chain="arc", chain_id=5042, to=ROUTER, data="0x00", token_address=TOKEN, side=Side.BUY, amount_usdc=10,
             min_out=9, slippage_pct=1.0, deadline=now() + timedelta(minutes=2))
    d.update(o)
    return UnsignedTransaction(**d)


class FakeAutonomousWallet(WalletProvider):
    """TEST DOUBLE ONLY: stands in for a verified delegated wallet."""
    name, verified = "fake", True

    def __init__(self, authorization, balance=1000.0):
        self._auth, self._bal, self.submitted = authorization, balance, []

    def capabilities(self): return {WalletCapability.AUTONOMOUS_DELEGATED}
    async def get_account(self): return WalletAccount(id="w1", provider="fake", address="0xW", chain="arc", kind="agent")
    async def get_usdc_balance(self): return self._bal
    async def get_authorization(self): return self._auth
    async def revoke(self): self._auth = None

    async def _submit(self, t, a):
        self.submitted.append(t)
        return SubmissionResult(status="SUBMITTED", tx_hash="0xhash")


class FakeChain(ChainAdapter):
    """TEST DOUBLE ONLY: stands in for a verified chain integration."""
    name, chain_id, live_trading_verified = "arc", 5042, True

    def __init__(self, receipt_status=1, receipt=True, router=ROUTER, liquidity=100_000.0, age=0):
        self.receipt_status, self.receipt, self.router, self.liq, self.age = receipt_status, receipt, router, liquidity, age

    def router_allowlist(self): return {ROUTER.lower()}
    async def health(self): return ChainHealth(ok=True, chain_id=5042)

    async def get_receipt(self, h):
        return TxReceipt(tx_hash=h, status=self.receipt_status) if self.receipt else None

    async def quote(self, req):
        t = now() - timedelta(seconds=self.age)
        return Quote(side=req.side, token_address=req.token_address, amount_in=req.amount_usdc or 0, expected_out=10,
                     price=1.0, price_impact_pct=0.1, fee_usdc=0.03, router_address=self.router,
                     pool_liquidity_usdc=self.liq, quoted_at=t, expires_at=t + timedelta(seconds=30), source="fake")

    async def build_swap_tx(self, q, req, deadline):
        return tx(to=q.router_address, amount_usdc=req.amount_usdc or 0, slippage_pct=req.max_slippage_pct, deadline=deadline)

    async def simulate(self, t): return SimulationResult(ok=True)
    async def parse_fill(self, r, q): return FillDetails(filled_quantity=9.9, avg_price=1.01, fee_usdc=0.03)
    async def wait_for_receipt(self, h, timeout_s, poll_s=0.0):
        return await self.get_receipt(h)


def live_req(key="L1", **o):
    d = dict(idempotency_key=key, mode=TradingMode.LIVE, chain="arc", token_address=TOKEN, side=Side.BUY,
             amount_usdc=10, max_slippage_pct=2.0, reference_price=1.0)
    d.update(o)
    return TradeRequest(**d)


def make_live(chain=None, wallet=None, enabled=True):
    approver = TradeApprover(SECRET)
    p = PortfolioState(0, TradingMode.LIVE)
    eng = ArcExecutionEngine(chain or FakeChain(), wallet or FakeAutonomousWallet(auth()), approver, p,
                             InMemoryIdempotencyStore(), LiveSettings(live_trading_enabled=enabled))
    return eng, approver, p


# ---------------- wallet policy ----------------
@pytest.mark.parametrize("a,t,expect", [
    (None, tx(), "NO_AUTHORIZATION"),
    (auth(revoked_at=now() - timedelta(minutes=1)), tx(), "AUTHORIZATION_INACTIVE_OR_REVOKED"),
    (auth(expires_at=now() - timedelta(minutes=1)), tx(), "AUTHORIZATION_INACTIVE_OR_REVOKED"),
    (auth(), tx(to="0xEvilRouter"), "ROUTER_NOT_ALLOWLISTED"),
    (auth(), tx(amount_usdc=26), "EXCEEDS_MAX_TRADE"),
    (auth(), tx(slippage_pct=5), "EXCEEDS_MAX_SLIPPAGE"),
    (auth(), tx(chain="other"), "CHAIN_NOT_IN_POLICY"),
    (auth(), tx(chain_id=1), "CHAIN_ID_MISMATCH"),
    (auth(), tx(built_by="llm"), "TX_NOT_BUILT_BY_ADAPTER"),
    (auth(policy=policy(allowed_routers=set())), tx(), "ROUTER_NOT_ALLOWLISTED"),  # empty allowlist == deny all
])
async def test_wallet_layer_blocks_policy_violations(a, t, expect):
    w = FakeAutonomousWallet(a)
    with pytest.raises(PolicyViolationError) as e:
        await w.submit(t, now(), expected_chain_id=5042)
    assert expect in e.value.violations and not w.submitted


async def test_valid_submission_passes_and_revocation_stops_it():
    w = FakeAutonomousWallet(auth())
    assert (await w.submit(tx(), now(), 5042)).tx_hash == "0xhash"
    await w.revoke()
    with pytest.raises(PolicyViolationError):
        await w.submit(tx(), now(), 5042)


async def test_paper_wallet_cannot_submit_and_browser_wallet_never_signs():
    with pytest.raises(PaperWalletCannotSubmit):
        await PaperWalletProvider(100).submit(tx(), now())
    acct = WalletAccount(id="b", provider="browser_wallet", address="0xUser", chain="arc", kind="browser")

    async def bal(_): return 42.0
    bw = BrowserWalletProvider(acct, auth(cap=WalletCapability.PER_TRADE_SIGNING), bal)
    r = await bw.submit(tx(), now(), 5042)
    assert r.status == "PENDING_SIGNATURE" and r.tx_hash is None            # user's wallet must sign
    assert bw.record_signed(r.signing_request_id, "0xuserhash").status == "SIGNED"
    st = await bw.status()
    assert st.label == "Explicit per-trade signing" and not st.autonomous


async def test_agent_wallet_is_disabled_until_verified():
    w = AgentWalletProvider()
    assert not w.verified and w.capabilities() == set()
    with pytest.raises(IntegrationNotVerified):
        await w.get_authorization()
    assert (await w.status()).label == "Paper trading only"


# ---------------- live execution safety ----------------
async def test_live_happy_path_records_hash_and_updates_portfolio():
    eng, ap, port = make_live()
    port.cash_usdc = 100
    r = await eng.buy(ap.approve_exit(live_req()))
    assert r.status == OrderStatus.FILLED and r.tx_hash == "0xhash" and not r.simulated and r.mode == TradingMode.LIVE
    assert port.open_count() == 1


@pytest.mark.parametrize("kw,err", [
    (dict(enabled=False), "LIVE_TRADING_DISABLED"),
    (dict(chain=FakeChain(router="0xEvil")), "ROUTER_NOT_ALLOWLISTED"),
    (dict(chain=FakeChain(age=60)), "QUOTE_STALE"),
    (dict(chain=FakeChain(liquidity=500)), "QUOTE_LIQUIDITY_BELOW_POLICY_MIN"),
    (dict(wallet=FakeAutonomousWallet(auth(revoked_at=now() - timedelta(seconds=1)))), "WALLET_AUTHORIZATION"),
    (dict(wallet=FakeAutonomousWallet(None)), "WALLET_AUTHORIZATION"),
    (dict(wallet=FakeAutonomousWallet(auth(), balance=1.0)), "INSUFFICIENT_WALLET_BALANCE"),
])
async def test_live_blocks_when_any_check_fails(kw, err):
    eng, ap, _ = make_live(**kw)
    r = await eng.buy(ap.approve_exit(live_req()))
    assert r.status == OrderStatus.REJECTED and err in r.error and r.tx_hash is None


async def test_live_rejects_forged_paper_and_oversize_requests():
    eng, ap, _ = make_live()
    forged = TradeApprover("another-secret-0123456789abcdef").approve_exit(live_req())
    assert (await eng.buy(forged)).status == OrderStatus.REJECTED
    assert (await eng.buy(ap.approve_exit(live_req("L2", mode=TradingMode.PAPER)))).status == OrderStatus.REJECTED
    assert "EXCEEDS_POLICY_MAX_TRADE" in (await eng.buy(ap.approve_exit(live_req("L3", amount_usdc=30)))).error


async def test_live_unverified_arc_adapter_refuses_to_trade():
    rpc = EvmRpcClient(["http://x"], transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"result": "0x13b2"})))
    eng, ap, _ = make_live(chain=ArcAdapter(rpc, router_allowlist={ROUTER}))
    r = await eng.buy(ap.approve_exit(live_req()))
    assert r.status == OrderStatus.REJECTED and "CHAIN_LIVE_INTEGRATION_NOT_VERIFIED" in r.error


async def test_live_revert_timeout_and_duplicates():
    eng, ap, port = make_live(chain=FakeChain(receipt_status=0))
    r = await eng.buy(ap.approve_exit(live_req()))
    assert r.status == OrderStatus.FAILED and r.tx_hash == "0xhash" and port.open_count() == 0
    eng, ap, port = make_live(chain=FakeChain(receipt=False))
    t = await eng.buy(ap.approve_exit(live_req()))
    assert t.status == OrderStatus.TIMEOUT and port.open_count() == 0        # unknown outcome, nothing assumed
    again = await eng.buy(ap.approve_exit(live_req()))                        # blind retry must not double-submit
    assert again.status == OrderStatus.REJECTED and "DUPLICATE_ORDER" in again.error


async def test_live_per_trade_signing_returns_pending():
    acct = WalletAccount(id="b", provider="browser_wallet", address="0xU", chain="arc", kind="browser")

    async def bal(_): return 500.0
    bw = BrowserWalletProvider(acct, auth(cap=WalletCapability.PER_TRADE_SIGNING), bal)
    eng, ap, port = make_live(wallet=bw)
    r = await eng.buy(ap.approve_exit(live_req()))
    assert r.status == OrderStatus.PENDING_SIGNATURE and r.signing_request_id and r.tx_hash is None


# ---------------- Arc adapter (RPC) / launchpads ----------------
def rpc_transport(chain_hex="0x13b2", fail_first=False):
    calls = {"n": 0}

    def h(req: httpx.Request):
        calls["n"] += 1
        if fail_first and req.url.host == "primary":
            return httpx.Response(503)
        m = req.read().decode()
        result = chain_hex if "eth_chainId" in m else "0x64"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
    return httpx.MockTransport(h)


async def test_arc_health_and_chain_id_check_and_failover():
    ok = await ArcAdapter(EvmRpcClient(["http://primary"], transport=rpc_transport())).health()
    assert ok.ok and ok.chain_id == 5042 and ok.block_number == 100
    bad = await ArcAdapter(EvmRpcClient(["http://primary"], transport=rpc_transport("0x1"))).health()
    assert not bad.ok and "mismatch" in bad.detail
    fo = EvmRpcClient(["http://primary", "http://backup"], transport=rpc_transport(fail_first=True), backoff_s=0)
    assert (await ArcAdapter(fo).health()).ok
    down = await ArcAdapter(EvmRpcClient(["http://primary"], transport=rpc_transport(fail_first=True), backoff_s=0)).health()
    assert not down.ok


async def test_arc_never_fabricates_market_data_or_quotes():
    with pytest.raises(DataUnavailable):
        await ArcMarketData().get_market_state(TOKEN)
    with pytest.raises(DataUnavailable):
        await ArcMarketData().discover_tokens()
    rpc = EvmRpcClient(["http://primary"], transport=rpc_transport())
    with pytest.raises(IntegrationNotVerified):
        await ArcAdapter(rpc).quote(live_req())


def test_launchpad_registry_ships_empty_and_cannot_enable_unverified():
    assert LaunchpadRegistry().all() == []
    with pytest.raises(ValueError):
        LaunchpadDescriptor(name="x", chain="arc", enabled=True)
    with pytest.raises(ValueError):
        LaunchpadDescriptor(name="x", chain="arc", enabled=True, verified=True, mainnet_live=True)  # no factory
    ok = LaunchpadDescriptor(name="x", chain="arc", enabled=True, verified=True, mainnet_live=True, factory_address="0x1")
    reg = LaunchpadRegistry()
    reg.register(ok)
    assert reg.enabled("arc") == [ok]
