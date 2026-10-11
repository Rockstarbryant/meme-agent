"""OKX Onchain OS (OKX Web3) Market API client.

Auth (OKX standard): headers ``OK-ACCESS-KEY``, ``OK-ACCESS-SIGN`` = base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath[?query] + body)),
``OK-ACCESS-TIMESTAMP`` (ISO-8601 UTC with milliseconds), ``OK-ACCESS-PASSPHRASE`` and, for DEX endpoints, ``OK-ACCESS-PROJECT`` (the
project id shown in the developer portal). The secret key and passphrase come from env only and are never logged or audited.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.errors import DataUnavailable

_RATE_LIMIT_CODES = {"50011", "50013"}
_AUTH_CODES = {"50100", "50101", "50102", "50103", "50104", "50105", "50111", "50112", "50113", "50114"}


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    mac = hmac.new(secret.encode(), (timestamp + method.upper() + request_path + body).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OkxClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, *, project_id: str = "", base_url: str = "https://web3.okx.com",
                 timeout_s: float = 8.0, min_interval_s: float = 0.25, transport: httpx.AsyncBaseTransport | None = None,
                 clock=_now_iso):
        if not (api_key and secret_key and passphrase):
            raise ValueError("OKX needs API key, secret key and passphrase (https://web3.okx.com/onchainos/dev-portal)")
        self._key, self._secret, self._pass, self._project = api_key.strip(), secret_key.strip(), passphrase, project_id.strip()
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self.min_interval_s = max(0.0, min_interval_s)
        self._clock = clock
        self._last = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = self._clock()
        h = {"OK-ACCESS-KEY": self._key, "OK-ACCESS-SIGN": sign(self._secret, ts, method, path, body), "OK-ACCESS-TIMESTAMP": ts,
             "OK-ACCESS-PASSPHRASE": self._pass, "Content-Type": "application/json", "accept": "application/json"}
        if self._project:
            h["OK-ACCESS-PROJECT"] = self._project
        return h

    async def _request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> list[Any]:
        query = ("?" + urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        async with self._lock:
            cool = self._cooldown_until - time.monotonic()
            if cool > 0:
                raise DataUnavailable(f"OKX cooling down after rate limit ({cool:.0f}s left)")
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.request(method, f"{self.base}{path}{query}", headers=self._headers(method, path + query, payload),
                                              content=payload or None)
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"OKX network error: {type(exc).__name__}") from exc
            finally:
                self._last = time.monotonic()
        if r.status_code == 429:
            self._cooldown_until = time.monotonic() + 20.0
            raise DataUnavailable("OKX rate limited (HTTP 429)")
        if r.status_code in (401, 403):
            raise DataUnavailable(f"OKX rejected the credentials (HTTP {r.status_code}); check key, secret, passphrase and project id")
        if r.status_code >= 500:
            raise DataUnavailable(f"OKX server error HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise DataUnavailable(f"OKX returned non-JSON (HTTP {r.status_code})") from exc
        code = str(data.get("code", "")) if isinstance(data, dict) else ""
        if code != "0":
            msg = str(data.get("msg") if isinstance(data, dict) else "")[:160]
            if code in _RATE_LIMIT_CODES:
                self._cooldown_until = time.monotonic() + 20.0
                raise DataUnavailable(f"OKX rate limited (code {code}): {msg}")
            if code in _AUTH_CODES:
                raise DataUnavailable(f"OKX credentials rejected (code {code}): {msg}")
            raise DataUnavailable(f"OKX error code {code}: {msg}")
        rows = data.get("data")
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------ endpoints
    async def supported_chains(self) -> list[dict]:
        return await self._request("GET", "/api/v6/dex/market/supported/chain")

    async def toplist(self, chain_index: str, *, sort_by: int, time_frame: int) -> list[dict]:
        """sort_by: 2 = price change, 5 = volume, 6 = market cap. time_frame: 1 = 5m, 2 = 1h, 3 = 4h, 4 = 24h. Max 100 rows."""
        return await self._request("GET", "/api/v6/dex/market/token/toplist",
                                   {"chains": chain_index, "sortBy": sort_by, "timeFrame": time_frame})

    async def hot_tokens(self, chain_index: str, *, ranking_type: int = 4) -> list[dict]:
        """ranking_type: 4 = trending score, 5 = X mentions."""
        return await self._request("GET", "/api/v6/dex/market/token/hot-token", {"rankingType": ranking_type, "chainIndex": chain_index})

    async def price_info(self, chain_index: str, token: str) -> dict | None:
        rows = await self._request("POST", "/api/v6/dex/market/price-info", body=[{"chainIndex": chain_index, "tokenContractAddress": token.lower()}])
        return rows[0] if rows else None

    async def aclose(self) -> None:
        await self.client.aclose()
