from datetime import datetime, timezone

import pytest

from app.chains.arc.gecko_market_data import GeckoTerminalArcMarketData


class FakeClient:
    async def token_pools(self, token):
        return {"data": [{
            "id": "arc_0xpool",
            "attributes": {
                "base_token_address": token,
                "base_token_price_usd": "0.12",
                "reserve_in_usd": "25000",
                "fdv_usd": "1000000",
                "pool_created_at": "2026-09-22T18:00:00Z",
                "price_change_percentage": {"m5": "12.5", "m15": "20"},
                "volume_usd": {"m1": "100", "m5": "900", "m15": "2500"},
                "transactions": {"m1": {"buys": 3, "sells": 1, "buyers": 3, "sellers": 1},
                                  "m5": {"buys": 20, "sells": 7, "buyers": 14, "sellers": 6}},
                "name": "ABC / USDC",
            },
        }]}

    async def pool_trades(self, pool):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {"data": [
            {"attributes": {"block_timestamp": now, "kind": "buy", "volume_usd": "500", "tx_from_address": "0x1", "price_from_in_usd": "0.12"}},
            {"attributes": {"block_timestamp": now, "kind": "sell", "volume_usd": "100", "tx_from_address": "0x2", "price_from_in_usd": "0.11"}},
        ]}


@pytest.mark.asyncio
async def test_gecko_provider_normalizes_pool_and_trade_data():
    p = GeckoTerminalArcMarketData(FakeClient(), cache_s=60)
    m = await p.get_market_state("0xabc")
    assert m.price == 0.12
    assert m.liquidity == 25000
    assert m.unique_buyers_5m == 14
    assert m.buy_volume_5m == 500
    assert m.sell_volume_5m == 100
    assert "geckoterminal:pools" in m.data_sources
