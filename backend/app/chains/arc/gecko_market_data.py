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
    """

    def __init__(self, client: GeckoTerminalClient, *, max_tokens: int = 10, cache_s: float = 60.0):
        self.client = client
        self.max_tokens = max(1, max_tokens)
        self.cache_s = max(5.0, cache_s)
        self._cache: dict[str, tuple[float, MarketState]] = {}

    async def discover_tokens(self) -> list[str]:
        """Discover recently listed Arc tokens from GeckoTerminal new_pools.

        GeckoTerminal's Arc payload does **not** put token addresses on
        ``attributes.base_token_address`` / ``quote_token_address`` (those are
        always null). Addresses live under ``relationships.*.data.id`` as
        ``arc_0x…``. Prefer relationship parsing; fall back to attribute fields
        for networks that still populate them.
        """
        try:
            payload = await self.client.new_pools()
        except DataUnavailable:
            # Discovery is optional for this provider; Arc RPC remains authoritative.
            raise
        tokens: list[str] = []
        seen: set[str] = set()
        included = payload.get("included") or []
        zero = "0x" + ("0" * 40)

        def _add(address: str | None) -> bool:
            if not address:
                return False
            addr = str(address).lower().strip()
            if not addr.startswith("0x") or len(addr) < 42:
                return False
            if addr == zero or addr in seen:
                return False
            # Skip obvious non-ERC20 placeholders sometimes returned for native assets.
            if addr.startswith("0x3600") and addr.count("0") > 30:
                return False
            seen.add(addr)
            tokens.append(addr)
            return True

        for item in payload.get("data") or []:
            attrs = _attr(item)
            # 1) Relationship ids (Arc / current GeckoTerminal shape)
            for relation in ("base_token", "quote_token"):
                _add(_related_address(item, relation, included))
                if len(tokens) >= self.max_tokens:
                    return tokens
            # 2) Attribute fields (other networks / older payload shape)
            for key in ("base_token_address", "quote_token_address"):
                _add(attrs.get(key))
                if len(tokens) >= self.max_tokens:
                    return tokens
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
        buys_1m = int(_num(m1.get("buys")) or 0)
        sells_1m = int(_num(m1.get("sells")) or 0)
        buys_5m = int(_num(m5.get("buys")) or 0)
        sells_5m = int(_num(m5.get("sells")) or 0)
        unique_buyers_5m = int(_num(m5.get("buyers")) or 0) or None
        unique_sellers_5m = int(_num(m5.get("sellers")) or 0) or None
        unique_buyers_1m = int(_num(m1.get("buyers")) or 0) or None
        unique_sellers_1m = int(_num(m1.get("sellers")) or 0) or None
        unique_buyers_15m = int(_num(m15.get("buyers")) or 0) or None
        unique_sellers_15m = int(_num(m15.get("sellers")) or 0) or None

        # One trade call gives more precise buy/sell USD pressure and a previous
        # 5m buyer baseline. It is cached so the free 30 calls/minute API is not
        # hammered on every runner cycle.
        trades_payload = await self.client.pool_trades(pool_address)
        trades = trades_payload.get("data") or []
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
        if not buy_volume_5m and not sell_volume_5m:
            buy_volume_5m = None
            sell_volume_5m = None

        recent_high = max(recent_prices, default=price or 0.0) or None
        state = MarketState(
            chain="arc", token_address=token, timestamp=now,
            pool_address=pool_address, pool_id=pool_id or None, symbol=symbol,
            token_created_at=created, price=price, market_cap=market_cap, liquidity=liquidity,
            price_change_1m=None, price_change_5m=price_change_5m, price_change_15m=price_change_15m,
            recent_high=recent_high, volume_1m=volume_1m, volume_5m=volume_5m, volume_15m=volume_15m,
            buy_volume_5m=buy_volume_5m, sell_volume_5m=sell_volume_5m,
            buys_1m=buys_1m or None, sells_1m=sells_1m or None, buys_5m=buys_5m or None, sells_5m=sells_5m or None,
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
