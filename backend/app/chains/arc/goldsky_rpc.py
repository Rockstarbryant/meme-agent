"""Goldsky Edge RPC for Arc.

Goldsky Edge RPC is a drop-in JSON-RPC endpoint with a 20,000-block
``eth_getLogs`` cap on Arc (chain 5042) — 2,000x Alchemy Free tier's 10-block
limit. That means the existing on-chain discovery providers
(``ArcRpcMarketData``, ``ArcUniswapV4RpcMarketData``) work as designed with no
incremental watermarks or scan-window gymnastics.

Usage:
    from app.chains.arc.goldsky_rpc import build_goldsky_rpc
    rpc = build_goldsky_rpc("gs_edge_abc123")
    # rpc is a normal EvmRpcClient; pass it to ArcRpcMarketData(...)

Get a free key at https://app.goldsky.com (Edge RPC page).
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.chains.evm import EvmRpcClient

GOLDSKY_ARC_RPC_URL = "https://edge.goldsky.com/standard/evm/5042"


def _extract_key(api_key: str | None) -> str:
    """Accept either a raw key (``gs_edge_xxx``) or a full URL.

    Users frequently paste the full dashboard URL into the API key field
    (``https://edge.goldsky.com/standard/evm/5042?key=gs_edge_xxx``). If that
    happens, extract just the ``key=`` query parameter. Otherwise use the
    string as-is. Falls back to ``demo`` when nothing is provided.
    """
    if not api_key:
        return "demo"
    raw = api_key.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            qs = parse_qs(urlparse(raw).query)
            key = (qs.get("key") or [""])[0].strip()
            if key:
                return key
        except Exception:
            pass
    return raw


def build_goldsky_rpc(api_key: str | None = None, *, fallback_urls: list[str] | None = None,
                      on_range_cap=None, initial_range_cap: int | None = None) -> EvmRpcClient:
    """Build an EvmRpcClient pointed at Goldsky Edge RPC for Arc.

    ``api_key`` may be a raw key or a full URL (see ``_extract_key``).
    ``api_key=None`` uses the shared ``demo`` key — works for smoke-testing
    but rate limited; get a real key at app.goldsky.com.

    ``fallback_urls`` are appended after Goldsky so the existing multi-provider
    failover still works if you want to keep Alchemy as a secondary.
    """
    key = _extract_key(api_key)
    primary = f"{GOLDSKY_ARC_RPC_URL}?key={key}"
    urls = [primary] + list(fallback_urls or [])
    return EvmRpcClient(
        urls,
        max_log_range=20000,
        min_log_range=10,
        on_range_cap=on_range_cap,
        initial_range_cap=initial_range_cap,
    )