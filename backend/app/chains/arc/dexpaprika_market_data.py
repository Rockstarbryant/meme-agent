from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState
from app.integrations.dexpaprika import DexPaprikaClient


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        # DexPaprika uses seconds; some endpoints return milliseconds.
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _estimate_buy_sell_usd(
    volume_5m: float | None,
    buys_5m: int | None,
    sells_5m: int | None,
) -> tuple[float | None, float | None]:
    """Split total USD volume by txn counts when the API has no buy/sell USD."""
    if volume_5m is None or volume_5m <= 0:
        return None, None
    b = int(buys_5m or 0)
    s = int(sells_5m or 0)
    total = b + s
    if total <= 0:
        return None, None
    return volume_5m * (b / total), volume_5m * (s / total)


class DexPaprikaArcMarketData(MarketDataProvider):
    """DexPaprika provider for Arc.

    Three roles:
    - New-launch discovery: ``/networks/arc/pools/search`` (``created_after``)
      returns recently created pools that already have activity
      (``txns_24h_min``), which is a much better signal than GeckoTerminal's
      raw ``new_pools`` list — most brand-new pools are dead on arrival. We
      filter to pools with at least one transaction. Continuous scanning of
      brand-new launches is the higher-risk, higher-churn mode; it is kept on
      by default today (``scan_new_launches=True``) but is intended to become
      an opt-in/premium mode later — see ``scan_new_launches``.
    - Established-token discovery (``scan_established``): the SAME endpoint
      with ``created_before`` instead of ``created_after`` — pools older than
      ``established_min_age_hours``. These are naturally "tracked while there
      is momentum, dropped once there isn't": the query re-runs every cycle
      and a token that has gone quiet simply stops appearing near the top of
      results, while one still trading actively keeps showing up. The actual
      holder-count/market-cap floor (``established_min_holders`` /
      ``established_min_market_cap_usdc``) is enforced by the caller once the
      token is enriched (see ``runner.runtime.RunnerRuntime.discover_once``),
      because holder count isn't known until then.
    - Enrichment: ``/networks/arc/tokens/{addr}`` gives price, market cap,
      multi-window volume, and buy/sell counts. ``/networks/arc/pools/{pool}``
      gives liquidity for the specific pool discovered earlier.

    Free tier is keyless at 15 req/min and 10k credits/month; a free key raises
    it to 30/min and 100k/month. The client paces itself accordingly.
    """

    name = "dexpaprika"

    def __init__(self, client: DexPaprikaClient, *, max_tokens: int = 20, cache_s: float = 60.0,
                 min_txns_24h: int = 1, min_volume_24h_usd: float = 100.0,
                 lookback_hours: int = 24, network: str = "arc",
                 scan_new_launches: bool = True, scan_established: bool = True,
                 established_min_age_hours: float = 24.0):
        self.client = client
        self.max_tokens = max(1, max_tokens)
        self.cache_s = max(5.0, cache_s)
        self.min_txns_24h = min_txns_24h
        self.min_volume_24h_usd = min_volume_24h_usd
        self.lookback_hours = max(1, lookback_hours)
        self.network = network
        self.scan_new_launches = scan_new_launches
        self.scan_established = scan_established
        self.established_min_age_hours = max(1.0, established_min_age_hours)
        self._cached_tokens: list[str] = []
        self._last_discovery = 0.0
        self._established_cached_tokens: list[str] = []
        self._last_established_discovery = 0.0
        self._pool_by_token: dict[str, str] = {}
        self._state_cache: dict[str, tuple[float, MarketState]] = {}

    def _extract_tokens(self, pools: list[dict], budget: int) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        zero = "0x" + "0" * 40
        for pool in pools:
            for key in ("tokens", "token_pair", "pair"):
                pair = pool.get(key)
                if isinstance(pair, list):
                    candidates = pair
                elif isinstance(pair, dict):
                    candidates = [pair.get("base"), pair.get("quote"), pair.get("token0"), pair.get("token1")]
                else:
                    continue
                for tok in candidates:
                    addr = None
                    if isinstance(tok, dict):
                        addr = tok.get("address") or tok.get("id") or tok.get("contract")
                    elif isinstance(tok, str):
                        addr = tok
                    if not addr:
                        continue
                    a = str(addr).lower().strip()
                    if not a.startswith("0x") or len(a) < 42:
                        continue
                    if a == zero or a in seen:
                        continue
                    seen.add(a)
                    tokens.append(a)
                    pool_addr = pool.get("address") or pool.get("id") or pool.get("pool_address")
                    if pool_addr:
                        self._pool_by_token[a] = str(pool_addr).lower()
                    if len(tokens) >= budget:
                        break
                if len(tokens) >= budget:
                    break
            if len(tokens) >= budget:
                break
        return tokens

    async def _discover_new(self, budget: int) -> list[str]:
        if self._cached_tokens and time.monotonic() - self._last_discovery < self.cache_s:
            return self._cached_tokens[:budget]
        import time as _t
        cutoff = int(_t.time()) - self.lookback_hours * 3600
        payload = await self.client.filter_pools(
            self.network, created_after=cutoff, txns_24h_min=self.min_txns_24h,
            volume_24h_min=self.min_volume_24h_usd, sort_by="created_at", sort_dir="desc",
            limit=min(100, budget * 3),
        )
        pools = payload.get("data") or payload.get("pools") or []
        tokens = self._extract_tokens(pools, budget)
        self._cached_tokens = tokens
        self._last_discovery = time.monotonic()
        return tokens

    async def _discover_established(self, budget: int) -> list[str]:
        if self._established_cached_tokens and time.monotonic() - self._last_established_discovery < self.cache_s:
            return self._established_cached_tokens[:budget]
        import time as _t
        cutoff = int(_t.time()) - int(self.established_min_age_hours * 3600)
        payload = await self.client.filter_pools(
            self.network, created_before=cutoff, txns_24h_min=self.min_txns_24h,
            volume_24h_min=self.min_volume_24h_usd, sort_by="created_at", sort_dir="desc",
            limit=min(100, budget * 3),
        )
        pools = payload.get("data") or payload.get("pools") or []
        tokens = self._extract_tokens(pools, budget)
        self._established_cached_tokens = tokens
        self._last_established_discovery = time.monotonic()
        return tokens

    async def discover_tokens(self) -> list[str]:
        if not self.scan_new_launches and not self.scan_established:
            return []
        # Split the token budget between the two buckets when both are on, so
        # new launches can't crowd established candidates out entirely (or
        # vice versa).
        if self.scan_new_launches and self.scan_established:
            new_budget = max(1, self.max_tokens // 2)
            established_budget = max(1, self.max_tokens - new_budget)
        else:
            new_budget = established_budget = self.max_tokens

        found: list[str] = []
        seen: set[str] = set()
        errors: list[str] = []
        if self.scan_new_launches:
            try:
                for a in await self._discover_new(new_budget):
                    if a not in seen:
                        seen.add(a)
                        found.append(a)
            except DataUnavailable as exc:
                errors.append(f"new-launch discovery: {exc}")
        if self.scan_established:
            try:
                for a in await self._discover_established(established_budget):
                    if a not in seen:
                        seen.add(a)
                        found.append(a)
            except DataUnavailable as exc:
                errors.append(f"established-token discovery: {exc}")
        if not found and errors:
            raise DataUnavailable("; ".join(errors))
        return found[: self.max_tokens]

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        cached = self._state_cache.get(token)
        if cached and time.monotonic() - cached[0] < self.cache_s:
            return cached[1]

        try:
            token_data = await self.client.token_details(self.network, token)
        except DataUnavailable as exc:
            raise DataUnavailable(f"DexPaprika has no data for {token}: {exc}") from exc

        summary = token_data.get("summary") or token_data
        now = datetime.now(timezone.utc)

        price = _num(summary.get("price_usd"))
        market_cap = _num(summary.get("fdv")) or _num(summary.get("market_cap"))
        liquidity = _num(summary.get("liquidity_usd")) or _num(summary.get("liquidity"))

        # Multi-window stats live under "summary" on the token endpoint.
        def vol(window: str) -> float | None:
            return _num(summary.get(f"volume_usd_{window}"))

        def buys(window: str) -> int | None:
            v = _num(summary.get(f"buys_{window}"))
            return int(v) if v is not None else None

        def sells(window: str) -> int | None:
            v = _num(summary.get(f"sells_{window}"))
            return int(v) if v is not None else None

        # Price change is not directly in the token summary on all tiers;
        # derive it from the 24h high/low if present, otherwise leave None.
        price_change_5m = _num(summary.get("price_change_pct_5m"))
        price_change_15m = _num(summary.get("price_change_pct_15m"))
        price_change_1m = _num(summary.get("price_change_pct_1m"))

        # Enrich liquidity from the specific pool if we captured one during discovery.
        pool_address = self._pool_by_token.get(token)
        if pool_address:
            try:
                pool_data = await self.client.pool_details(self.network, pool_address)
                p = pool_data.get("pool") or pool_data
                liquidity = liquidity or _num(p.get("liquidity_usd")) or _num(p.get("liquidity"))
            except DataUnavailable:
                pass

        symbol = summary.get("symbol")
        name = summary.get("name")
        created = _ts(summary.get("created_at") or summary.get("pool_created_at"))

        volume_5m = vol("5m")
        buys_5m = buys("5m")
        sells_5m = sells("5m")
        buy_volume_5m, sell_volume_5m = _estimate_buy_sell_usd(volume_5m, buys_5m, sells_5m)

        state = MarketState(
            chain="arc", token_address=token, timestamp=now,
            pool_address=pool_address, pool_id=pool_address, symbol=symbol or name,
            token_created_at=created, price=price, market_cap=market_cap, liquidity=liquidity,
            price_change_1m=price_change_1m, price_change_5m=price_change_5m, price_change_15m=price_change_15m,
            volume_1m=vol("1m"), volume_5m=volume_5m, volume_15m=vol("15m"),
            buy_volume_5m=buy_volume_5m, sell_volume_5m=sell_volume_5m,
            buy_sell_basis=("estimated_from_counts" if buy_volume_5m is not None else None),
            buys_1m=buys("1m"), sells_1m=sells("1m"),
            buys_5m=buys_5m, sells_5m=sells_5m,
            contract=ContractInfo(verified=None),
            data_sources=["dexpaprika:tokens", "dexpaprika:pools"], is_demo=False,
        )
        self._state_cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        await self.client.aclose()