"""GoldRush (Covalent) REST client for Arc.

Verified: Arc mainnet is supported as ``arc-mainnet`` (chain id 5042) in the Foundational API (a "Frontier" chain), with
token balances, transactions, approvals and (where indexed) token holders. Auth is ``Authorization: Bearer <key>``.
The holders endpoint is ``GET /v1/{chain}/tokens/{token}/token_holders_v2/`` (paged; ``pagination.total_count`` is the
holder count). Frontier chains may not support every endpoint, so any 4xx for a specific endpoint is surfaced as
``DataUnavailable`` and the caller degrades to a gap, never to a made-up value.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable


class GoldRushClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.covalenthq.com/v1", chain: str = "arc-mainnet",
                 timeout_s: float = 8.0, min_interval_s: float = 0.25, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("GoldRush needs an API key (https://goldrush.dev)")
        self.base_url, self.chain = base_url.rstrip("/"), chain
        self._key = api_key.strip()
        self.client = httpx.AsyncClient(timeout=timeout_s, transport=transport, headers={"accept": "application/json"})
        self.min_interval_s = max(0.0, min_interval_s)
        self._last = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            cool = self._cooldown_until - time.monotonic()
            if cool > 0:
                raise DataUnavailable(f"GoldRush cooling down after rate limit ({cool:.0f}s left)")
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.get(f"{self.base_url}{path}", params=params, headers={"Authorization": f"Bearer {self._key}"})
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"GoldRush network error: {type(exc).__name__}") from exc
            finally:
                self._last = time.monotonic()
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", "") or 30.0)
            except ValueError:
                ra = 30.0
            self._cooldown_until = time.monotonic() + min(120.0, max(5.0, ra))
            raise DataUnavailable(f"GoldRush rate limited (HTTP 429, retry-after={ra:.0f}s)")
        if r.status_code in (401, 402, 403):
            raise DataUnavailable(f"GoldRush rejected the request (HTTP {r.status_code}: key invalid, out of credits or plan)")
        if r.status_code >= 400:
            raise DataUnavailable(f"GoldRush HTTP {r.status_code}: {r.text[:160]}")
        try:
            body = r.json()
        except ValueError as exc:
            raise DataUnavailable("GoldRush returned non-JSON") from exc
        if isinstance(body, dict) and body.get("error"):
            raise DataUnavailable(f"GoldRush error: {str(body.get('error_message') or body.get('error_code') or 'unknown')[:160]}")
        return body

    async def token_holders(self, token: str, *, page_size: int = 100, page_number: int = 0) -> dict[str, Any]:
        """Returns {"items": [{"address", "balance", "total_supply", "contract_decimals"}], "pagination": {...}}."""
        body = await self._get(f"/{self.chain}/tokens/{token.lower()}/token_holders_v2/",
                               {"page-size": page_size, "page-number": page_number})
        data = body.get("data") or {}
        return {"items": data.get("items") or [], "pagination": data.get("pagination") or {}}

    async def wallet_token_balances(self, wallet: str) -> list[dict[str, Any]]:
        """ERC-20 balances of a wallet, used to cross-check what the wallet provider reports."""
        body = await self._get(f"/{self.chain}/address/{wallet.lower()}/balances_v2/", {"no-spam": "true"})
        return ((body.get("data") or {}).get("items")) or []

    async def aclose(self) -> None:
        await self.client.aclose()
