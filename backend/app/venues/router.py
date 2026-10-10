"""VenueRouter: quotes every enabled venue, scores each against the user's limits and picks where to trade.

It also implements the small adapter surface ``ArcAdapter`` expects from its ``dex`` (quote / build_swap_tx / simulate /
parse_fill / router_allowlist), so the live execution engine keeps working unchanged: the quote it gets back already says
which venue produced it, and build/simulate/parse are routed to that venue.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.chains.base import FillDetails, SimulationResult, TxReceipt
from app.core.errors import DataUnavailable, IntegrationNotVerified
from app.core.types import Side
from app.domain.trade import Quote, TradeRequest, UnsignedTransaction
from app.observability.audit import AuditKind, AuditStatus, record_event
from app.venues.base import Venue, VenuePolicy, VenueResult
from app.venues.cost import cost_breakdown


class VenueRouter:
    name = "venue_router"
    live_trading_verified = True

    def __init__(self, venues: dict[str, Venue], *, policy: VenuePolicy | None = None, timeout_s: float = 8.0):
        if not venues:
            raise ValueError("VenueRouter needs at least one venue")
        self.venues = dict(venues)
        self.policy = policy or VenuePolicy()
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ info
    def names(self) -> list[str]:
        return list(self.venues)

    def status(self) -> list[dict]:
        out = []
        for n, v in self.venues.items():
            ok, why = v.executable()
            out.append({"venue": n, "label": v.label or n, "executable": ok, "note": why})
        return out

    def router_allowlist(self) -> set[str]:
        out: set[str] = set()
        for v in self.venues.values():
            out |= {a.lower() for a in v.router_allowlist()}
        return out

    def policy_for(self, request: TradeRequest) -> VenuePolicy:
        p = self.policy.model_copy()
        if request.max_price_impact_pct is not None:
            p.max_price_impact_pct = request.max_price_impact_pct
        if request.max_total_cost_pct is not None:
            p.max_total_cost_pct = request.max_total_cost_pct
        return p

    # ------------------------------------------------------------------ scoring
    def evaluate(self, venue: str, q: Quote, request: TradeRequest, latency_ms: float | None = None) -> VenueResult:
        pol = self.policy_for(request)
        b = cost_breakdown(q, request.reference_price, request.side)
        fails: list[str] = []
        if q.price_impact_pct is not None and q.price_impact_pct > pol.max_price_impact_pct:
            fails.append(f"PRICE_IMPACT {q.price_impact_pct:.2f}% > {pol.max_price_impact_pct:.2f}%")
        if b["total_cost_pct"] is not None and b["total_cost_pct"] > pol.max_total_cost_pct:
            fails.append(f"TOTAL_COST {b['total_cost_pct']:.2f}% > {pol.max_total_cost_pct:.2f}% (fees {q.fee_pct if q.fee_pct is not None else 'unknown'}%)")
        if b["unexplained_cost_pct"] is not None and b["unexplained_cost_pct"] > pol.max_unexplained_pct:
            fails.append(f"UNEXPLAINED_COST {b['unexplained_cost_pct']:.2f}% > {pol.max_unexplained_pct:.2f}% (not fee, not reported impact)")
        if b["deviation_ex_fee_pct"] is not None:
            if b["deviation_ex_fee_pct"] > pol.max_deviation_pct:
                fails.append(f"QUOTE_DEVIATES {b['deviation_ex_fee_pct']:.2f}% after the {q.fee_pct}% pool fee > {pol.max_deviation_pct:.2f}%")
        elif b["total_cost_pct"] is not None and b["total_cost_pct"] < -pol.max_deviation_pct:
            fails.append(f"QUOTE_FAR_BELOW_MARKET {abs(b['total_cost_pct']):.2f}% (stale reference price or manipulated pool)")
        v = self.venues.get(venue)
        ok_exec, why = v.executable() if v else (False, "unknown venue")
        return VenueResult(venue=venue, ok=True, latency_ms=latency_ms, amount_in_usdc=request.amount_usdc, expected_out=q.expected_out,
                           price=q.price, fee_pct=q.fee_pct, price_impact_pct=q.price_impact_pct, total_cost_pct=b["total_cost_pct"],
                           unexplained_cost_pct=b["unexplained_cost_pct"], route=(q.diagnostics or {}).get("route"),
                           router_address=q.router_address, executable=ok_exec, not_executable_reason=why,
                           passes_policy=not fails, policy_failures=fails, quote=q)

    async def _one(self, name: str, request: TradeRequest) -> VenueResult:
        t0 = time.monotonic()
        try:
            q = await asyncio.wait_for(self.venues[name].quote(request), timeout=self.timeout_s)
            if not q.venue:
                q = q.model_copy(update={"venue": name})
            lat = (time.monotonic() - t0) * 1000
            res = self.evaluate(name, q, request, lat)
            record_event(AuditKind.VENUE_QUOTE, AuditStatus.OK, provider=name, operation="quote", component="venue_router",
                         latency_ms=lat, token_key=f"arc:{request.token_address.lower()}", decision_id=request.decision_id,
                         detail={"side": request.side.value, "amount_usdc": request.amount_usdc, **res.agent_view()})
            return res
        except asyncio.TimeoutError:
            err = f"timeout after {self.timeout_s:.0f}s"
        except (DataUnavailable, IntegrationNotVerified) as exc:
            err = str(exc)
        except Exception as exc:  # noqa: BLE001 - a venue bug must not break the others
            err = f"{type(exc).__name__}: {exc}"
        lat = (time.monotonic() - t0) * 1000
        record_event(AuditKind.VENUE_QUOTE, AuditStatus.FAILED, provider=name, operation="quote", component="venue_router",
                     latency_ms=lat, error=err, token_key=f"arc:{request.token_address.lower()}", decision_id=request.decision_id)
        return VenueResult(venue=name, ok=False, error=err[:300], latency_ms=round(lat, 1))

    async def quote_all(self, request: TradeRequest, only: list[str] | None = None) -> list[VenueResult]:
        names = [n for n in (only or self.names()) if n in self.venues]
        return list(await asyncio.gather(*(self._one(n, request) for n in names)))

    @staticmethod
    def best(results: list[VenueResult], *, need_executable: bool = True) -> VenueResult | None:
        cands = [r for r in results if r.ok and r.passes_policy and (r.executable or not need_executable)]
        return max(cands, key=lambda r: (r.expected_out or 0.0), default=None)

    # ------------------------------------------------------------------ ChainAdapter "dex" surface
    async def quote(self, request: TradeRequest) -> Quote:
        if request.venue:
            if request.venue not in self.venues:
                raise IntegrationNotVerified("venue", f"'{request.venue}' is not an enabled venue ({', '.join(self.names())})")
            r = await self._one(request.venue, request)
            if not r.ok or r.quote is None:
                raise DataUnavailable(f"{request.venue} quote failed: {r.error}")
            return r.quote
        results = await self.quote_all(request)
        pick = self.best(results) or self.best(results, need_executable=False) \
            or max((r for r in results if r.ok and r.quote), key=lambda r: r.expected_out or 0.0, default=None)
        if pick is None or pick.quote is None:
            raise DataUnavailable("no venue could quote: " + "; ".join(f"{r.venue}: {r.error}" for r in results)[:400])
        return pick.quote

    def _venue_for(self, quote: Quote) -> Venue:
        v = self.venues.get(quote.venue or "uniswap")
        if v is None:
            raise IntegrationNotVerified("venue", f"quote came from unknown venue '{quote.venue}'")
        return v

    async def build_swap_tx(self, quote: Quote, request: TradeRequest, deadline: datetime) -> UnsignedTransaction:
        return await self._venue_for(quote).build_swap_tx(quote, request, deadline)

    async def simulate(self, tx: UnsignedTransaction) -> SimulationResult:
        for v in self.venues.values():
            if tx.to.lower() in {a.lower() for a in v.router_allowlist()}:
                return await v.simulate(tx)
        return await next(iter(self.venues.values())).simulate(tx)

    async def parse_fill(self, receipt: TxReceipt, quote: Quote) -> FillDetails:
        return await self._venue_for(quote).parse_fill(receipt, quote)

    async def aclose(self) -> None:
        for v in self.venues.values():
            try:
                await v.aclose()
            except Exception:  # noqa: BLE001
                pass
