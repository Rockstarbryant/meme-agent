"""Builds the Codex / GoldRush / Goldsky providers from settings. Shared by the local runner and the cloud global
pipeline so both construct them identically (``getattr`` defaults keep it usable with either settings class)."""
from __future__ import annotations

import logging
from typing import Any

from app.chains.base import MarketDataProvider

log = logging.getLogger("market_data.factory")


def _secret(settings: Any, attr: str) -> str:
    v = getattr(settings, attr, None)
    if v is None:
        return ""
    return (v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)).strip()


def build_extra_market_providers(settings: Any, wanted: list[str]) -> tuple[dict[str, MarketDataProvider], list[str]]:
    """Returns (providers by name, human-readable notes about providers that were requested but skipped)."""
    built: dict[str, MarketDataProvider] = {}
    notes: list[str] = []
    timeout = float(getattr(settings, "market_data_timeout_s", 8.0))
    cache_s = float(getattr(settings, "market_data_cache_s", 60.0))
    max_tokens = int(getattr(settings, "market_data_max_tokens", 20))

    if "codex" in wanted:
        key = _secret(settings, "codex_api_key")
        if not key:
            notes.append("codex requested but ARC_RUNNER_CODEX_API_KEY is not set")
        else:
            from app.chains.arc.codex_market_data import CodexArcMarketData
            from app.integrations.codex import CodexClient
            client = CodexClient(key, url=getattr(settings, "codex_url", "https://graph.codex.io/graphql"),
                                 timeout_s=timeout, min_interval_s=float(getattr(settings, "codex_min_interval_s", 0.2)))
            built["codex"] = CodexArcMarketData(
                client, cache_s=float(getattr(settings, "codex_cache_s", 60.0)),
                discovery_enabled=bool(getattr(settings, "codex_discovery_enabled", False)),
                discovery_min_liquidity=float(getattr(settings, "candidate_min_liquidity_usdc", 5_000.0)),
                max_tokens=max_tokens, change_unit=str(getattr(settings, "codex_change_unit", "fraction")))

    if "goldrush" in wanted:
        key = _secret(settings, "goldrush_api_key")
        if not key:
            notes.append("goldrush requested but ARC_RUNNER_GOLDRUSH_API_KEY is not set")
        else:
            from app.chains.arc.goldrush_market_data import GoldRushArcMarketData
            from app.integrations.goldrush import GoldRushClient
            client = GoldRushClient(key, base_url=getattr(settings, "goldrush_base_url", "https://api.covalenthq.com/v1"),
                                    chain=getattr(settings, "goldrush_chain", "arc-mainnet"), timeout_s=timeout)
            built["goldrush"] = GoldRushArcMarketData(client, cache_s=float(getattr(settings, "goldrush_cache_s", 300.0)))

    if "goldsky" in wanted:
        from app.chains.arc.goldsky_market_data import GoldskyArcMarketData
        from app.chains.arc.goldsky_rpc import build_goldsky_rpc
        from app.chains.arc.rpc_market_data import ArcRpcMarketData
        from app.chains.arc.uniswap_v4_rpc import ArcUniswapV4RpcMarketData
        rpc = build_goldsky_rpc(_secret(settings, "goldsky_api_key") or None, fallback_urls=[])   # Goldsky ONLY: no hidden fallback
        built["goldsky"] = GoldskyArcMarketData(
            ArcUniswapV4RpcMarketData(rpc, max_tokens=max_tokens, scan_blocks=int(getattr(settings, "uniswap_v4_scan_blocks", 5000)),
                                      swap_scan_blocks=int(getattr(settings, "uniswap_v4_swap_scan_blocks", 1800)), cache_s=cache_s),
            ArcRpcMarketData(rpc, max_tokens=max_tokens, scan_blocks=int(getattr(settings, "rpc_launch_scan_blocks", 10000)),
                             cache_s=cache_s))
    return built, notes
