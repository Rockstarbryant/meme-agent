"""Etherscan API V2 client for Arc (chain 5042): the secondary explorer source.

Etherscan V2 is one endpoint for many chains (``https://api.etherscan.io/v2/api?chainid=5042&...``) and lists Arc
mainnet. Per Etherscan's supported-chains page, Arc community endpoints are free until 2026-10-15 and need a Lite
plan or above afterwards; source-code / ABI endpoints stay available on the Free tier. This client is therefore used
for the two calls that stay free (``getsourcecode`` -> verification + proxy, ``getcontractcreation`` -> creator) and,
best-effort, for holder count / holder list while the community window is open. A plan-gated response is treated as
"unavailable" and the client stops asking for that action instead of retrying it every cycle.

Etherscan signals errors inside an HTTP 200 body (``status: "0"``), including rate limits, so the body is inspected.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable

V2_URL = "https://api.etherscan.io/v2/api"
_PLAN_HINTS = ("upgrade", "plan", "not supported for this chain", "pro endpoint", "api pro")
_RATE_HINTS = ("rate limit", "max calls per sec")


class EtherscanRateLimited(DataUnavailable):
    pass


class EtherscanPlanRequired(DataUnavailable):
    pass


class EtherscanV2Client:
    def __init__(self, api_key: str, *, chain_id: int = 5042, base_url: str = V2_URL, timeout_s: float = 8.0,
                 min_interval_s: float = 0.4, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key.strip()
        self.chain_id = int(chain_id)
        self.base_url = base_url
        self.min_interval_s = max(0.1, min_interval_s)
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._plan_gated: set[str] = set()

    async def _call(self, module: str, action: str, **params: Any) -> Any:
        key = f"{module}.{action}"
        if key in self._plan_gated:
            raise EtherscanPlanRequired(f"Etherscan {key} needs a paid plan on this chain")
        q = {"chainid": self.chain_id, "module": module, "action": action, "apikey": self.api_key, **params}
        async with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self._client.get(self.base_url, params=q)
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"Etherscan network error: {type(exc).__name__}") from exc
            finally:
                self._last = time.monotonic()
        if r.status_code == 429:
            raise EtherscanRateLimited("Etherscan HTTP 429")
        if r.status_code >= 400:
            raise DataUnavailable(f"Etherscan HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise DataUnavailable("Etherscan returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise DataUnavailable("Etherscan returned an unexpected payload")
        result = body.get("result")
        if str(body.get("status")) == "1":
            return result
        text = f"{body.get('message', '')} {result if isinstance(result, str) else ''}".lower()
        if any(h in text for h in _RATE_HINTS):
            raise EtherscanRateLimited("Etherscan rate limit reached")
        if any(h in text for h in _PLAN_HINTS):
            self._plan_gated.add(key)
            raise EtherscanPlanRequired(f"Etherscan {key} needs a paid plan on this chain")
        if "no transactions found" in text or "no records found" in text:
            return None
        raise DataUnavailable(f"Etherscan {key}: {str(result or body.get('message'))[:160]}")

    # ------------------------------------------------------------------ endpoints
    async def source_code(self, address: str) -> dict[str, Any]:
        """Verification + proxy facts. ``verified`` is True only when source code is actually published."""
        result = await self._call("contract", "getsourcecode", address=address)
        row = (result or [{}])[0] if isinstance(result, list) else {}
        verified = bool((row.get("SourceCode") or "").strip()) and "not verified" not in str(row.get("ABI", "")).lower()
        impl = (row.get("Implementation") or "").strip().lower() or None
        return {"verified": verified, "contract_name": row.get("ContractName") or None,
                "is_proxy": str(row.get("Proxy", "0")) == "1", "implementation": impl}

    async def contract_creation(self, address: str) -> dict[str, Any] | None:
        result = await self._call("contract", "getcontractcreation", contractaddresses=address)
        row = (result or [None])[0] if isinstance(result, list) else None
        if not row:
            return None
        return {"creator": (row.get("contractCreator") or "").lower() or None,
                "tx_hash": row.get("txHash") or None}

    async def token_holder_count(self, address: str) -> int | None:
        result = await self._call("token", "tokenholdercount", contractaddress=address)
        try:
            return int(result)
        except (TypeError, ValueError):
            return None

    async def token_holder_list(self, address: str, limit: int = 20) -> list[dict[str, Any]]:
        result = await self._call("token", "tokenholderlist", contractaddress=address, page=1, offset=limit)
        return result if isinstance(result, list) else []

    async def aclose(self) -> None:
        await self._client.aclose()
