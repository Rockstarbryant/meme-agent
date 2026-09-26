from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable


class DexPaprikaRateLimited(DataUnavailable):
    """Raised on HTTP 429. Retry-After has already been honored before raising."""


class DexPaprikaClient:
    """Async client for DexPaprika's free DEX data API.

    Free tier (keyless): 10,000 credits per rolling 30 days, 15 requests/min.
    Free registered key: 100,000 credits/month, 30 requests/min, plus access
    to reserve and transaction streams. The client paces itself to stay under
    the keyless 15/min limit unless a key is provided.

    Base URL: https://api.dexpaprika.com
    Docs: https://docs.dexpaprika.com
    """

    BASE = "https://api.dexpaprika.com"

    def __init__(self, *, timeout_s: float = 10.0, api_key: str | None = None, min_interval_s: float | None = None):
        self.api_key = api_key.strip() if api_key else None
        # Keyless: 15/min => 4.0s spacing. With key: 30/min => 2.0s spacing.
        self.min_interval_s = min_interval_s if min_interval_s is not None else (2.0 if self.api_key else 4.0)
        self.client = httpx.AsyncClient(timeout=timeout_s, headers={"accept": "application/json"})
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            try:
                r = await self.client.get(f"{self.BASE}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"DexPaprika network error: {type(exc).__name__}") from exc
            finally:
                self._last_request = time.monotonic()
            if r.status_code == 429:
                retry_after = _retry_after_s(r.headers.get("retry-after"), default=5.0, floor=self.min_interval_s * 2)
                await asyncio.sleep(retry_after)
                self._last_request = time.monotonic()
                raise DexPaprikaRateLimited(f"DexPaprika HTTP 429 (retry-after={retry_after:.0f}s)")
            if r.status_code in (401, 403, 404):
                raise DataUnavailable(f"DexPaprika HTTP {r.status_code}")
            if r.status_code >= 400:
                raise DataUnavailable(f"DexPaprika HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()
            except ValueError as exc:
                raise DataUnavailable("DexPaprika returned invalid JSON") from exc

    async def networks(self) -> list[dict]:
        return await self._get("/networks")

    async def filter_pools(self, network: str, *, created_after: int | None = None, created_before: int | None = None,
                           volume_24h_min: float | None = None, txns_24h_min: int | None = None,
                           sort_by: str = "created_at", sort_dir: str = "desc",
                           page: int = 1, limit: int = 50) -> dict:
        """Discover pools on a network.

        DexPaprika retired ``/networks/{network}/pools/filter`` in favor of
        ``/networks/{network}/pools/search`` (they returned HTTP 410 with a
        ``replacement`` pointing at the new path). This method:
          1. Tries the new ``/pools/search`` endpoint with the same params.
          2. Falls back to ``/pools`` (top pools, ordered by volume) if
             search returns 400/404/410.
        Callers receive a dict with a ``data`` (or legacy ``pools``) key and
        should not care which endpoint answered.
        """
        params: dict[str, Any] = {
            "page": page,
            "limit": min(100, max(1, limit)),
            "sort_by": sort_by,
            "sort_dir": sort_dir,
        }
        if created_after is not None:
            params["created_after"] = created_after
        if created_before is not None:
            params["created_before"] = created_before
        if volume_24h_min is not None:
            params["volume_24h_min"] = volume_24h_min
        if txns_24h_min is not None:
            params["txns_24h_min"] = txns_24h_min

        try:
            return await self._get(f"/networks/{network}/pools/search", params)
        except DataUnavailable as exc:
            msg = str(exc)
            if "HTTP 410" in msg or "HTTP 404" in msg or "HTTP 400" in msg:
                fallback_params = {
                    "page": page,
                    "limit": min(100, max(1, limit)),
                    "order_by": "volume_usd",
                    "sort": "desc",
                }
                return await self._get(f"/networks/{network}/pools", fallback_params)
            raise

    async def top_pools(self, network: str, *, limit: int = 20, order_by: str = "volume_usd", sort: str = "desc") -> dict:
        return await self._get(f"/networks/{network}/pools", {"page": 1, "limit": limit, "order_by": order_by, "sort": sort})

    async def pool_details(self, network: str, pool_address: str) -> dict:
        return await self._get(f"/networks/{network}/pools/{pool_address}")

    async def token_details(self, network: str, token_address: str) -> dict:
        return await self._get(f"/networks/{network}/tokens/{token_address}")

    async def token_pools(self, network: str, token_address: str) -> list[dict]:
        data = await self.token_details(network, token_address)
        return data.get("pools") or []

    async def aclose(self) -> None:
        await self.client.aclose()


def _retry_after_s(header: str | None, *, default: float, cap: float = 60.0, floor: float = 0.0) -> float:
    value: float | None = None
    if header:
        raw = header.strip()
        try:
            value = float(raw)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                value = (when - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                value = None
    if value is None:
        value = default
    return max(floor, min(cap, max(0.0, value)))