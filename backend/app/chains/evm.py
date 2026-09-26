from __future__ import annotations

import asyncio
import itertools
import re
from typing import Callable

import httpx

from app.core.errors import DataUnavailable

# Provider wording for "your fromBlock/toBlock span is too wide" refusals. Not
# an exhaustive list, but covers Alchemy, Infura, QuickNode and most public
# nodes' actual phrasing (see e.g. Alchemy's "eth_getLogs is limited to a
# 10,000 range" / "Log response size exceeded... 2K block range", Infura's
# "query returned more than 10000 results" and the generic "block range too
# large/wide"). Deliberately broad (word fragments, not full sentences) since
# exact wording differs per provider and per chain, and a false-negative here
# means silently retrying a doomed request forever instead of narrowing it.
_RANGE_ERROR_PATTERNS = re.compile(
    r"(block range|range is too|range too (large|wide)|log response size|"
    r"more than \d+ results|limited to a[n]? \d[\d,]* (block )?range|"
    r"exceeds the range|query returned more than)",
    re.IGNORECASE,
)


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _looks_like_range_error(exc: Exception) -> bool:
    return bool(_RANGE_ERROR_PATTERNS.search(str(exc)))


class EvmRpcClient:
    """Minimal JSON-RPC client with retry, provider failover (Alchemy/QuickNode/public,
    via config), and adaptive eth_getLogs range narrowing.

    Providers commonly refuse an eth_getLogs call whose fromBlock..toBlock span is
    "too wide" (Alchemy: 10,000 blocks on most chains but as low as 2,000 on some;
    Infura/QuickNode/public nodes: often 1,000-10,000; a new chain like Arc may have
    its own, undocumented limit). That refusal is a normal 400/JSON-RPC error, not a
    transient fault — retrying the identical request just fails identically forever
    (this was a real production bug: repeated immediate 400s from Alchemy on Arc).
    get_logs() below detects that refusal and narrows the chunk size instead.

    The learned chunk size (``_log_range_cap``) is remembered on this client
    instance. Cloud workers, which rebuild the runner runtime every cycle, pass
    ``on_range_cap`` / ``initial_range_cap`` to persist and hydrate it via the
    durable local store so the same 400 burst is not paid every cycle.
    """

    def __init__(self, urls: list[str], timeout: float = 8.0, retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None, backoff_s: float = 0.2,
                 max_log_range: int = 2000, min_log_range: int = 50,
                 on_range_cap: Callable[[int], None] | None = None,
                 initial_range_cap: int | None = None):
        self.urls, self.retries, self.backoff_s = urls, retries, backoff_s
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._ids = itertools.count(1)
        self.max_log_range = max(min_log_range, max_log_range)
        self.min_log_range = max(1, min_log_range)
        # Learned once a provider refuses a range as too wide; reused for every
        # subsequent get_logs() call on this client instance so we stop paying
        # for the same discovery (and the same burst of 400s) every poll cycle.
        self._log_range_cap: int | None = (
            initial_range_cap if isinstance(initial_range_cap, int) and initial_range_cap > 0 else None
        )
        self._on_range_cap = on_range_cap

    async def call(self, method: str, params: list | None = None):
        if not self.urls:
            raise DataUnavailable("no RPC URL configured")
        last: Exception | None = None
        for url in self.urls:
            for attempt in range(self.retries + 1):
                try:
                    resp = await self._client.post(url, json={"jsonrpc": "2.0", "id": next(self._ids),
                                                               "method": method, "params": params or []})
                except httpx.HTTPError as exc:
                    last = exc
                    await asyncio.sleep(self.backoff_s * (attempt + 1))
                    continue
                # Parse the body before checking the HTTP status: providers (Alchemy
                # included) return a real JSON-RPC error object — with the actual
                # reason, e.g. "range too large" — on a non-2xx status. Raising on
                # raise_for_status() first would discard that body and this method's
                # caller would never learn *why* the call failed, only that it did.
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and "error" in data:
                    err = data["error"] or {}
                    raise RpcError(str(err.get("message") or err), code=err.get("code") if isinstance(err, dict) else None)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # A bare HTTP 400 from an RPC provider is almost always a
                    # bad-request filter refusal (e.g. eth_getLogs range too
                    # large) returned without a proper JSON-RPC error object.
                    # Surface it as RpcError so get_logs()'s narrowing logic
                    # can act on it instead of retrying an identical doomed
                    # request until it gives up.
                    if resp.status_code == 400:
                        raise RpcError(f"HTTP 400: {resp.text[:200]}") from exc
                    last = exc
                    await asyncio.sleep(self.backoff_s * (attempt + 1))
                    continue
                if data is None:
                    last = ValueError(f"non-JSON response from {url}")
                    await asyncio.sleep(self.backoff_s * (attempt + 1))
                    continue
                return data.get("result")
        raise DataUnavailable(f"all RPC endpoints failed for {method}: {type(last).__name__}: {last}")

    async def get_logs(self, address: str, topics: list, from_block: int, to_block: int) -> list[dict]:
        """eth_getLogs over [from_block, to_block], chunked to fit whatever range the
        provider currently accepts, narrowing automatically on a range-too-large
        refusal (halves the chunk, floor at min_log_range) rather than retrying the
        same doomed request. The learned chunk size is remembered on this client for
        subsequent calls (see _log_range_cap) and pushed to on_range_cap (if set) so
        a cloud worker can persist it across cycles."""
        cap = min(self._log_range_cap or self.max_log_range, max(1, to_block - from_block + 1))
        rows: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + cap - 1)
            try:
                got = await self.call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": address, "topics": topics}])
            except RpcError as exc:
                if _looks_like_range_error(exc) and cap > self.min_log_range:
                    cap = max(self.min_log_range, cap // 2)
                    self._log_range_cap = cap
                    if self._on_range_cap is not None:
                        try:
                            self._on_range_cap(cap)
                        except Exception:
                            pass
                    continue  # retry this same [start, ...] window with the smaller cap
                raise
            rows.extend(got or [])
            start = end + 1
        return rows

    async def aclose(self) -> None:
        await self._client.aclose()