from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable


class GeckoTerminalClient:
    """Small async client for GeckoTerminal's free public API.

    The client deliberately paces requests below the documented 30 calls/minute
    public limit and keeps the actual network id configurable because new chains
    can appear in GeckoTerminal under a provider-specific slug.
    """

    def __init__(self, base_url: str, network: str, *, timeout_s: float = 6.0, min_interval_s: float = 2.1, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.network = network.strip()
        self.api_key = api_key.strip() if api_key else None
        self.client = httpx.AsyncClient(timeout=timeout_s, headers={"accept": "application/json"})
        self.min_interval_s = max(0.1, min_interval_s)
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            headers = {}
            if self.api_key:
                # CoinGecko-compatible key header; public GeckoTerminal does not require it.
                headers["x-cg-demo-api-key"] = self.api_key
            try:
                r = await self.client.get(f"{self.base_url}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"GeckoTerminal network error: {type(exc).__name__}") from exc
            finally:
                self._last_request = time.monotonic()
            if r.status_code in (401, 403, 404, 429):
                raise DataUnavailable(f"GeckoTerminal HTTP {r.status_code}")
            if r.status_code >= 400:
                raise DataUnavailable(f"GeckoTerminal HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()
            except ValueError as exc:
                raise DataUnavailable("GeckoTerminal returned invalid JSON") from exc

    async def token_pools(self, token: str) -> dict[str, Any]:
        return await self._get(f"/networks/{self.network}/tokens/{token}/pools", {"page": 1, "include": "base_token,quote_token,dex"})

    async def token_info(self, token: str) -> dict[str, Any]:
        return await self._get(f"/networks/{self.network}/tokens/{token}")

    async def pool_trades(self, pool: str) -> dict[str, Any]:
        return await self._get(f"/networks/{self.network}/pools/{pool}/trades", {"page": 1})

    async def new_pools(self) -> dict[str, Any]:
        return await self._get(f"/networks/{self.network}/new_pools", {"page": 1})

    async def aclose(self) -> None:
        await self.client.aclose()
