"""Wraps any MarketDataProvider with enrichment, and builds an EnrichmentService from settings/env.

One wrapper is used by both the local runner (runner/runtime.py) and the cloud global pipeline
(app/discovery/bootstrap.py) so contract/holder/creator/MEV enrichment behaves identically in both places.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.chains.base import MarketDataProvider
from app.chains.evm import EvmRpcClient
from app.domain.market import MarketState
from app.enrichment.service import EnrichmentConfig, EnrichmentService
from app.integrations.blockscout import BlockscoutClient
from app.integrations.etherscan import EtherscanV2Client

log = logging.getLogger("enrichment.wrapper")

PreviousLookup = Callable[[str], Awaitable["MarketState | None"]]


class EnrichingMarketDataProvider(MarketDataProvider):
    """Delegates discovery/state to ``inner`` and layers enrichment onto every ``get_market_state`` result.

    ``previous_lookup``, if given, returns the last known MarketState for a token key (used to compute
    ``holder_growth_pct``). Discovery/monitoring callers can pass their own snapshot store; without one, holder
    growth simply stays unset on the first scan and populates from the second scan onward.
    """

    name = "enriching"

    def __init__(self, inner: MarketDataProvider, enrichment: EnrichmentService,
                 previous_lookup: PreviousLookup | None = None,
                 record_fn: Callable[["MarketState"], Awaitable[None]] | None = None) -> None:
        self.inner = inner
        self.enrichment = enrichment
        self.previous_lookup = previous_lookup
        self.record_fn = record_fn

    async def get_market_state(self, token_address: str) -> MarketState:
        m = await self.inner.get_market_state(token_address)
        previous = None
        if self.previous_lookup is not None:
            try:
                previous = await self.previous_lookup(m.key)
            except Exception:
                log.debug("previous-snapshot lookup failed for %s", m.key, exc_info=True)
        try:
            out = await self.enrichment.enrich(m, previous=previous)
        except Exception:
            log.exception("enrichment crashed for %s; returning un-enriched state", token_address)
            return m
        if self.record_fn is not None:
            try:
                await self.record_fn(out)
            except Exception:
                log.debug("snapshot record failed for %s", out.key, exc_info=True)
        return out

    async def discover_tokens(self) -> list[str]:
        return await self.inner.discover_tokens()

    def __getattr__(self, name: str) -> Any:
        # Pass through provider-specific extras (e.g. registry.status(), .last_state_sources) transparently.
        return getattr(self.inner, name)


def build_enrichment_service(settings: Any, *, rpc: EvmRpcClient | None) -> EnrichmentService | None:
    """Builds an EnrichmentService from settings/env. Returns None if disabled — callers then skip wrapping."""
    if not bool(getattr(settings, "enrichment_enabled", True)):
        return None

    chain_id = getattr(settings, "arc_chain_id", None)
    if chain_id is None:
        network = getattr(settings, "network", None)  # RunnerSettings exposes .network.chain_id
        chain_id = getattr(network, "chain_id", 5042) if network is not None else 5042

    blockscout = None
    if bool(getattr(settings, "blockscout_enabled", True)):
        key = None
        if getattr(settings, "blockscout_api_key", None):
            key = settings.blockscout_api_key.get_secret_value()
        try:
            blockscout = BlockscoutClient(
                api_key=key,
                chain_id=int(chain_id),
                pro_root=getattr(settings, "blockscout_pro_root", "https://api.blockscout.com"),
                public_root=getattr(settings, "blockscout_public_root", "https://explorer.arc.io"),
                timeout_s=float(getattr(settings, "market_data_timeout_s", 8.0)),
                daily_credit_budget=int(getattr(settings, "blockscout_daily_credit_budget", 90_000)),
            )
        except Exception:
            log.exception("blockscout client init failed")

    etherscan = None
    es_key = getattr(settings, "etherscan_api_key", None)
    if es_key and bool(getattr(settings, "etherscan_enabled", True)):
        try:
            etherscan = EtherscanV2Client(
                es_key.get_secret_value(),
                chain_id=int(chain_id),
                timeout_s=float(getattr(settings, "market_data_timeout_s", 8.0)),
            )
        except Exception:
            log.exception("etherscan client init failed")

    if blockscout is None and etherscan is None and rpc is None:
        log.warning("enrichment enabled but no explorer key and no RPC client available; enrichment is a no-op")
        return None

    cfg = EnrichmentConfig(
        enabled=True,
        holders_ttl_s=float(getattr(settings, "enrichment_holders_ttl_s", 300.0)),
        static_ttl_s=float(getattr(settings, "enrichment_static_ttl_s", 21_600.0)),
        probe_ttl_s=float(getattr(settings, "enrichment_probe_ttl_s", 900.0)),
        trade_usdc_for_mev=float(getattr(settings, "risk_max_trade_usdc", 25.0) or 25.0),
    )
    return EnrichmentService(blockscout=blockscout, etherscan=etherscan, rpc=rpc, config=cfg)
