from __future__ import annotations

import time
from datetime import datetime

from app.chains.base import ChainAdapter, ChainHealth, MarketDataProvider, TxReceipt, SimulationResult, FillDetails
from app.chains.evm import EvmRpcClient
from app.chains.arc.network import NATIVE_SENTINELS, USDC_ERC20_ADDRESS, USDC_ERC20_DECIMALS
from app.core.errors import DataUnavailable, IntegrationNotVerified
from app.domain.market import MarketState
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction


class ArcAdapter(ChainAdapter):
    """Arc chain adapter + optional verified Uniswap AMM execution adapter."""

    name = "arc"
    receipt_poll_s = 0.25

    def __init__(self, rpc: EvmRpcClient, chain_id: int = 5042, explorer_url: str = "",
                 router_allowlist: set[str] | None = None, usdc_address: str = USDC_ERC20_ADDRESS, dex=None,
                 live_trading_verified: bool = False):
        self.rpc, self.chain_id, self.explorer_url = rpc, chain_id, explorer_url
        self.usdc_address = usdc_address
        self.dex = dex
        self._live_trading_verified = bool(live_trading_verified)
        self._routers = {r.lower() for r in (router_allowlist or set())}
        if dex is not None and hasattr(dex, "name"):
            from app.chains.arc.deployments import ARC_UNIVERSAL_ROUTER
            self._routers.add(ARC_UNIVERSAL_ROUTER.lower())

    @property
    def live_trading_verified(self) -> bool:
        # Default False (fail-closed). Set ARC_RUNNER_LIVE_TRADING_VERIFIED=true only after
        # you have run `python -m runner circle-verify`, reviewed the capture, confirmed
        # Permit2 / wallet execute behaviour, and completed a testnet dry-run.
        return self._live_trading_verified

    def router_allowlist(self) -> set[str]:
        return set(self._routers)

    async def health(self) -> ChainHealth:
        t0 = time.monotonic()
        try:
            cid = int(await self.rpc.call("eth_chainId"), 16)
            blk = int(await self.rpc.call("eth_blockNumber"), 16)
        except Exception as exc:
            return ChainHealth(ok=False, detail=f"rpc unavailable: {type(exc).__name__}")
        ok = cid == self.chain_id
        return ChainHealth(ok=ok, chain_id=cid, block_number=blk, latency_ms=(time.monotonic() - t0) * 1000,
                           detail="" if ok else f"chain id mismatch: rpc={cid} configured={self.chain_id}")

    async def usdc_balance(self, owner: str) -> float:
        data = "0x70a08231" + owner.lower().removeprefix("0x").rjust(64, "0")
        raw = await self.rpc.call("eth_call", [{"to": self.usdc_address, "data": data}, "latest"])
        return int(raw, 16) / 10 ** USDC_ERC20_DECIMALS

    async def erc20_balance(self, token: str, owner: str) -> float:
        if token.lower() in NATIVE_SENTINELS:
            raise ValueError("native sentinel is not an ERC-20 contract")
        if token.lower() == self.usdc_address.lower():
            return await self.usdc_balance(owner)
        data = "0x70a08231" + owner.lower().removeprefix("0x").rjust(64, "0")
        raw = await self.rpc.call("eth_call", [{"to": token, "data": data}, "latest"])
        dec = int(await self.rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
        return int(raw, 16) / 10 ** dec

    async def get_receipt(self, tx_hash: str) -> TxReceipt | None:
        r = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
        if r is None:
            return None
        return TxReceipt(tx_hash=tx_hash, status=int(r.get("status", "0x0"), 16),
                         block_number=int(r["blockNumber"], 16) if r.get("blockNumber") else None,
                         gas_used=int(r["gasUsed"], 16) if r.get("gasUsed") else None, logs=r.get("logs", []))

    async def quote(self, request: TradeRequest) -> Quote:
        if self.dex is None:
            raise IntegrationNotVerified("Arc Uniswap quote", "configure UNISWAP_API_KEY on the Local Runner")
        return await self.dex.quote(request)

    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        if self.dex is None:
            raise IntegrationNotVerified("Arc Uniswap swap", "configure UNISWAP_API_KEY on the Local Runner")
        return await self.dex.build_swap_tx(quote, request, deadline)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        if self.dex is None:
            raise IntegrationNotVerified("Arc Uniswap simulation", "configure the Uniswap adapter")
        return await self.dex.simulate(tx)

    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails:
        if self.dex is None:
            raise IntegrationNotVerified("Arc fill parsing", "configure the Uniswap adapter")
        return await self.dex.parse_fill(receipt, quote)
