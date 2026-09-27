from __future__ import annotations
import asyncio
import pytest
from app.market_data.gateway import SharedMarketDataGateway

@pytest.mark.asyncio
async def test_request_deduplication():
    gw = SharedMarketDataGateway(default_ttl_s=5.0)
    calls = {"n": 0}
    async def fetch():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"price": 1.23}
    results = await asyncio.gather(
        gw.get("arc", "0xabc", fetch),
        gw.get("arc", "0xabc", fetch),
        gw.get("arc", "0xabc", fetch),
    )
    assert calls["n"] == 1
    assert all(r["price"] == 1.23 for r in results)
    assert gw.metrics["provider_calls"] == 1
    assert gw.metrics["deduped"] >= 2

@pytest.mark.asyncio
async def test_cache_hit_multi_tenant_reuse():
    gw = SharedMarketDataGateway(default_ttl_s=10.0)
    calls = {"n": 0}
    async def fetch():
        calls["n"] += 1
        return {"price": 9.9, "liquidity": 10000}
    # Simulate 20 tenants asking for the same token
    results = await asyncio.gather(*[gw.get("arc", "0x1", fetch) for _ in range(20)])
    assert calls["n"] == 1
    assert all(r["price"] == 9.9 for r in results)
    # Second wave should hit cache
    await asyncio.gather(*[gw.get("arc", "0x1", fetch) for _ in range(5)])
    assert calls["n"] == 1
    assert gw.metrics["hits"] >= 5
