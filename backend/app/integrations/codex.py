"""Codex (codex.io, formerly Defined.fi) GraphQL client.

Verified against https://docs.codex.io (July-Oct 2026 docs): Arc mainnet is network id ``5042``; endpoint
``https://graph.codex.io/graphql``; the API key goes in the raw ``Authorization`` header (no ``Bearer``);
token ids are ``<address>:<networkId>``.

Plan notes (from the docs): ``holders`` / ``balances`` need a Growth or Enterprise plan; ``filterTokens``,
``getTokenPrices``, ``getBars``, ``token`` and the standalone ``top10HoldersPercent`` do not. A plan error is surfaced
as ``CodexPlanRequired`` so callers can stop asking for that capability instead of burning quota every cycle.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable

ARC_NETWORK_ID = 5042


class CodexPlanRequired(DataUnavailable):
    """The endpoint exists but the account's plan does not include it (not a transient failure)."""


class CodexSchemaMismatch(DataUnavailable):
    """GraphQL validation error (unknown field / bad argument): the schema moved. Callers may retry a smaller query."""


class CodexClient:
    def __init__(self, api_key: str, *, url: str = "https://graph.codex.io/graphql", network_id: int = ARC_NETWORK_ID,
                 timeout_s: float = 8.0, min_interval_s: float = 0.2, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("Codex needs an API key (https://dashboard.codex.io)")
        self.url, self.network_id = url, int(network_id)
        self._key = api_key.strip()
        self.client = httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                        headers={"content-type": "application/json", "accept": "application/json"})
        self.min_interval_s = max(0.0, min_interval_s)
        self._last = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    def token_id(self, address: str) -> str:
        return f"{address.lower()}:{self.network_id}"

    async def query(self, gql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            cool = self._cooldown_until - time.monotonic()
            if cool > 0:
                raise DataUnavailable(f"Codex cooling down after rate limit ({cool:.0f}s left)")
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.post(self.url, json={"query": gql, "variables": variables or {}},
                                           headers={"Authorization": self._key})
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"Codex network error: {type(exc).__name__}") from exc
            finally:
                self._last = time.monotonic()
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", "") or 30.0)
            except ValueError:
                ra = 30.0
            self._cooldown_until = time.monotonic() + min(120.0, max(5.0, ra))
            raise DataUnavailable(f"Codex rate limited (HTTP 429, retry-after={ra:.0f}s)")
        if r.status_code in (401, 403):
            raise DataUnavailable(f"Codex rejected the API key (HTTP {r.status_code})")
        if r.status_code >= 500:
            raise DataUnavailable(f"Codex server error HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise DataUnavailable(f"Codex returned non-JSON (HTTP {r.status_code})") from exc
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            msg = "; ".join(str(e.get("message", e))[:160] for e in errors[:3] if isinstance(e, dict)) or str(errors)[:200]
            low = msg.lower()
            if "cannot query field" in low or "unknown argument" in low or "unknown type" in low or "validation" in low:
                raise CodexSchemaMismatch(f"Codex GraphQL schema mismatch: {msg}")
            if any(w in low for w in ("plan", "upgrade", "growth", "enterprise", "not authorized", "forbidden", "access denied")):
                raise CodexPlanRequired(f"Codex plan does not include this query: {msg}")
            if "rate" in low and "limit" in low:
                self._cooldown_until = time.monotonic() + 30.0
                raise DataUnavailable(f"Codex rate limited: {msg}")
            raise DataUnavailable(f"Codex GraphQL error: {msg}")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise DataUnavailable("Codex returned no data")
        return data

    # ------------------------------------------------------------------ token stats
    _FIELDS_MINIMAL = "priceUSD liquidity marketCap holders createdAt pair { address } token { info { name symbol totalSupply isScam } }"
    _FIELDS_CORE = (_FIELDS_MINIMAL + " volume1 volume4 volume12 volume24 change1 change4 change12 change24"
                    " buyCount1 sellCount1 uniqueBuys1 uniqueSells1 buyCount4 sellCount4 buyCount12 sellCount12 buyCount24 sellCount24")
    _FIELDS_RICH = _FIELDS_CORE + " volume5m change5m buyCount5m sellCount5m uniqueBuys5m uniqueSells5m"

    async def token_stats(self, address: str) -> dict[str, Any] | None:
        """One ``filterTokens`` call for one token. Falls back to smaller field sets if the schema rejects a field."""
        last: Exception | None = None
        for fields in (self._FIELDS_RICH, self._FIELDS_CORE, self._FIELDS_MINIMAL):
            gql = f"query($t:[String]){{ filterTokens(tokens:$t, limit:1){{ results {{ {fields} }} }} }}"
            try:
                data = await self.query(gql, {"t": [self.token_id(address)]})
            except CodexSchemaMismatch as exc:
                last = exc
                continue
            rows = ((data.get("filterTokens") or {}).get("results")) or []
            rows = [r for r in rows if r]
            return rows[0] if rows else None
        raise last or DataUnavailable("Codex filterTokens failed")

    async def discover(self, *, min_liquidity: float, limit: int = 40, created_after: int | None = None) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"network": [self.network_id], "liquidity": {"gte": float(min_liquidity)}}
        if created_after:
            filters["createdAt"] = {"gt": int(created_after)}
        gql = ("query($f:TokenFilters,$n:Int){ filterTokens(filters:$f, rankings:[{attribute:volume1, direction:DESC}], limit:$n)"
               "{ results { priceUSD liquidity token { address info { symbol name } } } } }")
        data = await self.query(gql, {"f": filters, "n": int(limit)})
        return [r for r in ((data.get("filterTokens") or {}).get("results") or []) if r]

    async def top10_holders_percent(self, address: str) -> float | None:
        data = await self.query("query($t:String!){ top10HoldersPercent(tokenId:$t) }", {"t": self.token_id(address)})
        v = data.get("top10HoldersPercent")
        return float(v) if isinstance(v, (int, float)) else None

    async def holders(self, address: str, *, cursor: str | None = None) -> dict[str, Any]:
        """Paid plans only. Returns {count, top10HoldersPercent, items:[{walletAddress, balance, shiftedBalance}], cursor}."""
        gql = ("query($i:HoldersInput!){ holders(input:$i){ count top10HoldersPercent cursor status "
               "items{ walletAddress balance shiftedBalance } } }")
        inp: dict[str, Any] = {"tokenId": self.token_id(address)}
        if cursor:
            inp["cursor"] = cursor
        return (await self.query(gql, {"i": inp})).get("holders") or {}

    async def bars(self, address: str, *, start: int, end: int, resolution: str, countback: int | None = None) -> dict[str, Any]:
        """OHLCV for the token's current top pool. ``symbolType: TOKEN`` follows a token across launchpad migrations."""
        gql = ("query($s:String!,$f:Int!,$t:Int!,$r:String!,$c:Int){ getBars(symbol:$s, from:$f, to:$t, resolution:$r, "
               "countback:$c, symbolType:TOKEN, removeEmptyBars:true){ t o h l c volume } }")
        data = await self.query(gql, {"s": self.token_id(address), "f": int(start), "t": int(end), "r": resolution, "c": countback})
        return data.get("getBars") or {}

    async def aclose(self) -> None:
        await self.client.aclose()
