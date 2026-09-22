from datetime import datetime, timezone

import pytest

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import MarketState
from app.market_data.registry import MarketDataRegistry


class Provider(MarketDataProvider):
    def __init__(self, name, state=None, error=False):
        self.name = name
        self.state = state
        self.error = error
        self.calls = 0

    async def discover_tokens(self):
        self.calls += 1
        if self.error:
            raise DataUnavailable("HTTP 402")
        return [self.state.token_address] if self.state else []

    async def get_market_state(self, token_address):
        self.calls += 1
        if self.error:
            raise DataUnavailable("HTTP 402")
        return self.state


@pytest.mark.asyncio
async def test_registry_merges_partial_rpc_with_market_provider():
    now = datetime.now(timezone.utc)
    rpc = Provider("arc_rpc", MarketState(chain="arc", token_address="0xabc", timestamp=now, symbol="ABC", creator_known=True))
    gecko = Provider("gecko", MarketState(chain="arc", token_address="0xabc", timestamp=now, price=1.2, liquidity=25000, volume_5m=1000))
    reg = MarketDataRegistry([("arc_rpc", rpc), ("gecko", gecko)])
    m = await reg.get_market_state("0xabc")
    assert m.symbol == "ABC"
    assert m.price == 1.2
    assert m.liquidity == 25000
    assert m.data_sources == ["arc_rpc", "gecko"]


@pytest.mark.asyncio
async def test_registry_circuit_breaks_402_provider_and_falls_back():
    good = Provider("arc_rpc", MarketState(chain="arc", token_address="0xabc", timestamp=datetime.now(timezone.utc), price=2, liquidity=30000))
    bad = Provider("bitquery", error=True)
    reg = MarketDataRegistry([("bitquery", bad), ("arc_rpc", good)], failure_threshold=1, cooldown_s=60)
    m = await reg.get_market_state("0xabc")
    assert m.price == 2
    assert reg.status()["bitquery"]["circuit_open"] is True
    assert bad.calls == 1
