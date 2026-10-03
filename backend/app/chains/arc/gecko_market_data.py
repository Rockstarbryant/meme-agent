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
    """Primary trending + market-data provider for Arc via GeckoTerminal.

    Discovery uses GeckoTerminal Trending (5m / 1h / 6h / 24h) and New Pools —
    the same views the GeckoTerminal app exposes — not launchpad factory events.
    Launchpad verification is no longer required for entry.

    Pacing is handled entirely inside ``GeckoTerminalClient._get`` (2.5s min
    interval + Retry-After on 429). ``pool_trades`` is optional enrichment: on
    failure the market state still returns with ``token_pools`` fields filled.
    """

    def __init__(
        self,
        client: GeckoTerminalClient,
        *,
        max_tokens: int = 40,
        cache_s: float = 90.0,
        min_pool_liquidity_usd: float = 0.0,
        trending_durations: tuple[str, ...] = ("5m", "1h", "6h", "24h"),
    ):
        self.client = client
        self.max_tokens = max(1, max_tokens)
        self.cache_s = max(5.0, cache_s)
        self.min_pool_liquidity_usd = max(0.0, min_pool_liquidity_usd)
        self.trending_durations = tuple(d for d in trending_durations if d in ("5m", "1h", "6h", "24h")) or ("5m", "1h")
        self._cache: dict[str, tuple[float, MarketState]] = {}
        # Pool objects already returned by the trending / new_pools lists (they carry the same attributes as the
        # per-token pools endpoint). Reusing them saves one rate-limited call per token.
        self._pool_hints: dict[str, tuple[float, dict, list]] = {}
        self.hint_ttl_s = 240.0

    def _extract_scored_tokens(
        self, payload: dict, scored: list[tuple[float, str]], seen: set[str], limit: int
    ) -> None:
        """Parse trending/new_pools payload into (liquidity_usd, address) pairs.

        Prefers higher-liquidity pools when ranking. Skips known native/placeholder
        addresses. Shared by every discovery source so trending durations and
        new_pools parse identically.
        """
        included = payload.get("included") or []
        zero = "0x" + ("0" * 40)

        def _candidate(address: str | None) -> str | None:
            if not address:
                return None
            addr = str(address).lower().strip()
            if not addr.startswith("0x") or len(addr) < 42:
                return None
            if addr == zero or addr in seen:
                return None
            if addr.startswith("0x3600") and addr.count("0") > 30:
                return None
            return addr

        for item in payload.get("data") or []:
            if len(scored) >= limit * 2:  # gather extra then rank/truncate later
                break
            attrs = _attr(item)
            liq = _num(attrs.get("reserve_in_usd")) or _num(attrs.get("liquidity_usd")) or 0.0
            if self.min_pool_liquidity_usd and liq < self.min_pool_liquidity_usd:
                continue
            candidates: list[str] = []
            for relation in ("base_token", "quote_token"):
                c = _candidate(_related_address(item, relation, included))
                if c:
                    candidates.append(c)
            for key in ("base_token_address", "quote_token_address"):
                c = _candidate(attrs.get(key))
                if c:
                    candidates.append(c)
            for addr in candidates:
                if addr in seen:
                    continue
                seen.add(addr)
                scored.append((liq, addr))
                self._pool_hints[addr] = (time.monotonic(), item, included)
        # Bound memory: drop hints that have expired.
        cutoff = time.monotonic() - self.hint_ttl_s
        for k in [k for k, v in self._pool_hints.items() if v[0] < cutoff]:
            self._pool_hints.pop(k, None)

    async def discover_tokens(self) -> list[str]:
        """Discover Arc tokens from GeckoTerminal Trending (5m/1h/6h/24h) + New Pools.

        Matches the GeckoTerminal app Trending tabs and New Pools. Each source is
        tried independently so one 429 does not zero out discovery. Results are
        ranked by pool liquidity (higher first) so thin/dead pools are deprioritized.
        """
        scored: list[tuple[float, str]] = []
        seen: set[str] = set()
        errors: list[str] = []

        sources: list[tuple[str, Any]] = [
            (f"trending_pools_{d}", (lambda dur=d: self.client.trending_pools(dur)))
            for d in self.trending_durations
        ]
        sources.append(("new_pools", self.client.new_pools))

        for name, call in sources:
            if len(scored) >= self.max_tokens * 2:
                break
            try:
                payload = await call()
            except DataUnavailable as exc:
                errors.append(f"{name}: {exc}")
                continue
            self._extract_scored_tokens(payload, scored, seen, self.max_tokens)

        if not scored and errors:
            raise DataUnavailable("GeckoTerminal discovery: " + " | ".join(errors))

        # Higher liquidity first, stable by address for ties.
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [addr for _, addr in scored[: self.max_tokens]]

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        cached = self._cache.get(token)
        if cached and time.monotonic() - cached[0] < self.cache_s:
            return cached[1]
        hint = self._pool_hints.get(token)
        if hint and time.monotonic() - hint[0] < self.hint_ttl_s:
            pools, included = [hint[1]], hint[2]
        else:
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
        # Skip the trades call for pools with no 5m activity: it cannot add information and it is the most
        # expensive call per token on the free tier.
        trades: list = []
        if (buys_5m or 0) + (sells_5m or 0) > 0 or (volume_5m or 0) > 0:
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