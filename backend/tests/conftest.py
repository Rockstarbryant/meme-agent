import json
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.analyzer import AIAnalyzer
from app.ai.provider import AIProviderError, LLMProvider
from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.core.types import AIMode, TradingMode
from app.domain.market import ContractInfo, MarketState
from app.events.bus import EventBus, InMemoryAuditSink, InMemoryIdempotencyStore
from app.execution.paper import PaperExecutionEngine
from app.portfolio.controls import ControlState
from app.portfolio.exits import ExitConfig, PositionManager
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover
from app.risk.engine import RiskEngine, RiskLimits
from app.services.decision import DecisionPipeline
from app.services.engine import TradingEngine
from app.strategies.traction_momentum import TractionMomentum

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
TOKEN = "0xAbCdEf0000000000000000000000000000000001"
SECRET = "test-approval-secret-0123456789"


def good_market(**over) -> MarketState:
    base = dict(
        chain="arc", token_address=TOKEN, timestamp=NOW, token_created_at=NOW - timedelta(hours=1), price=1.0,
        market_cap=500_000, liquidity=100_000, liquidity_change_5m_pct=5, volume_1m=5_000, volume_5m=20_000,
        volume_15m=40_000, buy_volume_5m=14_000, sell_volume_5m=6_000, buys_1m=30, sells_1m=10, buys_5m=120,
        sells_5m=40, unique_buyers_1m=15, unique_buyers_5m=50, unique_buyers_prev_5m=25, unique_sellers_1m=4,
        unique_sellers_5m=15, holder_count=250, holder_growth_pct=15, top5_holder_pct=20, top10_holder_pct=30,
        top20_holder_pct=45, creator_known=True, creator_balance_pct=3, creator_sold_pct=2, price_change_1m=3,
        price_change_5m=20, price_change_15m=35, recent_high=1.02, volatility_pct=10,
        contract=ContractInfo(verified=True, mint_authority_active=False, pausable=False, blacklist_capability=False,
                              transfer_restricted=False, buy_tax_pct=0, sell_tax_pct=0, sell_simulation_ok=True),
        expected_price_impact_pct=0.05, mev_risk_score=0.2, data_sources=["test-fixture"])
    base.update(over)
    return MarketState(**base)


class StaticMarketData(MarketDataProvider):
    """TEST DOUBLE ONLY: serves whatever the test puts in."""

    def __init__(self, *markets: MarketState):
        self.m = {x.token_address.lower(): x for x in markets}

    def set(self, m: MarketState):
        self.m[m.token_address.lower()] = m

    async def get_market_state(self, token_address):
        try:
            return self.m[token_address.lower()]
        except KeyError:
            raise DataUnavailable("no data")

    async def discover_tokens(self):
        return list(self.m)


BUY_JSON = json.dumps({"action": "BUY", "confidence": 0.85, "reasoning_summary": "traction", "positive_signals": ["buyers"],
                       "negative_signals": [], "risk_flags": [], "strategy_score": 80, "recommended_position_percent": 5})


def ai_json(**over) -> str:
    d = json.loads(BUY_JSON)
    d.update(over)
    return json.dumps(d)


class StubProvider(LLMProvider):
    name, model = "stub", "stub-model"

    def __init__(self, response: str | Exception = BUY_JSON, hook=None):
        self.response, self.calls, self.hook = response, 0, hook

    async def complete(self, system, user):
        self.calls += 1
        if self.hook:
            self.hook()
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)
        return self.t


class Rig:
    pass


def make_rig(provider=None, *, mode=TradingMode.PAPER, ai_mode=AIMode.ENABLED, limits=None, cash=1000.0, market=None,
             exit_cfg=None):
    r = Rig()
    r.clock, r.market = Clock(), market or good_market()
    r.data = StaticMarketData(r.market)
    r.portfolio, r.controls = PortfolioState(cash, mode), ControlState()
    r.approver = TradeApprover(SECRET)
    r.provider = provider or StubProvider()
    r.limits = limits or RiskLimits()
    r.strategy = TractionMomentum()
    r.pipeline = DecisionPipeline(r.strategy, RiskEngine(), r.limits, r.approver, AIAnalyzer(r.provider), ai_mode)
    r.executor = PaperExecutionEngine(r.portfolio, r.data, r.approver, clock=r.clock)
    r.sink = InMemoryAuditSink()
    r.bus, r.idem = EventBus([r.sink]), InMemoryIdempotencyStore()
    r.exits = PositionManager(exit_cfg or ExitConfig())
    r.engine = TradingEngine(mode=mode, portfolio=r.portfolio, controls=r.controls, pipeline=r.pipeline,
                             executor=r.executor, approver=r.approver, exit_manager=r.exits, market_data=r.data,
                             bus=r.bus, idempotency=r.idem)
    return r


@pytest.fixture
def rig():
    return make_rig()

from tests.api_fixtures import env, migrated_db  # noqa: E402,F401
