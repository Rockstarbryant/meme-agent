import httpx
import pytest

from app.chains.arc import deployments
from app.chains.arc.adapter import ArcAdapter
from app.chains.arc.network import (ARC_MAINNET, ARC_TESTNET, EVM_DIFFERENCES_TO_HANDLE, NATIVE_SENTINELS, USDC_ERC20_ADDRESS,
                                    format_usdc, is_usdc_or_native, wallet_chain_params)
from app.chains.base import ChainAdapter, TxReceipt
from app.chains.evm import EvmRpcClient
from app.config import Settings
from app.core.types import RiskDecision, TradingMode
from app.execution.live import LiveSettings
from app.launchpads.arc_candidates import POOLS, arc_candidates
from app.launchpads.registry import LaunchpadDescriptor
from tests.conftest import good_market
from tests.test_strategy_risk import assess, vetoes


def test_network_constants_match_packet_and_circle_skill():
    m, t = ARC_MAINNET, ARC_TESTNET
    assert (m.chain_id, hex(m.chain_id)) == (5042, "0x13b2") and (t.chain_id, hex(t.chain_id)) == (5042002, "0x4cef52")
    assert m.rpc_url == "https://rpc.mainnet.arc.io" and m.ws_url == "wss://rpc.mainnet.arc.io" and m.explorer_url == "https://explorer.arc.io"
    assert t.rpc_url == "https://rpc.testnet.arc.io" and t.explorer_url == "https://explorer.testnet.arc.io"
    assert m.cctp_domain == t.cctp_domain == 26 and (m.sdk_chain, t.sdk_chain) == ("Arc", "Arc_Testnet")
    assert USDC_ERC20_ADDRESS == "0x3600000000000000000000000000000000000000"
    assert format_usdc(12_345_678) == pytest.approx(12.345678)
    assert not hasattr(__import__("app.chains.arc.network", fromlist=["x"]), "native_to_erc20")  # no fake conversion helper


def test_settings_derive_from_network_and_refuse_mismatch():
    s = Settings(_env_file=None)
    assert (s.arc_network, s.arc_chain_id, s.arc_rpc_url, s.arc_usdc_address) == ("mainnet", 5042, ARC_MAINNET.rpc_url, USDC_ERC20_ADDRESS)
    t = Settings(_env_file=None, arc_network="testnet")
    assert t.arc_chain_id == 5042002 and t.arc_rpc_url == ARC_TESTNET.rpc_url and t.arc_explorer_url == ARC_TESTNET.explorer_url
    with pytest.raises(ValueError):
        Settings(_env_file=None, arc_network="testnet", arc_chain_id=5042)   # would mix networks
    assert Settings(_env_file=None, arc_rpc_url="").arc_rpc_urls == []       # empty string = deliberately no RPC


def test_wallet_chain_params_use_native_18_decimals():
    p = wallet_chain_params(ARC_MAINNET)
    assert p["chainId"] == "0x13b2" and p["nativeCurrency"] == {"name": "USDC", "symbol": "USDC", "decimals": 18}
    assert p["rpcUrls"] == [ARC_MAINNET.rpc_url] and p["blockExplorerUrls"] == [ARC_MAINNET.explorer_url]


async def test_usdc_balance_reads_only_the_erc20_view_with_6_decimals():
    calls = []

    def h(req: httpx.Request):
        import json
        body = json.loads(req.read())
        calls.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(12_345_678)})
    a = ArcAdapter(EvmRpcClient(["http://x"], transport=httpx.MockTransport(h)))
    assert await a.usdc_balance("0x" + "ab" * 20) == pytest.approx(12.345678)
    assert [c["method"] for c in calls] == ["eth_call"]                        # never eth_getBalance (native view)
    assert calls[0]["params"][0]["to"] == USDC_ERC20_ADDRESS and "313ce567" not in calls[0]["params"][0]["data"]  # no decimals() call
    assert await a.erc20_balance(USDC_ERC20_ADDRESS, "0x" + "ab" * 20) == pytest.approx(12.345678)
    for s in NATIVE_SENTINELS:
        with pytest.raises(ValueError):
            await a.erc20_balance(s, "0x" + "ab" * 20)


@pytest.mark.parametrize("addr", [USDC_ERC20_ADDRESS, "0x0000000000000000000000000000000000000000", "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"])
def test_usdc_and_native_are_never_trade_targets(addr):
    assert is_usdc_or_native(addr)
    a = assess(good_market(token_address=addr), mode=TradingMode.PAPER)
    assert a.decision == RiskDecision.REJECT and "NON_TRADABLE_ASSET" in vetoes(a)


async def test_arc_finality_settings():
    assert ArcAdapter.receipt_poll_s == 0.25 and LiveSettings().confirm_timeout_s == 20.0

    class Fake(ChainAdapter):
        name, chain_id, receipt_poll_s = "arc", 5042, 0.0
        n = 0
        async def health(self): ...
        async def get_receipt(self, h):
            self.n += 1
            return TxReceipt(tx_hash=h, status=1) if self.n >= 3 else None
    f = Fake()
    assert (await f.wait_for_receipt("0x1", 5)).status == 1 and f.n == 3       # one successful receipt == final
    g = Fake()
    g.n = -10**6                                                               # never confirms (e.g. gas consumed, no receipt)
    assert await g.wait_for_receipt("0x1", 0.05) is None                       # => TIMEOUT path, never success


def test_evm_differences_are_recorded_and_no_fee_floor_is_invented():
    joined = " ".join(EVM_DIFFERENCES_TO_HANDLE).lower()
    assert "maxfeepergas" in joined and "without a receipt" in joined and "18 decimals" in joined
    src = open("app/chains/arc/network.py").read()
    assert "gwei" not in src.lower()


def test_uniswap_addresses_are_evidence_only_and_router_allowlist_is_empty():
    d = deployments.describe()
    assert d["status"] == "unverified_evidence" and d["router_allowlist"] == [] and "NEVER" in d["usable_for"]
    assert set(d["addresses"]) == {"uniswap_v2_factory", "uniswap_v3_factory", "uniswap_v4_pool_manager"}
    assert ArcAdapter(EvmRpcClient([])).router_allowlist() == set()


def test_pools_is_registered_but_disabled_and_cannot_be_enabled():
    assert [p.name for p in arc_candidates()] == ["pools"]
    assert POOLS.enabled is False and POOLS.verified is False and POOLS.factory_address is None
    assert POOLS.quote_asset == "USDC" and "uniswap_v4" in POOLS.pool_model
    with pytest.raises(ValueError):
        LaunchpadDescriptor(**{**POOLS.model_dump(), "enabled": True})


async def test_api_exposes_pools_disabled_wallet_options_and_chain_params(env):
    h = await env.register()
    lp = (await env.client.get("/launchpads", headers=h)).json()["launchpads"]
    assert [(x["name"], x["enabled"], x["verified"]) for x in lp] == [("pools", False, False)]
    ch = (await env.client.get("/chains", headers=h)).json()[0]
    assert ch["network"] == "mainnet" and ch["wallet_chain_params"]["chainId"] == "0x13b2"
    assert ch["deployments"]["status"] == "unverified_evidence" and ch["live_trading_verified"] is False
    w = (await env.client.get("/wallet", headers=h)).json()
    opts = {o["id"]: o for o in w["wallet_options"]}
    assert opts["circle_developer_controlled_wallet"]["status"] == "forbidden" and opts["browser_wallet"]["status"] == "available"
    assert opts["circle_agent_wallet"]["status"] == "not_integrated"
    assert w["agent_wallet"]["available"] is False and "docs/local-runner.md" in w["agent_wallet"]["decision_doc"]
    assert w["usdc"]["view"] == "ERC-20, 6 decimals" and w["network"]["chain_params"]["nativeCurrency"]["decimals"] == 18
