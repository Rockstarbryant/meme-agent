from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable


class DexScreenerClient:
    """Small async client for DexScreener's free public API.

    No API key. Public rate limit is documented as ~300 requests/minute for the
    token-pairs endpoints used here (https://docs.dexscreener.com/api/reference).
    This client paces itself conservatively below that so it never becomes the
    thing that trips *our own* provider's failure count.

    Coverage of a given chain (its ``chainId`` slug) is entirely up to
    DexScreener's indexers; a brand-new chain (e.g. Arc) may not be indexed yet.
    That is treated as an ordinary "no data from this optional provider" case,
    never as a hard failure of the runner.
    """

    BASE = "https://api.dexscreener.com"

    def __init__(self, *, timeout_s: float = 6.0, min_interval_s: float = 0.35):
        self.client = httpx.AsyncClient(timeout=timeout_s, headers={"accept": "application/json"})
        self.min_interval_s = max(0.05, min_interval_s)
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, path: str) -> dict[str, Any]:
        async with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.get(f"{self.BASE}{path}")
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"DexScreener request failed: {type(exc).__name__}: {exc}") from exc
            finally:
                self._last_request = time.monotonic()
        if r.status_code == 429:
            raise DataUnavailable("DexScreener rate limited (HTTP 429)")
        if r.status_code >= 400:
            raise DataUnavailable(f"DexScreener HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise DataUnavailable(f"DexScreener returned non-JSON response: {exc}") from exc

    async def token_pairs(self, chain_id: str, token_address: str) -> list[dict[str, Any]]:
        """GET /tokens/v1/{chainId}/{tokenAddresses} — up to 30 comma-separated addresses."""
        body = await self._get(f"/tokens/v1/{chain_id}/{token_address}")
        return body if isinstance(body, list) else []

    async def search(self, query: str) -> list[dict[str, Any]]:
        body = await self._get(f"/latest/dex/search?q={query}")
        return (body or {}).get("pairs") or []

    async def close(self) -> None:
        await self.client.aclose()
