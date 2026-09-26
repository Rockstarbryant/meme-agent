from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from eth_abi import decode

from app.chains.base import MarketDataProvider
from app.chains.evm import EvmRpcClient
from app.chains.arc.network import USDC_ERC20_ADDRESS, USDC_ERC20_DECIMALS
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState

# Uniswap v4 core events. These topic0 values are the canonical v4 IPoolManager
# events; Arc uses the same PoolManager implementation at the Arc deployment.
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
USDC = USDC_ERC20_ADDRESS.lower()


def _i(v: str | None) -> int:
    return int(v or "0x0", 16)


def _addr(topic: str | None) -> str | None:
    if not topic:
        return None
    return "0x" + topic[-40:].lower()


def _signed(value: int, bits: int) -> int:
    if value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def _time(ts: int) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


class ArcUniswapV4RpcMarketData(MarketDataProvider):
    """Free Arc-native Uniswap v4 market indexer for USDC pools.

    It reads Initialize + Swap logs directly from Arc RPC. GeckoTerminal can
    enrich liquidity/market-cap fields, while this provider supplies fresh
    on-chain swap flow without a paid indexer.
    """

    def __init__(self, rpc: EvmRpcClient, *, max_tokens: int = 10, scan_blocks: int = 20000, swap_scan_blocks: int = 1800, cache_s: float = 20.0):
        self.rpc = rpc
        self.max_tokens = max(1, max_tokens)
        self.scan_blocks = max(100, scan_blocks)
        self.swap_scan_blocks = max(100, swap_scan_blocks)
        self.cache_s = max(5.0, cache_s)
        self._pools: dict[str, dict] = {}
        self._last_pool_scan = 0.0
        self._state_cache: dict[str, tuple[float, MarketState]] = {}

    async def _scan_pools(self) -> None:
        if time.monotonic() - self._last_pool_scan < self.cache_s and self._pools:
            return
        latest = _i(await self.rpc.call("eth_blockNumber"))
        start = max(0, latest - self.scan_blocks)
        usdc_topic = "0x" + "0" * 24 + USDC.removeprefix("0x")
        rows: list[dict] = []
        for topics in (
            [INITIALIZE_TOPIC, None, usdc_topic],
            [INITIALIZE_TOPIC, usdc_topic, None],
        ):
            try:
                # Route through get_logs() so a range-too-large refusal narrows
                # the window instead of repeating an identical 400 every cycle.
                got = await self.rpc.get_logs(
                    address=POOL_MANAGER,
                    topics=topics,
                    from_block=start,
                    to_block=latest,
                ) or []
                rows.extend(got)
            except Exception as exc:
                raise DataUnavailable(f"Arc Uniswap v4 Initialize logs unavailable: {type(exc).__name__}") from exc
        for row in rows:
            topics = row.get("topics") or []
            if len(topics) < 3:
                continue
            pool_id = str(topics[1]).lower()
            c0, c1 = _addr(topics[2]), _addr(topics[3] if len(topics) > 3 else None)
            if not c0 or not c1 or USDC not in {c0, c1}:
                continue
            token = c1 if c0 == USDC else c0
            block_no = _i(row.get("blockNumber"))
            created_at = None
            try:
                block = await self.rpc.call("eth_getBlockByNumber", [hex(block_no), False])
                created_at = _time(_i((block or {}).get("timestamp")))
            except Exception:
                pass
            self._pools[token] = {"pool_id": pool_id, "currency0": c0, "currency1": c1, "created_at": created_at, "block": block_no}
        self._last_pool_scan = time.monotonic()

    async def discover_tokens(self) -> list[str]:
        await self._scan_pools()
        return list(self._pools.keys())[: self.max_tokens]

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        cached = self._state_cache.get(token)
        if cached and time.monotonic() - cached[0] < self.cache_s:
            return cached[1]
        await self._scan_pools()
        pool = self._pools.get(token)
        if not pool:
            raise DataUnavailable(f"no Arc Uniswap v4 USDC pool found for {token}")
        latest = _i(await self.rpc.call("eth_blockNumber"))
        start = max(pool["block"], latest - self.swap_scan_blocks)
        try:
            rows = await self.rpc.get_logs(
                address=POOL_MANAGER,
                topics=[SWAP_TOPIC, pool["pool_id"]],
                from_block=start,
                to_block=latest,
            ) or []
        except Exception as exc:
            raise DataUnavailable(f"Arc Uniswap v4 Swap logs unavailable: {type(exc).__name__}") from exc
        if not rows:
            raise DataUnavailable(f"no recent swaps for Arc pool {pool['pool_id']}")
        now = datetime.now(timezone.utc)
        prices: list[tuple[datetime, float]] = []
        buys = sells = 0
        buy_usd = sell_usd = 0.0
        buckets = {60: [0.0, 0.0, 0, 0], 300: [0.0, 0.0, 0, 0], 900: [0.0, 0.0, 0, 0]}
        token_decimals = 18
        try:
            raw = await self.rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            token_decimals = _i(raw)
        except Exception:
            pass
        for row in rows[-250:]:
            topics = row.get("topics") or []
            data = row.get("data") or "0x"
            if len(topics) < 2 or len(data) < 2 + 32 * 6:
                continue
            try:
                amount0, amount1, sqrt_price, liquidity, tick, fee = decode(
                    ["int128", "int128", "uint160", "uint128", "int24", "uint24"], bytes.fromhex(data[2:])
                )
            except Exception:
                continue
            block_no = _i(row.get("blockNumber"))
            try:
                block = await self.rpc.call("eth_getBlockByNumber", [hex(block_no), False])
                ts = _time(_i((block or {}).get("timestamp"))) or now
            except Exception:
                ts = now
            age = (now - ts).total_seconds()
            if age < 0 or age > 900:
                continue
            token_delta = int(amount1 if pool["currency0"] == USDC else amount0)
            quote_delta = int(amount0 if pool["currency0"] == USDC else amount1)
            quote_usd = abs(quote_delta) / 10 ** USDC_ERC20_DECIMALS
            # PoolManager emits the pool balance delta. Positive token delta means
            # the pool received tokens (the trader sold); negative means the
            # trader received tokens (the trader bought).
            if token_delta < 0:
                buys += 1; buy_usd += quote_usd
            elif token_delta > 0:
                sells += 1; sell_usd += quote_usd
            for window, bucket in buckets.items():
                if age <= window:
                    bucket[2] += 1 if token_delta < 0 else 0
                    bucket[3] += 1 if token_delta > 0 else 0
                    if token_delta < 0: bucket[0] += quote_usd
                    elif token_delta > 0: bucket[1] += quote_usd
            if sqrt_price:
                raw_ratio = (int(sqrt_price) ** 2) / (2 ** 192)
                if pool["currency0"] == USDC:
                    px = 1.0 / (raw_ratio * (10 ** USDC_ERC20_DECIMALS) / (10 ** token_decimals)) if raw_ratio > 0 else None
                else:
                    px = raw_ratio * (10 ** token_decimals) / (10 ** USDC_ERC20_DECIMALS)
                if px and px > 0:
                    prices.append((ts, px))
        if not prices:
            raise DataUnavailable(f"unable to decode a current price for {token}")
        prices.sort(key=lambda x: x[0])
        current = prices[-1][1]
        recent_high = max(p for _, p in prices)
        def change(seconds: int) -> float | None:
            cutoff = now.timestamp() - seconds
            old = next((p for t, p in prices if t.timestamp() >= cutoff), None)
            return ((current / old) - 1) * 100 if old and old > 0 else None
        b5 = buckets[300]
        state = MarketState(
            chain="arc", token_address=token, timestamp=now, pool_address=POOL_MANAGER,
            pool_id=pool["pool_id"], token_created_at=pool.get("created_at"), price=current,
            buys_1m=buckets[60][2] + 0 or None, sells_1m=buckets[60][3] or None,
            buys_5m=b5[2] or None, sells_5m=b5[3] or None,
            volume_1m=(buckets[60][0] + buckets[60][1]) or None,
            volume_5m=(b5[0] + b5[1]) or None,
            volume_15m=(buckets[900][0] + buckets[900][1]) or None,
            buy_volume_5m=b5[0] or None, sell_volume_5m=b5[1] or None,
            price_change_1m=change(60), price_change_5m=change(300), price_change_15m=change(900), recent_high=recent_high,
            contract=ContractInfo(verified=None), data_sources=["arc_rpc:uniswap_v4_initialize", "arc_rpc:uniswap_v4_swap"], is_demo=False,
        )
        self._state_cache[token] = (time.monotonic(), state)
        return state

    async def aclose(self) -> None:
        return None