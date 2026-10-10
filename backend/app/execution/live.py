"""LIVE execution on a chain via the wallet abstraction. Every step fails closed."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.chains.base import ChainAdapter
from app.core.errors import (ApprovalError, IntegrationNotVerified, LiveSafetyError, PolicyViolationError)
from app.core.types import OrderStatus, Side, TradingMode
from app.domain.trade import ApprovedTrade, ExecutionResult, Quote, TradeRequest
from app.events.bus import IdempotencyStore
from app.execution.base import ExecutionAdapter
from app.portfolio.state import PortfolioState
from app.observability.audit import AuditKind, AuditStatus, record_event
from app.risk.approval import TradeApprover
from app.venues.cost import cost_breakdown
from app.wallets.base import PolicyValidator, WalletProvider


class LiveSettings(BaseModel):
    live_trading_enabled: bool = False
    confirm_timeout_s: float = 20.0  # Arc: sub-second finality; no receipt after this = TIMEOUT (reconcile, never assume success)
    max_quote_age_s: float = 15.0
    max_quote_deviation_pct: float = 3.0
    # True: the pool fee of the route is part of the EXPECTED price, so only the excess over it counts as "deviation from
    # market". False restores the old behaviour (fees counted as deviation).
    fee_aware_deviation: bool = True
    tx_deadline_s: int = 120


class ArcExecutionEngine(ExecutionAdapter):
    mode = TradingMode.LIVE

    def __init__(self, chain: ChainAdapter, wallet: WalletProvider, approver: TradeApprover,
                 portfolio: PortfolioState, idempotency: IdempotencyStore, settings: LiveSettings):
        self.chain, self.wallet, self.approver = chain, wallet, approver
        self.portfolio, self.idem, self.settings = portfolio, idempotency, settings
        self.audit_user_id = ""

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _res(self, req: TradeRequest, status: OrderStatus, **kw) -> ExecutionResult:
        return ExecutionResult(order_id=f"live-{uuid.uuid4().hex[:12]}", idempotency_key=req.idempotency_key,
                               mode=TradingMode.LIVE, status=status, simulated=False, side=req.side,
                               token_address=req.token_address, label="LIVE", at=self._now(), **kw)

    async def _preflight(self, approved: ApprovedTrade) -> tuple[Quote, list[str]]:
        req, failed = approved.request, []
        if not self.settings.live_trading_enabled:
            raise LiveSafetyError(["LIVE_TRADING_DISABLED"])
        if not self.approver.verify(approved):
            raise ApprovalError("invalid or forged trade approval")
        if req.mode != TradingMode.LIVE:
            raise LiveSafetyError(["REQUEST_NOT_LIVE_MODE"])
        if not self.chain.live_trading_verified:
            raise LiveSafetyError(["CHAIN_LIVE_INTEGRATION_NOT_VERIFIED"])
        if not (await self.chain.health()).ok:
            raise LiveSafetyError(["CHAIN_UNHEALTHY"])
        auth = await self.wallet.get_authorization()
        if auth is None or not auth.is_active(self._now()):
            raise LiveSafetyError(["WALLET_AUTHORIZATION_MISSING_OR_INACTIVE"])
        amount = req.amount_usdc or 0.0
        if req.side == Side.BUY:
            if await self.wallet.get_usdc_balance() < amount:
                failed.append("INSUFFICIENT_WALLET_BALANCE")
            if amount > auth.policy.max_trade_usdc:
                failed.append("EXCEEDS_POLICY_MAX_TRADE")
            if self.portfolio.token_exposure(req.token_address) + amount > auth.policy.max_position_usdc:
                failed.append("EXCEEDS_POLICY_MAX_POSITION")
        if req.max_slippage_pct > auth.policy.max_slippage_pct:
            failed.append("EXCEEDS_POLICY_SLIPPAGE")
        quote = await self.chain.quote(req)  # IntegrationNotVerified propagates -> REJECTED
        now = self._now()
        if quote.expires_at <= now or (now - quote.quoted_at).total_seconds() > self.settings.max_quote_age_s:
            failed.append("QUOTE_STALE")
        diag = quote.diagnostics or {}
        impact_cap = req.max_price_impact_pct if req.max_price_impact_pct is not None else req.max_slippage_pct
        if quote.price_impact_pct > impact_cap:
            code = "PRICE_IMPACT_EXCEEDS_LIMIT" if req.max_price_impact_pct is not None else "PRICE_IMPACT_EXCEEDS_SLIPPAGE"
            failed.append(f"{code}(impact={quote.price_impact_pct:.4f}% > max={impact_cap:.2f}%"
                          f", api_raw={diag.get('price_impact_raw')}, unit={diag.get('impact_unit')}, venue={quote.venue or 'n/a'})")
        if req.reference_price:
            b = cost_breakdown(quote, req.reference_price, req.side)
            raw_dev = abs(quote.price / req.reference_price - 1) * 100
            fee_aware = self.settings.fee_aware_deviation and quote.fee_pct is not None
            dev = b["deviation_ex_fee_pct"] if fee_aware else raw_dev
            if dev > self.settings.max_quote_deviation_pct:
                failed.append(f"QUOTE_DEVIATES_FROM_MARKET(quote={quote.price:.8g} vs market={req.reference_price:.8g}"
                              f" = {raw_dev:.2f}% raw, {dev:.2f}% after the {quote.fee_pct if quote.fee_pct is not None else 'unknown'}% pool fee"
                              f" > {self.settings.max_quote_deviation_pct:.2f}%, amount_in={quote.amount_in:.6g}"
                              f", expected_out={quote.expected_out:.8g}, route={diag.get('route')}, venue={quote.venue or 'n/a'})")
            if req.side == Side.BUY and req.max_total_cost_pct is not None and b["total_cost_pct"] is not None \
                    and b["total_cost_pct"] > req.max_total_cost_pct:
                failed.append(f"TOTAL_COST_EXCEEDS_LIMIT(cost={b['total_cost_pct']:.2f}% > max={req.max_total_cost_pct:.2f}%"
                              f", fee={quote.fee_pct if quote.fee_pct is not None else 'unknown'}%, venue={quote.venue or 'n/a'})")
        # Liquidity floor protects ENTRIES. The DEX quote carries no pool liquidity, so fall back to the liquidity the
        # decision was made on. It is never applied to SELLs: a protective exit must not be blocked by a thin pool.
        if req.side == Side.BUY:
            liq = quote.pool_liquidity_usdc if quote.pool_liquidity_usdc is not None else req.reference_liquidity_usdc
            if liq is None or liq < auth.policy.min_liquidity_usdc:
                failed.append(f"QUOTE_LIQUIDITY_BELOW_POLICY_MIN(liquidity={liq}, min={auth.policy.min_liquidity_usdc})")
        allow = {r.lower() for r in auth.policy.allowed_routers} & {r.lower() for r in self.chain.router_allowlist()}
        if quote.router_address.lower() not in allow:
            failed.append("ROUTER_NOT_ALLOWLISTED")
        if failed:
            raise LiveSafetyError(failed)
        return quote, failed

    async def _execute(self, approved: ApprovedTrade) -> ExecutionResult:
        req = approved.request
        try:
            quote, _ = await self._preflight(approved)
        except (LiveSafetyError, ApprovalError, PolicyViolationError, IntegrationNotVerified) as exc:
            record_event(AuditKind.EXECUTION, AuditStatus.FAILED, provider=req.venue or "", operation="preflight",
                         component="execution.live", error=f"{type(exc).__name__}: {exc}", token_key=f"arc:{req.token_address.lower()}",
                         decision_id=req.decision_id, user_id=self.audit_user_id,
                         detail={"side": req.side.value, "amount_usdc": req.amount_usdc,
                                 "failed_checks": getattr(exc, "failed_checks", None)})
            return self._res(req, OrderStatus.REJECTED, error=f"{type(exc).__name__}: {exc}")
        deadline = self._now() + timedelta(seconds=self.settings.tx_deadline_s)
        try:
            tx = await self.chain.build_swap_tx(quote, req, deadline)
            sim = await self.chain.simulate(tx)
            if not sim.ok:
                return self._res(req, OrderStatus.REJECTED, error=f"SIMULATION_FAILED: {sim.detail}")
            if not await self.idem.claim(f"live:{req.idempotency_key}"):
                return self._res(req, OrderStatus.REJECTED, error="DUPLICATE_ORDER")
            sub = await self.wallet.submit(tx, self._now(), expected_chain_id=self.chain.chain_id)
        except (PolicyViolationError, IntegrationNotVerified) as exc:
            return self._res(req, OrderStatus.REJECTED, error=f"{type(exc).__name__}: {exc}")
        if sub.status == "PENDING_SIGNATURE":
            return self._res(req, OrderStatus.PENDING_SIGNATURE, signing_request_id=sub.signing_request_id,
                             requested_amount_usdc=req.amount_usdc)
        h = sub.tx_hash
        if not h and sub.provider_tx_id:
            h = await self.wallet.resolve_tx_hash(sub.provider_tx_id)
        if not h:  # accepted by the provider but no chain hash yet: never assume success, reconcile later
            return self._res(req, OrderStatus.SUBMITTED, provider_tx_id=sub.provider_tx_id, error="TX_HASH_PENDING_RECONCILE")
        receipt = await self.chain.wait_for_receipt(h, self.settings.confirm_timeout_s)
        if receipt is None:  # unknown outcome: never resubmit blindly; keep idempotency claim; reconcile later
            return self._res(req, OrderStatus.TIMEOUT, tx_hash=h, error="CONFIRMATION_TIMEOUT")
        if receipt.status != 1:
            await self.idem.release(f"live:{req.idempotency_key}")
            return self._res(req, OrderStatus.FAILED, tx_hash=h, error="TX_REVERTED")
        fill = await self.chain.parse_fill(receipt, quote)  # verifies the ACTUAL outcome
        return self._finish(req, h, fill, quote)

    def _finish(self, req: TradeRequest, tx_hash: str, fill, quote: Quote) -> ExecutionResult:
        now = self._now()
        if req.side == Side.BUY:
            spent = fill.filled_quantity * fill.avg_price + fill.fee_usdc
            pos = self.portfolio.apply_buy(chain=req.chain, launchpad=req.launchpad, token=req.token_address,
                                           symbol=None, qty=fill.filled_quantity, price=fill.avg_price, cost_usdc=spent,
                                           now=now, strategy_id=req.strategy_id, strategy_version=req.strategy_version,
                                           decision_id=req.decision_id)
            pid = pos.id
        else:
            pid = req.position_id
            proceeds = fill.filled_quantity * fill.avg_price - fill.fee_usdc
            self.portfolio.apply_sell(pid, fill.filled_quantity, fill.avg_price, proceeds, now)  # type: ignore[arg-type]
        return self._res(req, OrderStatus.FILLED, tx_hash=tx_hash, filled_quantity=fill.filled_quantity,
                         avg_price=fill.avg_price, fee_usdc=fill.fee_usdc, position_id=pid,
                         notional_usdc=fill.filled_quantity * fill.avg_price, price_impact_pct=quote.price_impact_pct)

    async def buy(self, approved: ApprovedTrade) -> ExecutionResult:
        return await self._execute(approved)

    async def sell(self, approved: ApprovedTrade) -> ExecutionResult:
        return await self._execute(approved)

    async def get_balance(self) -> float:
        return await self.wallet.get_usdc_balance()

    async def get_position(self, token_address: str):
        return self.portfolio.position_for_token(token_address)

    async def quote(self, request: TradeRequest) -> Quote:
        return await self.chain.quote(request)

    async def simulate(self, request: TradeRequest) -> dict:
        q = await self.chain.quote(request)
        tx = await self.chain.build_swap_tx(q, request, self._now() + timedelta(seconds=self.settings.tx_deadline_s))
        r = await self.chain.simulate(tx)
        return {"ok": r.ok, "detail": r.detail}


def assert_executor_matches_mode(mode: TradingMode, executor: ExecutionAdapter) -> None:
    """Paper executor can never serve LIVE, and a chain executor can never serve PAPER."""
    from app.core.errors import ModeMismatchError
    from app.execution.paper import PaperExecutionEngine

    if executor.mode != mode:
        raise ModeMismatchError(f"executor mode {executor.mode.value} != trading mode {mode.value}")
    if mode == TradingMode.LIVE and isinstance(executor, PaperExecutionEngine):
        raise ModeMismatchError("paper executor cannot be used in LIVE mode")
