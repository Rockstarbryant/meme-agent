from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, Field

from app.core.types import RiskCategory as C, RiskDecision, Severity as S, Side, TradingMode
from app.domain.market import MarketState
from app.execution.math import estimate_price_impact_pct
from app.portfolio.controls import ControlState
from app.portfolio.state import PortfolioState

log = logging.getLogger(__name__)


class RiskLimits(BaseModel):
    max_trade_usdc: float = 25.0
    max_position_usdc: float = 50.0
    max_daily_loss_usdc: float = 50.0
    max_total_exposure_usdc: float = 250.0
    max_open_positions: int = 5
    max_chain_exposure_usdc: float = 250.0
    max_launchpad_exposure_usdc: float = 100.0
    max_slippage_pct: float = 2.0
    min_liquidity_usdc: float = 10_000.0
    max_trade_liquidity_ratio: float = 0.01
    max_price_impact_pct: float = 2.0
    cooldown_seconds: int = 300
    max_data_age_seconds: float = 30.0
    min_token_age_seconds: float = 180.0
    max_price_change_5m_chase_pct: float = 150.0
    max_liquidity_drop_5m_pct: float = 25.0
    max_top10_holder_pct: float = 60.0
    min_holder_count: int = 20
    max_creator_sold_pct: float = 20.0
    max_creator_balance_pct: float = 30.0
    max_tax_pct: float = 10.0
    max_mev_score: float = 0.7
    require_contract_verified: bool = False
    veto_active_mint_authority: bool = True
    veto_pausable: bool = True
    veto_blacklist_capability: bool = True
    allowed_chains: set[str] = Field(default_factory=lambda: {"arc"})
    allowed_launchpads: set[str] = Field(default_factory=set)  # launchpad-sourced tokens need explicit allow
    # On Arc, native gas and ERC-20 USDC are ONE asset: never a trade target (Circle use-arc / swap-tokens skills)
    non_tradable_addresses: set[str] = Field(default_factory=lambda: {
        "0x3600000000000000000000000000000000000000", "0x0000000000000000000000000000000000000000",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"})


class RiskFlag(BaseModel):
    rule: str
    category: C
    severity: S
    message: str
    value: float | str | None = None
    threshold: float | str | None = None


class RiskAssessment(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    decision: RiskDecision
    flags: list[RiskFlag]
    risk_score: float
    side: Side
    token_address: str
    amount_usdc: float
    mode: TradingMode
    limits: dict
    created_at: datetime

    @property
    def vetoes(self) -> list[RiskFlag]:
        return [f for f in self.flags if f.severity == S.VETO]

    def summary(self) -> str:
        return ", ".join(f.rule for f in self.vetoes) or "no vetoes"


@dataclass(frozen=True)
class TradeIntent:
    side: Side
    amount_usdc: float
    max_slippage_pct: float
    mode: TradingMode
    strategy_id: str


@dataclass(frozen=True)
class RiskContext:
    market: MarketState
    intent: TradeIntent
    portfolio: PortfolioState
    limits: RiskLimits
    controls: ControlState
    now: datetime


@dataclass(frozen=True)
class RiskRule:
    name: str
    category: C
    fn: Callable[[RiskContext], list[RiskFlag]]


def _veto(rule: str, cat: C, msg: str, value=None, threshold=None) -> RiskFlag:
    return RiskFlag(rule=rule, category=cat, severity=S.VETO, message=msg, value=value, threshold=threshold)


def _warn(rule: str, cat: C, msg: str, value=None, threshold=None) -> RiskFlag:
    return RiskFlag(rule=rule, category=cat, severity=S.WARN, message=msg, value=value, threshold=threshold)


# ------------------------------------------------------------------ rules
def contract_rules(x: RiskContext) -> list[RiskFlag]:
    c, L, out = x.market.contract, x.limits, []
    K = C.CONTRACT_RISK
    live = x.intent.mode == TradingMode.LIVE
    if c.verified is False and L.require_contract_verified:
        out.append(_veto("CONTRACT_UNVERIFIED", K, "contract source not verified"))
    elif c.verified is None:
        out.append(_warn("CONTRACT_VERIFICATION_UNKNOWN", K, "contract verification status unknown"))
    if c.mint_authority_active and L.veto_active_mint_authority:
        out.append(_veto("MINT_AUTHORITY_ACTIVE", K, "mint authority still active"))
    if c.blacklist_capability and L.veto_blacklist_capability:
        out.append(_veto("BLACKLIST_CAPABILITY", K, "token can blacklist holders"))
    if c.transfer_restricted:
        out.append(_veto("TRANSFER_RESTRICTED", K, "transfers are restricted"))
    if c.pausable and L.veto_pausable:
        out.append(_veto("PAUSABLE", K, "token transfers can be paused"))
    for name, tax in (("BUY_TAX", c.buy_tax_pct), ("SELL_TAX", c.sell_tax_pct)):
        if tax is not None and tax > L.max_tax_pct:
            out.append(_veto(f"{name}_TOO_HIGH", K, f"{name.lower()} above limit", tax, L.max_tax_pct))
    if c.sell_simulation_ok is False:
        out.append(_veto("SELL_SIMULATION_FAILED", K, "sell simulation failed (possible honeypot)"))
    elif c.sell_simulation_ok is None:
        f = _veto if live else _warn
        out.append(f("SELLABILITY_UNVERIFIED", K, "could not verify the token can be sold"))
    if c.is_proxy:
        out.append(_warn("UPGRADEABLE_PROXY", K, "contract is an upgradeable proxy"))
    return out


def liquidity_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, out, K = x.market, x.limits, [], C.LIQUIDITY_RISK
    if m.liquidity is None:
        return [_veto("LIQUIDITY_UNKNOWN", K, "liquidity unavailable")]
    if m.liquidity < L.min_liquidity_usdc:
        out.append(_veto("LIQUIDITY_BELOW_MIN", K, "liquidity below minimum", m.liquidity, L.min_liquidity_usdc))
    ratio = x.intent.amount_usdc / m.liquidity if m.liquidity > 0 else 1.0
    if ratio > L.max_trade_liquidity_ratio:
        out.append(_veto("TRADE_LIQUIDITY_RATIO", K, "trade too large for pool", ratio, L.max_trade_liquidity_ratio))
    chg = m.liquidity_change_5m_pct
    if chg is not None and chg <= -L.max_liquidity_drop_5m_pct:
        out.append(_veto("LIQUIDITY_DRAINING", K, "liquidity falling quickly", chg, -L.max_liquidity_drop_5m_pct))
    return out


def holder_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, out, K = x.market, x.limits, [], C.HOLDER_RISK
    if m.top10_holder_pct is None:
        out.append(_warn("HOLDER_DATA_MISSING", K, "holder concentration unavailable"))
    elif m.top10_holder_pct > L.max_top10_holder_pct:
        out.append(_veto("TOP10_CONCENTRATION", K, "top-10 holders too concentrated",
                         m.top10_holder_pct, L.max_top10_holder_pct))
    if m.holder_count is not None and m.holder_count < L.min_holder_count:
        out.append(_warn("FEW_HOLDERS", K, "very few holders", m.holder_count, L.min_holder_count))
    return out


def creator_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, out, K = x.market, x.limits, [], C.CREATOR_RISK
    if m.creator_address and m.creator_address.lower() in x.controls.blacklisted_creators:
        out.append(_veto("CREATOR_BLACKLISTED", K, "creator is blacklisted"))
    if not m.creator_known:
        out.append(_warn("CREATOR_UNVERIFIED", K, "creator behaviour could not be verified"))
        return out
    if m.creator_sold_pct is not None and m.creator_sold_pct > L.max_creator_sold_pct:
        out.append(_veto("CREATOR_DUMPING", K, "creator has sold a large share", m.creator_sold_pct, L.max_creator_sold_pct))
    if m.creator_balance_pct is not None and m.creator_balance_pct > L.max_creator_balance_pct:
        out.append(_warn("CREATOR_HOLDS_MUCH", K, "creator holds a large share", m.creator_balance_pct, L.max_creator_balance_pct))
    return out


def market_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, out, K = x.market, x.limits, [], C.MARKET_RISK
    if m.price is None or m.price <= 0:
        return [_veto("PRICE_UNAVAILABLE", K, "no valid price")]
    age = m.data_age_seconds(x.now)
    if age > L.max_data_age_seconds:
        out.append(_veto("STALE_DATA", K, "market data is stale", age, L.max_data_age_seconds))
    if m.token_address.lower() in x.controls.blacklisted_tokens:
        out.append(_veto("TOKEN_BLACKLISTED", K, "token is blacklisted"))
    if m.token_address.lower() in {a.lower() for a in L.non_tradable_addresses}:
        out.append(_veto("NON_TRADABLE_ASSET", K, "USDC/native gas asset is the quote asset, not a trade target"))
    tok_age = m.age_seconds(x.now)
    if tok_age is not None and tok_age < L.min_token_age_seconds:
        out.append(_veto("TOKEN_TOO_NEW", K, "token younger than minimum age", tok_age, L.min_token_age_seconds))
    pc5 = m.price_change_5m
    if pc5 is not None and pc5 > L.max_price_change_5m_chase_pct:
        out.append(_veto("OVERHEATED_CHASE", K, "5m price change too extreme to chase", pc5, L.max_price_change_5m_chase_pct))
    if m.is_demo and x.intent.mode == TradingMode.LIVE:
        out.append(_veto("DEMO_DATA_IN_LIVE", K, "demo data cannot drive live trades"))
    return out


def execution_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, out, K = x.market, x.limits, [], C.EXECUTION_RISK
    if x.intent.max_slippage_pct > L.max_slippage_pct:
        out.append(_veto("SLIPPAGE_LIMIT_EXCEEDED", K, "requested slippage above limit",
                         x.intent.max_slippage_pct, L.max_slippage_pct))
    impact = m.expected_price_impact_pct
    if impact is None and m.liquidity:
        impact = estimate_price_impact_pct(x.intent.amount_usdc, m.liquidity)
    if impact is None:
        out.append(_veto("IMPACT_UNKNOWN", K, "price impact cannot be estimated"))
    elif impact > L.max_price_impact_pct:
        out.append(_veto("PRICE_IMPACT_TOO_HIGH", K, "expected price impact too high", impact, L.max_price_impact_pct))
    return out


def mev_rules(x: RiskContext) -> list[RiskFlag]:
    s, L = x.market.mev_risk_score, x.limits
    if s is None:
        return [_warn("MEV_UNASSESSED", C.MEV_RISK, "MEV exposure not assessed")]
    if s > L.max_mev_score:
        return [_veto("MEV_RISK_HIGH", C.MEV_RISK, "MEV risk above limit", s, L.max_mev_score)]
    return []


def portfolio_rules(x: RiskContext) -> list[RiskFlag]:
    m, L, P, ctl, i, K = x.market, x.limits, x.portfolio, x.controls, x.intent, C.PORTFOLIO_RISK
    out: list[RiskFlag] = []
    if ctl.emergency_stop:
        out.append(_veto("EMERGENCY_STOP", K, "emergency stop is active"))
    if ctl.global_pause:
        out.append(_veto("GLOBAL_PAUSE", K, "agent is paused"))
    if i.strategy_id in ctl.paused_strategies:
        out.append(_veto("STRATEGY_PAUSED", K, "strategy is paused"))
    if m.chain in ctl.paused_chains:
        out.append(_veto("CHAIN_PAUSED", K, "chain is paused"))
    if m.chain not in L.allowed_chains:
        out.append(_veto("CHAIN_NOT_ALLOWED", K, "chain is not allowlisted", m.chain))
    if m.launchpad:
        if m.launchpad in ctl.blacklisted_launchpads:
            out.append(_veto("LAUNCHPAD_BLACKLISTED", K, "launchpad is blacklisted", m.launchpad))
        if m.launchpad not in L.allowed_launchpads:
            out.append(_veto("LAUNCHPAD_NOT_ALLOWED", K, "launchpad is not allowlisted", m.launchpad))
    amt = i.amount_usdc
    if amt <= 0:
        out.append(_veto("NON_POSITIVE_AMOUNT", K, "trade amount must be positive", amt))
    if amt > L.max_trade_usdc + 1e-9:
        out.append(_veto("MAX_TRADE_EXCEEDED", K, "trade above maximum", amt, L.max_trade_usdc))
    if amt > P.cash_usdc + 1e-9:
        out.append(_veto("INSUFFICIENT_FUNDS", K, "not enough available USDC", P.cash_usdc, amt))
    dpnl = P.daily_pnl(x.now)
    if dpnl <= -L.max_daily_loss_usdc:
        out.append(_veto("DAILY_LOSS_LIMIT", K, "daily loss limit reached", dpnl, -L.max_daily_loss_usdc))
    if P.has_open_position(m.token_address):
        out.append(_veto("DUPLICATE_POSITION", K, "position in this token already open"))
    elif P.open_count() >= L.max_open_positions:
        out.append(_veto("MAX_OPEN_POSITIONS", K, "max open positions reached", P.open_count(), L.max_open_positions))
    if m.key in P.pending_tokens:
        out.append(_veto("DUPLICATE_ORDER", K, "an order for this token is already pending"))
    if P.token_exposure(m.token_address) + amt > L.max_position_usdc + 1e-9:
        out.append(_veto("MAX_POSITION_EXCEEDED", K, "position would exceed maximum", None, L.max_position_usdc))
    if P.total_exposure() + amt > L.max_total_exposure_usdc + 1e-9:
        out.append(_veto("MAX_TOTAL_EXPOSURE", K, "total exposure would exceed maximum", None, L.max_total_exposure_usdc))
    if P.chain_exposure(m.chain) + amt > L.max_chain_exposure_usdc + 1e-9:
        out.append(_veto("MAX_CHAIN_EXPOSURE", K, "chain exposure would exceed maximum", None, L.max_chain_exposure_usdc))
    if m.launchpad and P.launchpad_exposure(m.launchpad) + amt > L.max_launchpad_exposure_usdc + 1e-9:
        out.append(_veto("MAX_LAUNCHPAD_EXPOSURE", K, "launchpad exposure would exceed maximum", None, L.max_launchpad_exposure_usdc))
    last = P.last_trade_at.get(m.key)
    if last is not None and (x.now - last).total_seconds() < L.cooldown_seconds:
        out.append(_veto("COOLDOWN_ACTIVE", K, "token is in cooldown"))
    return out


DEFAULT_RULES: list[RiskRule] = [
    RiskRule("contract", C.CONTRACT_RISK, contract_rules),
    RiskRule("liquidity", C.LIQUIDITY_RISK, liquidity_rules),
    RiskRule("holders", C.HOLDER_RISK, holder_rules),
    RiskRule("creator", C.CREATOR_RISK, creator_rules),
    RiskRule("market", C.MARKET_RISK, market_rules),
    RiskRule("execution", C.EXECUTION_RISK, execution_rules),
    RiskRule("mev", C.MEV_RISK, mev_rules),
    RiskRule("portfolio", C.PORTFOLIO_RISK, portfolio_rules),
]


class RiskEngine:
    """Hard-veto engine. Fails CLOSED: a crashing rule is a veto. AI output is never an input."""

    def __init__(self, rules: list[RiskRule] | None = None):
        self.rules = rules or DEFAULT_RULES

    def assess(self, ctx: RiskContext) -> RiskAssessment:
        flags: list[RiskFlag] = []
        if ctx.intent.side == Side.SELL:
            # Exits are never blocked by entry rules: position protection must always work.
            flags.append(RiskFlag(rule="EXIT_ALLOWED", category=C.PORTFOLIO_RISK, severity=S.INFO,
                                  message="exits are not subject to entry vetoes"))
        else:
            for rule in self.rules:
                try:
                    flags.extend(rule.fn(ctx))
                except Exception as exc:  # noqa: BLE001 - fail closed by design
                    log.exception("risk rule %s crashed", rule.name)
                    flags.append(_veto(f"RULE_ERROR:{rule.name}", rule.category, f"rule crashed: {type(exc).__name__}"))
        score = min(100.0, sum(40.0 if f.severity == S.VETO else 10.0 if f.severity == S.WARN else 0.0 for f in flags))
        decision = RiskDecision.REJECT if any(f.severity == S.VETO for f in flags) else RiskDecision.APPROVE
        return RiskAssessment(decision=decision, flags=flags, risk_score=score, side=ctx.intent.side,
                              token_address=ctx.market.token_address, amount_usdc=ctx.intent.amount_usdc,
                              mode=ctx.intent.mode, limits=ctx.limits.model_dump(mode="json"),
                              created_at=datetime.now(timezone.utc))


def max_entry_amount(market: MarketState, portfolio: PortfolioState, limits: RiskLimits) -> float:
    """Largest BUY the limits allow. Sizing only ever reduces; rules still veto oversize orders."""
    caps = [limits.max_trade_usdc, portfolio.cash_usdc,
            limits.max_position_usdc - portfolio.token_exposure(market.token_address),
            limits.max_total_exposure_usdc - portfolio.total_exposure(),
            limits.max_chain_exposure_usdc - portfolio.chain_exposure(market.chain)]
    if market.launchpad:
        caps.append(limits.max_launchpad_exposure_usdc - portfolio.launchpad_exposure(market.launchpad))
    if market.liquidity:
        caps.append(market.liquidity * limits.max_trade_liquidity_ratio)
    return max(0.0, round(min(caps), 6))
