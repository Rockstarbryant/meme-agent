"""Codex market-data provider (price, liquidity, holders count, per-window trading stats) for Arc.

One ``filterTokens`` request per token (cached) gives price, liquidity, market cap, holder count and the 1h/4h/12h/24h
(and, when the schema offers them, 5m) buy/sell/unique-trader/volume/price-change figures. Discovery is OFF unless
asked for, so the default use is a quota-friendly fallback/enrichment behind the free providers.

Unit handling: Codex documents price changes as "decimal format". ``change_unit="fraction"`` (default) multiplies by 100
to the percent unit the rest of the system uses; set ``"percent"`` if the first raw sample (recorded in the audit trail)
shows values already in percent.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState, WindowStats
from app.integrations.codex import CodexClient
from app.observability.audit import AuditKind, AuditStatus, record_event


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _int(v: Any) -> int | None:
    x = _num(v)
    return None if x is None else int(x)


class CodexArcMarketData(MarketDataProvider):
    name = "codex"

    def __init__(self, client: CodexClient, *, cache_s: float = 60.0, discovery_enabled: bool = False,
                 discovery_min_liquidity: float = 5_000.0, max_tokens: int = 40, discovery_lookback_s: float = 6 * 3600,
                 change_unit: str = "fraction"):
        self.client = client
        self.cache_s = max(5.0, cache_s)
        self.discovery_enabled = discovery_enabled
        self.discovery_min_liquidity = discovery_min_liquidity
        self.max_tokens = max_tokens
        self.discovery_lookback_s = discovery_lookback_s
        self.change_mult = 100.0 if change_unit == "fraction" else 1.0
        self._cache: dict[str, tuple[float, MarketState]] = {}
        self._sampled_units = False

    async def discover_tokens(self) -> list[str]:
        if not self.discovery_enabled:
            return []
        rows = await self.client.discover(min_liquidity=self.discovery_min_liquidity, limit=self.max_tokens,
                                          created_after=int(time.time() - self.discovery_lookback_s) if self.discovery_lookback_s else None)
        out: list[str] = []
        for r in rows:
            addr = str(((r.get("token") or {}).get("address")) or "").lower()
            if addr.startswith("0x") and len(addr) == 42 and addr not in out:
                out.append(addr)
        return out

    def _pct(self, v: Any) -> float | None:
        x = _num(v)
        return None if x is None else x * self.change_mult

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        hit = self._cache.get(token)
        if hit and time.monotonic() - hit[0] < self.cache_s:
            return hit[1]
        row = await self.client.token_stats(token)
        if not row:
            raise DataUnavailable(f"Codex has no stats for {token} on network {self.client.network_id}")
        if not self._sampled_units:
            self._sampled_units = True
            record_event(AuditKind.MARKET_PROVIDER, AuditStatus.OK, provider="codex", operation="unit_sample",
                         component="codex.first_sample", token_key=f"arc:{token}",
                         detail={"change1_raw": row.get("change1"), "change24_raw": row.get("change24"),
                                 "assumed_unit": "fraction" if self.change_mult == 100.0 else "percent"})
        info = ((row.get("token") or {}).get("info")) or {}
        if info.get("isScam") is True:
            raise DataUnavailable(f"Codex flags {token} as a scam token (isScam=true); refusing to supply data")
        windows: dict[str, WindowStats] = {}
        for w, sfx in (("5m", "5m"), ("1h", "1"), ("4h", "4"), ("12h", "12"), ("24h", "24")):
            vol, chg = _num(row.get(f"volume{sfx}")), self._pct(row.get(f"change{sfx}"))
            buys, sells = _int(row.get(f"buyCount{sfx}")), _int(row.get(f"sellCount{sfx}"))
            ub, us = _int(row.get(f"uniqueBuys{sfx}")), _int(row.get(f"uniqueSells{sfx}"))
            if all(x is None for x in (vol, chg, buys, sells, ub, us)):
                continue
            windows[w] = WindowStats(volume_usd=vol, price_change_pct=chg, buys=buys, sells=sells, buyers=ub, sellers=us,
                                     basis="pool_stats")
        w5 = windows.get("5m")
        created = _int(row.get("createdAt"))
        pair = row.get("pair") or {}
        state = MarketState(
            chain="arc", token_address=token, timestamp=datetime.now(timezone.utc),
            pool_address=(str(pair.get("address")) or None) if pair.get("address") else None,
            symbol=info.get("symbol") if isinstance(info.get("symbol"), str) else None,
            token_name=info.get("name") if isinstance(info.get("name"), str) else None,
            token_created_at=datetime.fromtimestamp(created, timezone.utc) if created else None,
            price=_num(row.get("priceUSD")), liquidity=_num(row.get("liquidity")), market_cap=_num(row.get("marketCap")),
            holder_count=_int(row.get("holders")),
            volume_5m=w5.volume_usd if w5 else None, price_change_5m=w5.price_change_pct if w5 else None,
            buys_5m=w5.buys if w5 else None, sells_5m=w5.sells if w5 else None,
            unique_buyers_5m=w5.buyers if w5 else None, unique_sellers_5m=w5.sellers if w5 else None,
            windows=windows, contract=ContractInfo(), data_sources=["codex:filterTokens"], is_demo=False,
        )
        if state.price is None and state.liquidity is None:
            raise DataUnavailable(f"Codex returned an empty record for {token}")
        self._cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        await self.client.aclose()
