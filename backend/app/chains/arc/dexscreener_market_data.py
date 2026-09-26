from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState
from app.integrations.dexscreener import DexScreenerClient


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DexScreenerArcMarketData(MarketDataProvider):
    """Free, no-API-key enrichment provider (https://docs.dexscreener.com/api/reference).

    Discovery is intentionally NOT implemented here: DexScreener has no
    reliable "newest pools on chain X" endpoint for a free-tier integration,
    and Arc RPC / Uniswap v4 RPC remain the authoritative on-chain discovery
    sources. This provider only fills in price/liquidity/volume fields for a
    token address the runner already discovered elsewhere — a pure enrichment
    fallback for whenever GeckoTerminal is also rate-limited or Bitquery's
    quota is exhausted.

    ``chain_id`` is the DexScreener chain slug. Coverage of new chains (Arc
    included) depends entirely on DexScreener's own indexers; if the chain
    isn't indexed yet this simply raises DataUnavailable like any other empty
    result — never a hard runner failure, since it's an optional source.
    """

    def __init__(self, client: DexScreenerClient, *, chain_id: str = "arc", cache_s: float = 60.0):
        self.client = client
        self.chain_id = chain_id
        self.cache_s = max(5.0, cache_s)
        self._cache: dict[str, tuple[float, MarketState]] = {}

    async def discover_tokens(self) -> list[str]:
        return []  # enrichment-only; see class docstring

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        cached = self._cache.get(token)
        if cached and time.monotonic() - cached[0] < self.cache_s:
            return cached[1]
        pairs = await self.client.token_pairs(self.chain_id, token)
        if not pairs:
            raise DataUnavailable(f"DexScreener has no pair for {token} on chain '{self.chain_id}'")
        pairs = sorted(pairs, key=lambda p: (_num((p.get("liquidity") or {}).get("usd")) or 0.0), reverse=True)
        pair = pairs[0]
        base = pair.get("baseToken") or {}
        base_is_target = str(base.get("address") or "").lower() == token
        liq = pair.get("liquidity") or {}
        vol = pair.get("volume") or {}
        chg = pair.get("priceChange") or {}
        txns = pair.get("txns") or {}
        m5 = txns.get("m5") or {}
        created_ms = pair.get("pairCreatedAt")
        created = datetime.fromtimestamp(created_ms / 1000, timezone.utc) if isinstance(created_ms, (int, float)) else None
        now = datetime.now(timezone.utc)
        state = MarketState(
            chain="arc", token_address=token, timestamp=now,
            pool_address=str(pair.get("pairAddress") or "") or None,
            symbol=base.get("symbol") if base_is_target else (pair.get("quoteToken") or {}).get("symbol"),
            token_created_at=created,
            price=_num(pair.get("priceUsd")),
            market_cap=_num(pair.get("marketCap")) or _num(pair.get("fdv")),
            liquidity=_num(liq.get("usd")),
            price_change_5m=_num(chg.get("m5")), price_change_15m=None, price_change_1m=None,
            volume_5m=_num(vol.get("m5")), volume_15m=None, volume_1m=None,
            buys_5m=int(_num(m5.get("buys")) or 0) or None, sells_5m=int(_num(m5.get("sells")) or 0) or None,
            contract=ContractInfo(verified=None),
            data_sources=["dexscreener:pairs"], is_demo=False,
        )
        self._cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        await self.client.close()
