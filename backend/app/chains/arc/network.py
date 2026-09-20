"""Arc network constants.

PROVENANCE (both agree, fetched/read 2026-09-19):
  1. User-supplied "Arc + Circle Implementation Documentation Packet" (sections 2, 3, 22).
  2. Circle's official `use-arc` skill: github.com/circlefin/skills/blob/master/plugins/circle/skills/use-arc/SKILL.md
NOT independently re-checked against docs.arc.io / developers.circle.com (unreachable from the build sandbox).
Circle's own guidance is "look up USDC addresses per chain; never hardcode": every value here is CONFIG DEFAULTS,
overridable via environment, never baked into trading logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArcNetwork:
    name: str
    chain_id: int
    rpc_url: str
    ws_url: str
    explorer_url: str
    cctp_domain: int
    viem_chain: str   # built-in viem chain export
    sdk_chain: str    # Circle App Kit / Swap Kit chain string


ARC_MAINNET = ArcNetwork("mainnet", 5042, "https://rpc.mainnet.arc.io", "wss://rpc.mainnet.arc.io",
                         "https://explorer.arc.io", 26, "arc", "Arc")
ARC_TESTNET = ArcNetwork("testnet", 5042002, "https://rpc.testnet.arc.io", "wss://rpc.testnet.arc.io",
                         "https://explorer.testnet.arc.io", 26, "arcTestnet", "Arc_Testnet")
NETWORKS = {"mainnet": ARC_MAINNET, "testnet": ARC_TESTNET}

# USDC on Arc: ONE pool of funds exposed two ways. Never sum, convert or "swap" between the views.
USDC_ERC20_ADDRESS = "0x3600000000000000000000000000000000000000"   # same on mainnet and testnet
USDC_ERC20_DECIMALS = 6      # balances, transfers, approvals, display
NATIVE_USDC_DECIMALS = 18    # gas and msg.value ONLY (1e18 native == 1e6 ERC-20)
NATIVE_SENTINELS = frozenset({"0x0000000000000000000000000000000000000000",
                              "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"})

# Documented Arc runtime differences that matter for execution (details live on docs.arc.io/arc/references/evm-differences,
# which was NOT readable here, so none of the numbers below are invented):
EVM_DIFFERENCES_TO_HANDLE = (
    "native USDC 18 decimals vs ERC-20 USDC 6 decimals",
    "system emitter for USDC Transfer events: do not parse fills from USDC Transfer logs without reading the doc",
    "mempool maxFeePerGas floor: fee must be read from the node/docs, never hard-coded",
    "blocklist-related behaviour can consume gas WITHOUT a receipt: no receipt is never success (TIMEOUT path)",
    "sending to address(0) behaves differently from Ethereum",
)


def is_usdc_or_native(address: str) -> bool:
    a = address.lower()
    return a == USDC_ERC20_ADDRESS.lower() or a in NATIVE_SENTINELS


def format_usdc(raw_6dp: int) -> float:
    """ERC-20 (6-decimal) view -> display value. There is deliberately no native<->ERC-20 conversion helper."""
    return raw_6dp / 10 ** USDC_ERC20_DECIMALS


def wallet_chain_params(net: ArcNetwork) -> dict:
    """EIP-3085 wallet_addEthereumChain payload. Native currency is USDC with 18 decimals (the native view)."""
    return {"chainId": hex(net.chain_id), "chainName": "Arc" if net.name == "mainnet" else "Arc Testnet",
            "nativeCurrency": {"name": "USDC", "symbol": "USDC", "decimals": NATIVE_USDC_DECIMALS},
            "rpcUrls": [net.rpc_url], "blockExplorerUrls": [net.explorer_url]}
