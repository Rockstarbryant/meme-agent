"""GoldRush market-data provider: holder count + holder concentration (+ wallet balances) for Arc.

GoldRush does not offer DEX spot prices / swap statistics for Frontier chains such as Arc, so this provider NEVER
supplies price, liquidity or volume. It exists as the holders/concentration fallback behind Codex/Blockscout and as an
independent on-chain-indexed cross-check. A state from this provider alone is intentionally incomplete (price missing):
the strategy/risk gates treat that as missing data, never as zero.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState
from app.integrations.goldrush import GoldRushClient

_EXCLUDED = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead",
             "0x8366a39cc670b4001a1121b8f6a443a643e40951"}   # burn addresses + Uniswap v4 PoolManager on Arc


def concentration_from_rows(rows: list[dict], *, total_supply: int | None, token: str, pool: str | None = None) -> dict:
    """Top-5/10/20 share of CIRCULATING supply (supply minus protocol/burn/pool/token balances). Pure function."""
    ex = set(_EXCLUDED) | {token.lower()} | ({pool.lower()} if pool else set())
    kept: list[int] = []
    excluded_bal = 0
    for r in rows:
        try:
            bal = int(str(r.get("balance") or "0"))
        except ValueError:
            continue
        if str(r.get("address") or "").lower() in ex:
            excluded_bal += bal
        else:
            kept.append(bal)
    kept.sort(reverse=True)
    out: dict = {"top5": None, "top10": None, "top20": None, "sampled": len(kept)}
    if not total_supply or total_supply <= 0:
        return out
    circ = total_supply - excluded_bal
    if circ <= 0 or not kept:
        return out
    for key, n in (("top5", 5), ("top10", 10), ("top20", 20)):
        if len(kept) >= n:
            out[key] = round(sum(kept[:n]) / circ * 100.0, 2)
    return out


class GoldRushArcMarketData(MarketDataProvider):
    name = "goldrush"

    def __init__(self, client: GoldRushClient, *, cache_s: float = 300.0, page_size: int = 100):
        self.client = client
        self.cache_s = max(10.0, cache_s)
        self.page_size = max(20, min(page_size, 100))
        self._cache: dict[str, tuple[float, MarketState]] = {}

    async def discover_tokens(self) -> list[str]:
        return []   # holders provider only

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        hit = self._cache.get(token)
        if hit and time.monotonic() - hit[0] < self.cache_s:
            return hit[1]
        page = await self.client.token_holders(token, page_size=self.page_size, page_number=0)
        rows, pag = page["items"], page["pagination"]
        if not rows and not pag.get("total_count"):
            raise DataUnavailable(f"GoldRush has no holder data for {token}")
        total_supply = None
        for r in rows:
            try:
                total_supply = int(str(r.get("total_supply"))) if r.get("total_supply") not in (None, "") else None
            except ValueError:
                total_supply = None
            if total_supply:
                break
        conc = concentration_from_rows(rows, total_supply=total_supply, token=token)
        gaps = []
        if conc["top10"] is None:
            gaps.append("goldrush holders: supply unknown or fewer than 10 holder rows; concentration not computed")
        count = pag.get("total_count")
        state = MarketState(
            chain="arc", token_address=token, timestamp=datetime.now(timezone.utc),
            holder_count=int(count) if isinstance(count, (int, float)) else None,
            top5_holder_pct=conc["top5"], top10_holder_pct=conc["top10"], top20_holder_pct=conc["top20"],
            holders_sampled=conc["sampled"] or None,
            holder_basis=f"goldrush top {conc['sampled']} holder rows, circulating supply" if conc["top10"] is not None else None,
            contract=ContractInfo(), enrichment_gaps=gaps, data_sources=["goldrush:token_holders_v2"], is_demo=False,
        )
        self._cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        await self.client.aclose()
