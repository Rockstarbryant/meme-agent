from __future__ import annotations

import asyncio
import itertools

import httpx

from app.core.errors import DataUnavailable


class RpcError(Exception):
    pass


class EvmRpcClient:
    """Minimal JSON-RPC client with retry and provider failover (Alchemy/QuickNode/public, via config)."""

    def __init__(self, urls: list[str], timeout: float = 8.0, retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None, backoff_s: float = 0.2):
        self.urls, self.retries, self.backoff_s = urls, retries, backoff_s
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list | None = None):
        if not self.urls:
            raise DataUnavailable("no RPC URL configured")
        last: Exception | None = None
        for url in self.urls:
            for attempt in range(self.retries + 1):
                try:
                    resp = await self._client.post(url, json={"jsonrpc": "2.0", "id": next(self._ids),
                                                               "method": method, "params": params or []})
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
                    await asyncio.sleep(self.backoff_s * (attempt + 1))
                    continue
                if "error" in data:
                    raise RpcError(str(data["error"]))
                return data.get("result")
        raise DataUnavailable(f"all RPC endpoints failed for {method}: {type(last).__name__}")

    async def aclose(self) -> None:
        await self._client.aclose()
