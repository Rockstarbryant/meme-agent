"""Agentic analyst: the model may call READ-ONLY tools before it decides, then also chooses WHERE to buy.

Why: the single-shot analyst only saw a frozen digest and answered BUY/WATCH/REJECT. It could not see whether the token could
actually be bought within the user's limits, so BUY decisions died later in the live pre-flight. Here the model can
  * get_market_data      - the latest merged market state, with which provider supplied what (optionally refreshed);
  * get_trading_policy   - the user's risk limits, price-impact / total-cost caps, wallet policy, sizing band, mode;
  * get_wallet_state     - USDC available, exposure, open positions;
  * list_venues          - enabled trading venues and whether each can place live orders;
  * quote_venues         - real quotes from every venue for an order size, already scored against the limits.
and finally answer with the usual decision JSON plus a ``venue``.

Hard guarantees (none of them depend on the model behaving):
  * tools are read-only; the model cannot trade, pick contracts, set calldata or change a limit;
  * the pipeline re-quotes at the FINAL order size and only trades on a venue that passes the user's limits and can execute;
    if the model's venue fails but another passes, the passing one is used; if none passes, the buy is skipped with the reason;
  * tool results contain numbers and provider/venue names only (remote error text is sanitised and truncated);
  * the loop is bounded (``max_steps``); every step is written to the audit trail with the decision id.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from app.ai.analyzer import SYSTEM_PROMPT, _extract_object, _normalise, _r, build_user_prompt
from app.ai.provider import AIProviderError, LLMProvider
from app.ai.schemas import AIDecision, AIOutcome
from app.core.types import Side, TradingMode
from app.domain.market import MarketState
from app.domain.trade import TradeRequest
from app.observability.audit import AuditKind, AuditStatus, record_event
from app.risk.engine import RiskAssessment
from app.strategies.base import StrategySignal
from app.venues.router import VenueRouter

PROMPT_VERSION = "agent-v1"

TOOL_PROTOCOL = """

TOOLS (optional). You may look things up before deciding. To call a tool reply with ONE JSON object and nothing else:
  {"tool": "<name>", "args": {...}}
You will get the result and may call another tool. When you are ready, reply with the final decision JSON described above
(no "tool" key) and ADD the key "venue": the venue you want to buy on ("uniswap", "kyberswap", ...) or null.
Tools:
  get_market_data   args: {"refresh": true|false}   latest merged market state; "sources" says which provider supplied which field.
  get_trading_policy args: {}                        user's limits (max price impact %, max total cost %, slippage), wallet policy, sizing band, mode.
  get_wallet_state  args: {}                         USDC available, exposure, open positions.
  list_venues       args: {}                         venues you can choose from and whether each can execute live orders.
  quote_venues      args: {"amount_usdc": number}    real quote per venue for a BUY of that size, scored against the user's limits
                                                     (fee_pct, price_impact_pct, total_cost_pct, passes_policy, executable).
Rules for choosing a venue (BUY only):
  - Prefer a venue with passes_policy=true AND executable=true; among those, the one with the highest expected_out.
  - If NO venue passes the limits, do NOT buy: answer WATCH and name the failing cost (for example a 5% pool fee) in reasoning_summary.
  - A cost that is mostly pool fee is a property of that pool; no other tool changes it. Do not retry the same tool with the same args.
The digest below already contains policy, wallet and a venue quote at the maximum size, so most decisions need no tool call."""

AGENT_SYSTEM_PROMPT = SYSTEM_PROMPT + TOOL_PROTOCOL

_SAFE = re.compile(r"[^A-Za-z0-9 .,:;%()_\-/=|+]")


def _clean(text: Any, n: int = 140) -> str:
    return _SAFE.sub("", str(text))[:n]


@dataclass
class ToolContext:
    market: MarketState
    signal: StrategySignal
    risk: RiskAssessment
    sizing: dict | None
    limits: Any                         # RiskLimits
    mode: str                           # "PAPER" | "LIVE"
    decision_id: str = ""
    wallet_policy: Any = None           # WalletPolicy | None
    router: VenueRouter | None = None
    refresh_market: Callable[[], Awaitable[MarketState]] | None = None
    wallet_state: Callable[[], Awaitable[dict]] | None = None
    quotes_seen: list[dict] = field(default_factory=list)
    refreshed: bool = False

    def request(self, amount_usdc: float) -> TradeRequest:
        """A throw-away BUY request used ONLY to ask venues for quotes (never submitted)."""
        m = self.market
        return TradeRequest(
            idempotency_key=f"quote:{self.decision_id or m.token_address}:{int(time.time())}",
            mode=TradingMode.LIVE if self.mode == "LIVE" else TradingMode.PAPER, chain="arc", token_address=m.token_address,
            side=Side.BUY, amount_usdc=float(amount_usdc), max_slippage_pct=float(getattr(self.limits, "max_slippage_pct", 2.0)),
            reference_price=m.price, reference_liquidity_usdc=m.liquidity, decision_id=self.decision_id,
            max_price_impact_pct=float(getattr(self.limits, "max_price_impact_pct", 2.0)),
            max_total_cost_pct=float(getattr(self.limits, "max_total_cost_pct", 5.0)))


# ------------------------------------------------------------------------------------------------ tools
async def tool_get_market_data(ctx: ToolContext, args: dict) -> dict:
    refreshed = False
    if args.get("refresh") and ctx.refresh_market is not None and not ctx.refreshed:
        ctx.refreshed = True
        try:
            ctx.market = await ctx.refresh_market()
            refreshed = True
        except Exception as exc:  # noqa: BLE001
            return {"error": _clean(f"refresh failed: {type(exc).__name__}"), "refreshed": False}
    m = ctx.market
    fs: dict[str, list[str]] = {}
    for f, src in (m.field_sources or {}).items():
        fs.setdefault(src, []).append(f)
    return {"refreshed": refreshed, "price": _r(m.price), "liquidity_usd": _r(m.liquidity), "market_cap_usd": _r(m.market_cap),
            "holder_count": m.holder_count, "top10_holder_pct": _r(m.top10_holder_pct),
            "volume_5m": _r(m.volume_5m), "price_change_5m_pct": _r(m.price_change_5m),
            "answered_by": list(m.data_sources)[:8], "sources": {k: v[:12] for k, v in fs.items()},
            "data_gaps": [_clean(g, 100) for g in (m.enrichment_gaps or [])[:6]]}


async def tool_get_trading_policy(ctx: ToolContext, args: dict) -> dict:
    L = ctx.limits
    out: dict[str, Any] = {
        "mode": ctx.mode,
        "max_price_impact_pct": getattr(L, "max_price_impact_pct", None),
        "max_total_cost_pct": getattr(L, "max_total_cost_pct", None),
        "max_slippage_pct": getattr(L, "max_slippage_pct", None),
        "min_liquidity_usdc": getattr(L, "min_liquidity_usdc", None),
        "max_open_positions": getattr(L, "max_open_positions", None),
        "sizing": ctx.sizing or {},
        "risk_flags": [{"code": f.code, "severity": str(getattr(f.severity, "value", f.severity))}
                       for f in (ctx.risk.flags or [])[:10]] if hasattr(ctx.risk, "flags") else [],
    }
    wp = ctx.wallet_policy
    if wp is not None:
        out["wallet_policy"] = {"max_trade_usdc": wp.max_trade_usdc, "max_position_usdc": wp.max_position_usdc,
                                "max_slippage_pct": wp.max_slippage_pct, "min_liquidity_usdc": wp.min_liquidity_usdc,
                                "max_open_positions": wp.max_open_positions, "allowed_router_count": len(wp.allowed_routers)}
    return out


async def tool_get_wallet_state(ctx: ToolContext, args: dict) -> dict:
    if ctx.wallet_state is None:
        return {"available": False}
    try:
        return await ctx.wallet_state()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": _clean(type(exc).__name__)}


async def tool_list_venues(ctx: ToolContext, args: dict) -> dict:
    if ctx.router is None:
        return {"venues": [], "note": "no live venues in this mode; the venue field is ignored"}
    return {"venues": [{"venue": v["venue"], "can_execute_live": v["executable"], "note": _clean(v["note"], 100)}
                       for v in ctx.router.status()]}


async def tool_quote_venues(ctx: ToolContext, args: dict) -> dict:
    if ctx.router is None:
        return {"available": False, "note": "no venue router in this mode"}
    sz = ctx.sizing or {}
    lo, hi = float(sz.get("min_order_usdc") or 0.0), float(sz.get("max_order_usdc") or 0.0)
    try:
        amt = float(args.get("amount_usdc") or hi)
    except (TypeError, ValueError):
        amt = hi
    if hi > 0:
        amt = min(max(amt, lo), hi)
    if amt <= 0 or not ctx.market.price:
        return {"available": False, "note": "no usable order size or reference price"}
    only = args.get("venues") if isinstance(args.get("venues"), list) else None
    res = await ctx.router.quote_all(ctx.request(amt), only=[str(x).lower() for x in only] if only else None)
    views = [r.agent_view() for r in res]
    for v in views:
        if "error" in v:
            v["error"] = _clean(v["error"])
        v["policy_failures"] = [_clean(x, 120) for x in v.get("policy_failures", [])]
    best = ctx.router.best(res)
    ctx.quotes_seen = views
    return {"amount_usdc": round(amt, 2), "quotes": views, "best_executable_within_policy": best.venue if best else None}


TOOLS: dict[str, Callable[[ToolContext, dict], Awaitable[dict]]] = {
    "get_market_data": tool_get_market_data, "get_trading_policy": tool_get_trading_policy,
    "get_wallet_state": tool_get_wallet_state, "list_venues": tool_list_venues, "quote_venues": tool_quote_venues,
}


# ------------------------------------------------------------------------------------------------ parsing
def parse_step(text: str) -> tuple[str, Any]:
    """('tool', (name, args)) or ('final', AIDecision). Raises on anything else so the provider chain can try the next model."""
    obj = _extract_object(text)
    if "tool" in obj or "tool_call" in obj:
        call = obj.get("tool_call") if isinstance(obj.get("tool_call"), dict) else obj
        name = str(call.get("tool") or call.get("name") or "").strip()
        if name not in TOOLS:
            raise ValueError(f"unknown tool '{_clean(name, 40)}'")
        args = call.get("args") or call.get("arguments") or {}
        return "tool", (name, args if isinstance(args, dict) else {})
    return "final", AIDecision.model_validate(_normalise(obj))


# ------------------------------------------------------------------------------------------------ analyzer
class AIAgentAnalyzer:
    def __init__(self, provider: LLMProvider | None, *, max_steps: int = 5, prefetch: bool = True):
        self.provider = provider
        self.max_steps = max(1, min(int(max_steps), 8))
        self.prefetch = prefetch

    async def analyze(self, m: MarketState, s: StrategySignal, r: RiskAssessment, sizing: dict | None = None,
                      ctx: ToolContext | None = None) -> AIOutcome:
        if self.provider is None:
            return AIOutcome(status="DISABLED", prompt_version=PROMPT_VERSION)
        base = dict(provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION)
        if ctx is None:   # no tool context available -> behaves like the single-shot analyst
            ctx = ToolContext(market=m, signal=s, risk=r, sizing=sizing, limits=_DefaultLimits(), mode="PAPER")
        digest = json.loads(build_user_prompt(m, s, r, sizing))
        if self.prefetch:
            digest["policy"] = await tool_get_trading_policy(ctx, {})
            digest["wallet"] = await tool_get_wallet_state(ctx, {})
            if ctx.router is not None:
                digest["venue_quotes_at_max_size"] = await tool_quote_venues(ctx, {})
        digest["data_provenance"] = (await tool_get_market_data(ctx, {}))["sources"]
        transcript: list[dict] = []
        trace: list[dict] = []
        attempts: list[str] = []
        done_provider, done_model = self.provider.name, self.provider.model
        for step in range(1, self.max_steps + 1):
            last = step == self.max_steps
            user = json.dumps({"digest": digest, "tool_results_so_far": transcript,
                               **({"instruction": "No more tool calls: reply with the final decision JSON now."} if last else {})},
                              separators=(",", ":"), default=str)

            def _validate(text: str, _last=last) -> None:
                kind, _ = parse_step(text)
                if _last and kind != "final":
                    raise ValueError("final decision required")

            t0 = time.monotonic()
            try:
                done = await self.provider.complete_ex(AGENT_SYSTEM_PROMPT, user, validate=_validate)
            except AIProviderError as exc:
                return AIOutcome(status="UNAVAILABLE", error=str(exc)[:300], tool_trace=trace, venue_quotes=ctx.quotes_seen, **base)
            except Exception as exc:  # noqa: BLE001
                return AIOutcome(status="UNAVAILABLE", error=type(exc).__name__, tool_trace=trace, venue_quotes=ctx.quotes_seen, **base)
            done_provider, done_model, attempts = done.provider, done.model, list(done.attempts)
            ident = {**base, "provider": done_provider, "model": done_model, "attempts": attempts}
            try:
                kind, payload = parse_step(done.text)
            except (ValidationError, ValueError) as exc:
                return AIOutcome(status="INVALID", raw=done.text[:4000], error=f"{type(exc).__name__}: {exc}"[:300],
                                 tool_trace=trace, venue_quotes=ctx.quotes_seen, **ident)
            if kind == "final":
                trace.append({"step": step, "final": True, "latency_ms": round((time.monotonic() - t0) * 1000)})
                record_event(AuditKind.AI_AGENT, AuditStatus.OK, provider=done_provider, model=done_model, operation="final",
                             component="ai.agent", latency_ms=(time.monotonic() - t0) * 1000, decision_id=ctx.decision_id,
                             token_key=f"arc:{m.token_address.lower()}",
                             detail={"action": payload.action, "venue": payload.venue, "steps": step, "failed_before": attempts[:4]})
                return AIOutcome(status="OK", decision=payload, raw=json.dumps({"final": done.text[:1500], "trace": trace})[:4000],
                                 tool_trace=trace, venue_quotes=ctx.quotes_seen, **ident)
            name, args = payload
            if any(t.get("tool") == name and t.get("args") == args for t in trace):
                result: dict = {"error": "duplicate call; use the earlier result"}
            else:
                try:
                    result = await TOOLS[name](ctx, args)
                except Exception as exc:  # noqa: BLE001 - a broken tool becomes a tool error the model can read
                    result = {"error": _clean(f"{type(exc).__name__}")}
            trace.append({"step": step, "tool": name, "args": args, "ok": "error" not in result,
                          "latency_ms": round((time.monotonic() - t0) * 1000)})
            record_event(AuditKind.AI_AGENT, AuditStatus.OK if "error" not in result else AuditStatus.FAILED, provider=done_provider,
                         model=done_model, operation=f"tool:{name}", component="ai.agent", latency_ms=(time.monotonic() - t0) * 1000,
                         decision_id=ctx.decision_id, token_key=f"arc:{m.token_address.lower()}", error=str(result.get("error", "")),
                         detail={"args": args, "result_keys": list(result)[:10]})
            transcript.append({"tool": name, "args": args, "result": result})
        return AIOutcome(status="INVALID", error="agent loop ended without a final decision", tool_trace=trace,
                         venue_quotes=ctx.quotes_seen, **{**base, "provider": done_provider, "model": done_model})


class _DefaultLimits:
    max_price_impact_pct = 2.0
    max_total_cost_pct = 5.0
    max_slippage_pct = 2.0
