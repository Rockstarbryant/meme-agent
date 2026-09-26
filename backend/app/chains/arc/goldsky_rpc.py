"""Goldsky Edge RPC for Arc.

Goldsky Edge RPC is a drop-in JSON-RPC endpoint with a 20,000-block
``eth_getLogs`` cap on Arc (chain 5042) — 2,000x Alchemy Free tier's 10-block
limit. That means the existing on-chain discovery providers
(``ArcRpcMarketData``, ``ArcUniswapV4RpcMarketData``) work as designed with no
incremental watermarks or scan-window gymnastics.

Usage:
    from app.chains.arc.goldsky_rpc import build_goldsky_rpc
    rpc = build_goldsky_rpc("your-key")
    # rpc is a normal EvmRpcClient; pass it to ArcRpcMarketData(...)

Get a free key at https://app.goldsky.com. The shared ``key=demo`` works for
testing but is rate limited and not for production.
"""
from __future__ import annotations

from app.chains.evm import EvmRpcClient

GOLDSKY_ARC_RPC_URL = "https://edge.goldsky.com/standard/evm/5042"


def build_goldsky_rpc(api_key: str | None = None, *, fallback_urls: list[str] | None = None,
                      on_range_cap=None, initial_range_cap: int | None = None) -> EvmRpcClient:
    """Build an EvmRpcClient pointed at Goldsky Edge RPC for Arc.

    ``api_key=None`` uses the shared demo key. That works for smoke-testing
    but is rate limited; register for a free key at app.goldsky.com for real
    traffic. ``fallback_urls`` are appended after Goldsky so the existing
    multi-provider failover still works if you want to keep Alchemy as a
    secondary.
    """
    key = (api_key or "demo").strip()
    primary = f"{GOLDSKY_ARC_RPC_URL}?key={key}"
    urls = [primary] + list(fallback_urls or [])
    return EvmRpcClient(
        urls,
        # Goldsky's per-request cap is 20,000 blocks on Arc; start there and
        # let get_logs() narrow if a future chain-specific limit is lower.
        max_log_range=20000,
        min_log_range=10,
        on_range_cap=on_range_cap,
        initial_range_cap=initial_range_cap,
    )