"""Blockscout client for Arc (chain 5042): token info, holders, address/contract metadata.

Blockscout is Arc's official block explorer. Two access routes, both speaking the same REST v2 API:

  * Pro API   ``https://api.blockscout.com/{chain_id}/api/v2/...``  (key ``proapi_...`` as Bearer token)
              Free tier: 100K credits/day at 5 requests/second; most endpoints cost 20 credits, so roughly
              5,000 calls/day. Every response carries ``x-credits-remaining`` / ``x-ratelimit-remaining``.
  * Public    ``https://explorer.arc.io/api/v2/...``  (keyless, low rate limit, may be permissioned)

The client therefore:
  * paces requests below the route's rate limit,
  * keeps a DAILY CREDIT BUDGET (default 90K of the 100K free credits) and stops spending on the Pro route when it
    is used up, falling back to the keyless public route instead of failing,
  * treats a 404 as "not indexed (yet)": brand-new tokens routinely are, so that is a normal miss, not an outage.

Everything raises ``DataUnavailable`` (never fabricates). Callers cache aggressively; see app/enrichment/service.py.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.errors import DataUnavailable

PRO_ROOT = "https://api.blockscout.com"
PUBLIC_ROOT = "https://explorer.arc.io"


class BlockscoutNotFound(DataUnavailable):
    """404: the address/token is not indexed (yet)."""


class BlockscoutRateLimited(DataUnavailable):
    """429. Retry-After has already been honoured (bounded) before this is raised."""


class BlockscoutBudgetExhausted(DataUnavailable):
    """The daily credit budget is spent and no public fallback is allowed."""


def _retry_after_s(header: str | None, *, default: float = 3.0, cap: float = 10.0) -> float:
    try:
        return max(0.0, min(cap, float(header))) if header else default
    except ValueError:
        return default


class BlockscoutClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        chain_id: int = 5042,
        pro_root: str = PRO_ROOT,
        public_root: str = PUBLIC_ROOT,
        timeout_s: float = 8.0,
        daily_credit_budget: int = 90_000,
        credit_cost: int = 20,
        allow_public_fallback: bool = True,
        pro_interval_s: float = 0.25,
        public_interval_s: float = 0.6,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.chain_id = int(chain_id)
        self.pro_base = f"{pro_root.rstrip('/')}/{self.chain_id}/api/v2"
        self.public_base = f"{public_root.rstrip('/')}/api/v2"
        self.daily_credit_budget = max(0, int(daily_credit_budget))
        self.credit_cost = max(1, int(credit_cost))
        self.allow_public_fallback = allow_public_fallback
        self._intervals = {"pro": max(0.05, pro_interval_s), "public": max(0.1, public_interval_s)}
        self._last = {"pro": 0.0, "public": 0.0}
        self._locks = {"pro": asyncio.Lock(), "public": asyncio.Lock()}
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport, headers={"accept": "application/json"})
        self._public_blocked_until = 0.0
        self._day = self._utc_day()
        self.credits_used_today = 0
        self.credits_remaining: int | None = None  # last value reported by the server
        self.calls = {"pro": 0, "public": 0}

    # ------------------------------------------------------------------ budget
    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._utc_day()
        if today != self._day:
            self._day, self.credits_used_today, self.credits_remaining = today, 0, None

    def _pro_available(self) -> bool:
        if not self.api_key:
            return False
        self._roll_day()
        if self.credits_used_today + self.credit_cost > self.daily_credit_budget:
            return False
        if self.credits_remaining is not None and self.credits_remaining < self.credit_cost:
            return False
        return True

    def status(self) -> dict:
        self._roll_day()
        return {
            "mode": "pro" if self.api_key else "public",
            "credits_used_today": self.credits_used_today,
            "daily_credit_budget": self.daily_credit_budget,
            "credits_remaining_reported": self.credits_remaining,
            "pro_available": self._pro_available(),
            "calls": dict(self.calls),
        }

    # ------------------------------------------------------------------ transport
    def _public_ok(self) -> bool:
        return time.monotonic() >= self._public_blocked_until

    async def _request(self, route: str, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        base = self.pro_base if route == "pro" else self.public_base
        headers = {"authorization": f"Bearer {self.api_key}"} if route == "pro" else {}
        async with self._locks[route]:
            wait = self._intervals[route] - (time.monotonic() - self._last[route])
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self._client.get(f"{base}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise DataUnavailable(f"Blockscout network error ({route}): {type(exc).__name__}") from exc
            finally:
                self._last[route] = time.monotonic()
            self.calls[route] += 1
            if route == "pro":
                self.credits_used_today += self.credit_cost
                rem = r.headers.get("x-credits-remaining")
                if rem and rem.strip().lstrip("-").isdigit():
                    self.credits_remaining = int(rem)
            if r.status_code == 429:
                delay = _retry_after_s(r.headers.get("retry-after"))
                await asyncio.sleep(delay)
                raise BlockscoutRateLimited(f"Blockscout HTTP 429 ({route}, retry-after={delay:.0f}s)")
        if r.status_code == 404:
            raise BlockscoutNotFound(f"Blockscout HTTP 404 for {path}")
        if r.status_code in (401, 402, 403):
            # Rejected / exhausted key (pro) or a permissioned public instance: stop asking for a while.
            if route == "pro":
                self.credits_remaining = 0
            else:
                self._public_blocked_until = time.monotonic() + 300.0
            raise DataUnavailable(f"Blockscout HTTP {r.status_code} ({route})")
        if r.status_code >= 400:
            raise DataUnavailable(f"Blockscout HTTP {r.status_code} ({route}): {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as exc:
            raise DataUnavailable("Blockscout returned invalid JSON") from exc
        return body if isinstance(body, dict) else {"items": body}

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, prefer_public: bool = False) -> dict[str, Any]:
        """Route selection. ``prefer_public`` is for cheap, static lookups (creator, verification) so the Pro credit
        budget is kept for the data that has to be fresh (holders). Either route falls back to the other."""
        order: list[str] = []
        if prefer_public:
            order = (["public"] if self._public_ok() else []) + (["pro"] if self._pro_available() else [])
        else:
            order = (["pro"] if self._pro_available() else []) + (
                ["public"] if (self._public_ok() and (self.allow_public_fallback or not self.api_key)) else [])
        if not order:
            raise BlockscoutBudgetExhausted("Blockscout unavailable: daily credit budget spent and no public route")
        last: Exception | None = None
        for route in order:
            try:
                return await self._request(route, path, params)
            except BlockscoutNotFound:
                raise  # a real answer, no point asking the other route
            except DataUnavailable as exc:
                last = exc
        assert last is not None
        raise last

    # ------------------------------------------------------------------ endpoints
    async def address_info(self, address: str) -> dict[str, Any]:
        """Creator, creation tx, verification and proxy metadata for an address/contract."""
        return await self._get(f"/addresses/{address}", prefer_public=True)

    async def token_info(self, address: str) -> dict[str, Any]:
        """name/symbol/decimals/total_supply/holders_count for an ERC-20."""
        return await self._get(f"/tokens/{address}")

    async def token_holders(self, address: str, page_params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One page (top holders by balance, ~50 rows). ``page_params`` is the previous page's next_page_params."""
        return await self._get(f"/tokens/{address}/holders", page_params or None)

    async def token_holders_rows(self, address: str, max_rows: int = 50) -> tuple[list[dict[str, Any]], bool]:
        """Top holders by balance, following pagination until ``max_rows`` rows (50 per page, 20 credits each on
        the Pro route). Returns (rows, complete) where ``complete`` is True when the last page was reached, i.e. the
        rows are ALL holders."""
        rows: list[dict[str, Any]] = []
        params: dict[str, Any] | None = None
        complete = False
        for _ in range(max(1, (max(1, max_rows) + 49) // 50)):
            data = await self.token_holders(address, params)
            items = data.get("items") if isinstance(data, dict) else None
            rows.extend(i for i in (items or []) if isinstance(i, dict))
            nxt = data.get("next_page_params") if isinstance(data, dict) else None
            if not nxt:
                complete = True
                break
            params = nxt if isinstance(nxt, dict) else None
            if params is None or len(rows) >= max_rows:
                break
        return rows[:max_rows] if len(rows) > max_rows else rows, complete

    async def aclose(self) -> None:
        await self._client.aclose()
