"""Goldsky as its OWN named market-data provider (on-chain, via Goldsky Edge RPC for Arc / chain 5042).

Before this, ``goldsky`` in MARKET_DATA_PROVIDERS was only an alias that registered ``arc_rpc`` and ``uniswap_v4_rpc``
on an RPC client that silently fell back to Alchemy/public RPC. That made outages invisible: the registry blamed
``arc_rpc`` while the real cause was Goldsky, and the fallback hid it. This composite:

  * is named ``goldsky`` in the registry (health, circuit breaker and audit trail all say "goldsky");
  * talks ONLY to Goldsky Edge RPC (no hidden fallback inside the client; the registry's other providers ARE the fallback);
  * derives price / flow / buyer-seller counts from Uniswap v4 ``Swap`` events and token metadata / creation from the
    launchpad contracts (the existing, tested on-chain providers), merged with the same rules as the registry.
"""
from __future__ import annotations

import logging

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import MarketState

log = logging.getLogger("goldsky")


class GoldskyArcMarketData(MarketDataProvider):
    name = "goldsky"

    def __init__(self, swaps: MarketDataProvider | None, metadata: MarketDataProvider | None):
        if swaps is None and metadata is None:
            raise ValueError("GoldskyArcMarketData needs at least one on-chain provider")
        self._swaps, self._meta = swaps, metadata

    async def discover_tokens(self) -> list[str]:
        found: list[str] = []
        errs: list[str] = []
        for label, p in (("uniswap_v4", self._swaps), ("launchpads", self._meta)):
            if p is None:
                continue
            try:
                for t in await p.discover_tokens():
                    if t.lower() not in found:
                        found.append(t.lower())
            except DataUnavailable as exc:
                errs.append(f"{label}: {exc}")
        if not found and errs:
            raise DataUnavailable("goldsky discovery failed: " + " | ".join(errs)[:400])
        return found

    async def get_market_state(self, token_address: str) -> MarketState:
        from app.market_data.registry import MarketDataRegistry
        primary: MarketState | None = None
        errs: list[str] = []
        for label, p in (("uniswap_v4", self._swaps), ("launchpads", self._meta)):
            if p is None:
                continue
            try:
                st = await p.get_market_state(token_address)
            except DataUnavailable as exc:
                errs.append(f"{label}: {exc}")
                continue
            st = st.model_copy(update={"data_sources": [f"goldsky:{label}", *[s for s in st.data_sources if not s.startswith("goldsky:")]]})
            primary = st if primary is None else MarketDataRegistry._merge(primary, st)
        if primary is None:
            raise DataUnavailable("goldsky has no on-chain data for " + token_address + ("; " + " | ".join(errs)[:400] if errs else ""))
        return primary

    async def aclose(self) -> None:
        for p in (self._swaps, self._meta):
            close = getattr(p, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.debug("goldsky close failed", exc_info=True)
