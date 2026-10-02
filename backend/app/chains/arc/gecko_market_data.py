from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState
from app.integrations.geckoterminal import GeckoTerminalClient


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _count(v: Any) -> int | None:
    """Non-negative integer count that PRESERVES zero. Zero buyers is real data ("nobody bought"), not a gap."""
    x = _num(v)
    return None if x is None else int(x)


def _pct(v: Any) -> float | None:
    x = _num(v)
    return x


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _attr(item: dict) -> dict:
    return (item or {}).get("attributes") or {}


def _related_address(item: dict, relation: str, included: list[dict]) -> str:
    rid = (((item or {}).get("relationships") or {}).get(relation) or {}).get("data") or {}
    rid_value = str(rid.get("id") or "").lower()
    if rid_value.startswith("arc_"):
        return rid_value.split("_", 1)[1]
    for obj in included:
        if str(obj.get("id") or "").lower() == rid_value:
            address = str((_attr(obj).get("address") or "")).lower()
            if address:
                return address
    return ""


def _tx_stat(attrs: dict, window: str, side: str, field: str) -> float | None:
    tx = attrs.get("transactions") or {}
    row = tx.get(window) or {}
    side_row = row.get(side) if isinstance(row, dict) else None
    if isinstance(side_row, dict):
        return _num(side_row.get(field))
    return _num(row.get(f"{side}_{field}")) if isinstance(row, dict) else None


class GeckoTerminalArcMarketData(MarketDataProvider):
    """GeckoTerminal enrichment provider for Arc.

    Discovery is intentionally not authoritative here: Arc RPC discovers the
    launchpad-created contracts, while GeckoTerminal supplies DEX market data
    for those addresses. This keeps launchpad provenance on-chain.

    Pacing is handled entirely inside ``GeckoTerminalClient._get`` (see its
    docstring: 2.5s min interval, plus Retry-After honoring on 429), so this
    provider does not add a second pacing layer. The only local resilience is
    that ``pool_trades`` is treated as optional enrichment: when it fails
    (rate-limited, no coverage, transient 5xx), the market state still returns
    with the ``token_pools`` fields populated and buy/sell USD fields unset,
    rather than raising and losing the whole cycle.
    """

    def __init__(self, client: GeckoTerminalClient, *, max_tokens: int = 10, cache_s: float = 60.0):
        self.client = client
        self.max_tokens = max(1, max_tokens)
        self.cache_s = max(5.0, cache_s)
        self._cache: dict[str, tuple[float, MarketState]] = {}

    @staticmethod
    def _extract_token_addresses(payload: dict, tokens: list[str], seen: set[str], limit: int) -> None:
        """Appends up to ``limit`` (total) token addresses found in a new_pools/trending_pools-shaped payload
        into ``tokens``, skipping ones already in ``seen``. Shared by every discovery source below so new_pools
        and each trending_pools(duration) call parse addresses identically."""
        included = payload.get("included") or []
        zero = "0x" + ("0" * 40)

        def _add(address: str | None) -> None:
            if len(tokens) >= limit or not address:
                return
            addr = str(address).lower().strip()
            if not addr.startswith("0x") or len(addr) < 42:
                return
            if addr == zero or addr in seen:
                return
            # Skip obvious non-ERC20 placeholders sometimes returned for native assets.
            if addr.startswith("0x3600") and addr.count("0") > 30:
                return
            seen.add(addr)
            tokens.append(addr)

        for item in payload.get("data") or []:
            if len(tokens) >= limit:
                return
            attrs = _attr(item)
            # 1) Relationship ids (Arc / current GeckoTerminal shape)
            for relation in ("base_token", "quote_token"):
                _add(_related_address(item, relation, included))
            # 2) Attribute fields (other networks / older payload shape)
            for key in ("base_token_address", "quote_token_address"):
                _add(attrs.get(key))

    async def discover_tokens(self) -> list[str]:
        """Discover Arc tokens from GeckoTerminal: new pools PLUS trending pools (5m and 1h windows) -- i.e. the
        same two views ("New Pools" and "Trending") the GeckoTerminal app itself shows. Each source is tried
        independently so one failing (e.g. a 429 on trending_pools) doesn't take discovery down to zero; only if
        every source fails does this raise, matching the previous new_pools-only behavior.

        GeckoTerminal's Arc payload does **not** put token addresses on
        ``attributes.base_token_address`` / ``quote_token_address`` (those are
        always null). Addresses live under ``relationships.*.data.id`` as
        ``arc_0x…``. Prefer relationship parsing; fall back to attribute fields
        for networks that still populate them.
        """
        tokens: list[str] = []
        seen: set[str] = set()
        errors: list[str] = []
        # (name, thunk) pairs -- calling the client method is deferred into the loop body so a source we never
        # reach (budget already full) never has its coroutine created, let alone left unawaited.
        sources = (
            ("trending_pools_5m", lambda: self.client.trending_pools("5m")),
            ("trending_pools_1h", lambda: self.client.trending_pools("1h")),
            ("new_pools", self.client.new_pools),
        )
        for name, call in sources:
            if len(tokens) >= self.max_tokens:
                break
            try:
                payload = await call()
            except DataUnavailable as exc:
                errors.append(f"{name}: {exc}")
                continue
            self._extract_token_addresses(payload, tokens, seen, self.max_tokens)
        if not tokens and errors:
            # Discovery is optional for this provider; Arc RPC / other providers remain authoritative.
            raise DataUnavailable("GeckoTerminal discovery: " + " | ".join(errors))
        return tokens

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        cached = self._cache.get(token)
        if cached and time.monotonic() - cached[0] < self.cache_s:
            return cached[1]
        payload = await self.client.token_pools(token)
        pools = payload.get("data") or []
        included = payload.get("included") or []
        if not pools:
            raise DataUnavailable(f"GeckoTerminal has no pool for {token}")
        pools = sorted(pools, key=lambda x: (_num(_attr(x).get("reserve_in_usd")) or 0.0), reverse=True)
        pool = pools[0]
        attrs = _attr(pool)
        pool_id = str(pool.get("id") or "")
        pool_address = pool_id.split("_")[-1] if "_" in pool_id else attrs.get("address")
        if not pool_address:
            raise DataUnavailable(f"GeckoTerminal pool address missing for {token}")

        base_address = str(attrs.get("base_token_address") or "").lower() or _related_address(pool, "base_token", included)
        quote_address = str(attrs.get("quote_token_address") or "").lower() or _related_address(pool, "quote_token", included)
        base_is_target = base_address == token
        price = _num(attrs.get("base_token_price_usd")) if base_is_target else _num(attrs.get("quote_token_price_usd"))
        if price is None:
            price = _num(attrs.get("price_usd"))
        liquidity = _num(attrs.get("reserve_in_usd"))
        market_cap = _num(attrs.get("market_cap_usd"))
        if market_cap is None:
            market_cap = _num(attrs.get("fdv_usd"))
        created = _ts(attrs.get("pool_created_at"))
        symbol = attrs.get("name") or attrs.get("base_token_symbol")
        changes = attrs.get("price_change_percentage") or {}
        price_change_5m = _pct(changes.get("m5"))
        price_change_1h = _pct(changes.get("h1"))
        # h1 is a better available cross-check than inventing a 15m value.
        price_change_15m = _pct(changes.get("m15"))
        volume = attrs.get("volume_usd") or {}
        volume_1m = _num(volume.get("m1"))
        volume_5m = _num(volume.get("m5"))
        volume_15m = _num(volume.get("m15"))
        txs = attrs.get("transactions") or {}
        m1 = txs.get("m1") or {}
        m5 = txs.get("m5") or {}
        m15 = txs.get("m15") or {}
        buys_1m, sells_1m = _count(m1.get("buys")), _count(m1.get("sells"))
        buys_5m, sells_5m = _count(m5.get("buys")), _count(m5.get("sells"))
        unique_buyers_5m, unique_sellers_5m = _count(m5.get("buyers")), _count(m5.get("sellers"))
        unique_buyers_1m, unique_sellers_1m = _count(m1.get("buyers")), _count(m1.get("sellers"))
        unique_buyers_15m, unique_sellers_15m = _count(m15.get("buyers")), _count(m15.get("sellers"))

        # One trade call gives more precise buy/sell USD pressure and a previous
        # 5m buyer baseline. It is optional enrichment: if the free tier rate
        # limits us (the client honors Retry-After before raising), or the pool
        # has no trades endpoint coverage, we still return a usable state with
        # those specific fields left unset instead of losing the whole cycle.
        try:
            trades_payload = await self.client.pool_trades(pool_address)
            trades = trades_payload.get("data") or []
        except DataUnavailable:
            trades = []
        now = datetime.now(timezone.utc)
        buy_volume_5m = sell_volume_5m = 0.0
        buyers_recent: set[str] = set()
        buyers_prev: set[str] = set()
        recent_prices: list[float] = []
        for row in trades:
            a = _attr(row)
            ts = _ts(a.get("block_timestamp"))
            if not ts:
                continue
            age = (now - ts).total_seconds()
            if age < 0 or age > 600:
                continue
            kind = str(a.get("kind") or "").lower()
            usd = _num(a.get("volume_usd")) or 0.0
            trader = str(a.get("tx_from_address") or a.get("from_address") or "").lower()
            if age <= 300:
                if kind == "buy":
                    buy_volume_5m += usd
                    if trader: buyers_recent.add(trader)
                elif kind == "sell":
                    sell_volume_5m += usd
            elif age <= 600 and kind == "buy" and trader:
                buyers_prev.add(trader)
            p = _num(a.get("price_from_in_usd")) or _num(a.get("price_to_in_usd"))
            if p:
                recent_prices.append(p)
        unique_buyers_prev_5m = len(buyers_prev) or None
        basis = "usd"
        if not buy_volume_5m and not sell_volume_5m:
            # No usable trade rows (rate limited / empty page). Leave the split unset; the registry derives a
            # count-based estimate from buys/sells and labels it as such.
            buy_volume_5m = None
            sell_volume_5m = None
            basis = None

        recent_high = max(recent_prices, default=price or 0.0) or None
        state = MarketState(
            chain="arc", token_address=token, timestamp=now,
            pool_address=pool_address, pool_id=pool_id or None, symbol=symbol,
            token_created_at=created, price=price, market_cap=market_cap, liquidity=liquidity,
            price_change_1m=None, price_change_5m=price_change_5m, price_change_15m=price_change_15m,
            recent_high=recent_high, volume_1m=volume_1m, volume_5m=volume_5m, volume_15m=volume_15m,
            buy_volume_5m=buy_volume_5m, sell_volume_5m=sell_volume_5m,
            buys_1m=buys_1m, sells_1m=sells_1m, buys_5m=buys_5m, sells_5m=sells_5m, buy_sell_basis=basis,
            unique_buyers_1m=unique_buyers_1m, unique_buyers_5m=unique_buyers_5m,
            unique_buyers_15m=unique_buyers_15m, unique_buyers_prev_5m=unique_buyers_prev_5m,
            unique_sellers_1m=unique_sellers_1m, unique_sellers_5m=unique_sellers_5m, unique_sellers_15m=unique_sellers_15m,
            contract=ContractInfo(verified=None),
            data_sources=["geckoterminal:pools", "geckoterminal:trades"], is_demo=False,
        )
        self._cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        await self.client.aclose()