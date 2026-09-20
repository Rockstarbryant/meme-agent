"""DEMO DATA generator. Synthetic, seeded, PAPER-only. It never touches a chain and every state is flagged is_demo.

It exists only so the agent, risk engine and UI can be demonstrated while no verified Arc data source exists.
The risk engine vetoes any is_demo state in LIVE mode (DEMO_DATA_IN_LIVE).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable

from app.chains.base import MarketDataProvider
from app.core.clock import utcnow
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState

SCENARIOS = ["strong", "crash", "chop", "rug", "thin", "honeypot"]


class _Tok:
    def __init__(self, i: int, scenario: str, created: datetime):
        self.scenario, self.step, self.created = scenario, 0, created
        self.address = f"0xde{ i:038x}"  # visibly synthetic
        self.symbol = f"DEMO-{scenario.upper()}"
        self.hist = [1.0] * 20
        self.liq = 120_000.0


class DemoMarketData(MarketDataProvider):
    def __init__(self, seed: int = 7, clock: Callable[[], datetime] = utcnow):
        self.rng, self.clock = random.Random(seed), clock
        t0 = clock() - timedelta(minutes=40)
        self.toks = {t.address: t for t in (_Tok(i + 1, s, t0) for i, s in enumerate(SCENARIOS))}

    def _advance(self, t: _Tok) -> None:
        t.step += 1
        s, n = t.scenario, t.step
        px = t.hist[-1]
        noise = self.rng.uniform(-0.004, 0.004)
        if s == "strong":
            g = 0.035 if n <= 26 else -0.04
        elif s == "crash":
            g = 0.035 if n <= 8 else -0.08
        elif s == "chop":
            g = 0.0
        else:
            g = 0.025 if n <= 15 else 0.004
        t.hist.append(max(px * (1 + g + noise), 1e-6))
        t.hist = t.hist[-40:]

    async def discover_tokens(self) -> list[str]:
        for t in self.toks.values():
            self._advance(t)
        return list(self.toks)

    async def get_market_state(self, token_address: str) -> MarketState:
        t = self.toks.get(token_address)
        if t is None:
            raise DataUnavailable("unknown demo token")
        h, px, n = t.hist, t.hist[-1], t.step
        ch = lambda k: (px / h[-1 - k] - 1) * 100 if len(h) > k else None  # noqa: E731
        s = t.scenario
        rising = ch(3) is not None and ch(3) > 0
        good = (s in ("strong", "rug", "thin", "honeypot") and rising) or (s == "crash" and n <= 8)
        buyers = min(90, 35 + 4 * n) if good else 6
        ratio = 3.0 if good else 0.7 if s == "crash" else 1.0  # crash = panic selling; strong just bleeds out
        vol5 = 24_000.0 if good else 5_000.0
        bv, sv = vol5 * ratio / (1 + ratio), vol5 / (1 + ratio)
        liq = 4_000.0 if s == "thin" else t.liq * (px ** 0.5)
        contract = ContractInfo(verified=True, mint_authority_active=False, pausable=False, blacklist_capability=False,
                                transfer_restricted=False, buy_tax_pct=0.0, sell_tax_pct=0.0,
                                sell_simulation_ok=(False if s == "honeypot" else True))
        return MarketState(
            chain="arc", token_address=t.address, symbol=t.symbol, timestamp=self.clock(), token_created_at=t.created,
            price=px, market_cap=px * 600_000, liquidity=liq, liquidity_change_5m_pct=8.0 if good else -1.0,
            volume_1m=vol5 / 5, volume_5m=vol5, volume_15m=vol5 * 2.4, buy_volume_5m=bv, sell_volume_5m=sv,
            buys_1m=25 if good else 6, sells_1m=8 if good else 6, buys_5m=110 if good else 25, sells_5m=40 if good else 30,
            unique_buyers_1m=int(buyers / 3), unique_buyers_5m=buyers, unique_buyers_prev_5m=max(4, int(buyers * 0.55)),
            unique_sellers_1m=4, unique_sellers_5m=15, holder_count=260 if good else 40, holder_growth_pct=14 if good else 1,
            top5_holder_pct=(70 if s == "rug" else 20), top10_holder_pct=(88 if s == "rug" else 30),
            top20_holder_pct=(95 if s == "rug" else 45), creator_known=True, creator_balance_pct=3.0, creator_sold_pct=2.0,
            price_change_1m=ch(1), price_change_5m=ch(5), price_change_15m=ch(15), recent_high=max(h), volatility_pct=10.0,
            contract=contract, expected_price_impact_pct=0.05, mev_risk_score=0.2,
            data_sources=["DEMO DATA (synthetic)"], is_demo=True)
