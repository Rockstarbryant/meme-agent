from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db
from app.db import models as M
from app.db import repo

router = APIRouter(tags=["trading"])


class BuyAnywayIn(BaseModel):
    amount_usdc: float | None = None


async def _queue_command(db: AsyncSession, user_id: str, type_: str, payload: dict) -> M.RunnerCommand:
    """Mirrors app.api.routes_agent._queue: idempotent, don't double-queue the same command."""
    existing = (await db.execute(select(M.RunnerCommand).where(
        M.RunnerCommand.user_id == user_id, M.RunnerCommand.type == type_, M.RunnerCommand.status == "PENDING"
    ))).scalars().all()
    for c in existing:
        if c.payload == payload:
            return c
    cmd = M.RunnerCommand(user_id=user_id, type=type_, payload=payload)
    db.add(cmd)
    await db.flush()
    return cmd


def _ratio(m: dict):
    """Buy/sell pressure ratio for the opportunity list. Prefers real USD flow; falls back to trade counts (the
    same fallback MarketState.buy_sell_volume_ratio() applies) so the card isn't blank just because a provider
    only gave counts. ``m.get("buy_sell_basis")`` tells the frontend which kind of number this is."""
    b, s = m.get("buy_volume_5m"), m.get("sell_volume_5m")
    if b is not None and s is not None:
        return None if s == 0 and b == 0 else (10.0 if s == 0 else round(b / s, 3))
    bc, sc = m.get("buys_5m"), m.get("sells_5m")
    if bc is None and sc is None:
        return None
    bc, sc = bc or 0, sc or 0
    if bc == 0 and sc == 0:
        return None
    return 10.0 if sc == 0 else round(min(bc / sc, 10.0), 3)


def _opp(d: M.Decision) -> dict:
    m = d.market
    created = m.get("token_created_at")
    age = (d.created_at - datetime.fromisoformat(created)).total_seconds() if created else None
    scanned = m.get("scanned_at") or m.get("enriched_at") or m.get("timestamp")
    return {"decision_id": d.id, "token_key": d.token_key, "symbol": d.symbol, "token_name": m.get("token_name"), "chain": m.get("chain"), "launchpad": m.get("launchpad"),
            "launchpad_detected": m.get("launchpad_detected"), "launchpad_evidence": m.get("launchpad_evidence"),
            "age_seconds": age, "scanned_at": scanned, "price": m.get("price"), "market_cap": m.get("market_cap"), "liquidity": m.get("liquidity"),
            "volume_5m": m.get("volume_5m"), "unique_buyers_5m": m.get("unique_buyers_5m"), "buy_sell_ratio": _ratio(m),
            "buy_sell_basis": m.get("buy_sell_basis"),
            "price_change_5m": m.get("price_change_5m"), "price_change_15m": m.get("price_change_15m"),
            "holder_growth_pct": m.get("holder_growth_pct"), "holder_growth_window_s": m.get("holder_growth_window_s"),
            "top10_holder_pct": m.get("top10_holder_pct"), "holder_basis": m.get("holder_basis"),
            "creator_known": m.get("creator_known"), "creator_sold_pct": m.get("creator_sold_pct"),
            "mev_risk_score": m.get("mev_risk_score"), "mev_method": m.get("mev_method"),
            "enrichment_gaps": m.get("enrichment_gaps") or [],
            "strategy_score": d.score, "risk_score": d.risk_score, "ai_status": d.ai_status, "final_action": d.final_action,
            "final_reason": d.final_reason, "strategy_version": d.strategy_version, "mode": d.mode,
            "data_label": "DEMO DATA" if d.is_demo else "LIVE DATA", "at": d.created_at}


def _pos(p) -> dict:
    return {"id": p.id, "mode": p.mode if isinstance(p.mode, str) else p.mode.value, "chain": p.chain, "launchpad": p.launchpad,
            "token_address": p.token_address, "symbol": p.symbol, "status": p.status, "entry_price": p.entry_price,
            "quantity": p.quantity, "initial_quantity": p.initial_quantity, "last_price": p.last_price, "peak_price": p.peak_price,
            "cost_basis_usdc": p.cost_basis_usdc, "realized_pnl_usdc": p.realized_pnl_usdc,
            "unrealized_pnl_usdc": (p.quantity * p.last_price - p.cost_basis_usdc) if p.status == "OPEN" else 0.0,
            "gain_pct": (p.last_price / p.entry_price - 1) * 100 if p.entry_price else None, "tiers_hit": list(p.tiers_hit),
            "strategy_id": p.strategy_id, "strategy_version": p.strategy_version, "decision_id": p.decision_id,
            "opened_at": p.opened_at, "closed_at": p.closed_at,
            "exit_reason": (p.exit_reason if isinstance(p.exit_reason, (str, type(None))) else p.exit_reason.value)}


@router.get("/opportunities")
async def opportunities(request: Request, limit: int = Query(50, le=200), action: str | None = None, mode: str | None = None,
                        user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    # Only opportunities from the retention window (default 24h) are shown; older ones are also deleted hourly.
    # Decisions are made per trading mode: the list shows the CURRENT mode's (mode=ALL for both).
    scope = mode_scope(mode, user)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1.0, C(request).settings.retention_hours))
    q = select(M.Decision).where(M.Decision.user_id == user.id, M.Decision.created_at >= cutoff)
    if scope:
        q = q.where(M.Decision.mode == scope)
    rows = (await db.execute(q.order_by(M.Decision.created_at.desc()).limit(1000))).scalars().all()
    seen, out = set(), []
    for d in rows:  # latest decision per token
        if d.token_key in seen:
            continue
        seen.add(d.token_key)
        if action and d.final_action != action.upper():
            continue
        out.append(_opp(d))
        if len(out) >= limit:
            break
    return out


@router.get("/tokens/{token_key:path}")
async def token_detail(token_key: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    decisions = (await db.execute(select(M.Decision).where(M.Decision.user_id == user.id, M.Decision.token_key == token_key).order_by(M.Decision.created_at.desc()).limit(50))).scalars().all()
    snaps = (await db.execute(select(M.MarketSnapshot).where(M.MarketSnapshot.token_key == token_key).order_by(M.MarketSnapshot.at.desc()).limit(120))).scalars().all()
    if not decisions and not snaps:
        # Tokens shown on the Discovery page may not have been evaluated yet: serve the registry's latest snapshot.
        chain, _, addr = token_key.partition(":")
        reg = None
        if addr:
            reg = (await db.execute(select(M.LaunchpadTokenRow).where(M.LaunchpadTokenRow.chain == chain, M.LaunchpadTokenRow.token_address == addr.lower()))).scalars().first()
        snap = ((reg.meta or {}).get("last_snapshot") if reg else None)
        if reg is None or not isinstance(snap, dict):
            raise HTTPException(404, "unknown token")
        market = {**snap, "chain": reg.chain, "token_address": reg.token_address, "symbol": snap.get("symbol") or reg.symbol,
                  "token_name": snap.get("token_name") or reg.name}
        return {"token_key": token_key, "latest_market": market, "data_label": "LIVE DATA", "price_series": [],
                "latest_decision": None, "decision_history": [], "evaluated": False,
                "note": "Discovered but not evaluated by your agent yet. Strategy, risk and AI results appear after its next scan."}
    latest = decisions[0] if decisions else None
    return {"token_key": token_key, "latest_market": (snaps[0].data if snaps else latest.market), "evaluated": bool(decisions),
            "data_label": "DEMO DATA" if (snaps[0].is_demo if snaps else latest.is_demo) else "LIVE DATA",
            "price_series": [{"at": s.at, "price": s.data.get("price"), "liquidity": s.data.get("liquidity")} for s in reversed(snaps)],
            "latest_decision": _opp(latest) if latest else None,
            "decision_history": [_opp(d) for d in decisions]}


@router.post("/decisions/{decision_id}/buy-anyway")
async def buy_anyway(decision_id: str, body: BuyAnywayIn, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Manual override: force a BUY on a WATCH-listed opportunity even though the
    strategy/AI did not qualify it. This only skips the strategy/AI *opinion* --
    the risk engine still re-runs against FRESH market data on the runner right
    before anything is bought, and can still refuse the trade (bad liquidity, an
    unsafe/unsellable contract, exposure or position limits, etc.)."""
    d = await db.get(M.Decision, decision_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "not found")
    if d.final_action != "WATCH":
        raise HTTPException(409, "'buy anyway' is only offered for WATCH opportunities "
                                  "(REJECT was blocked by a risk/safety check; BUY already went through)")
    if d.mode != user.mode:
        raise HTTPException(409, f"this opportunity was evaluated in {d.mode} mode; switch to {d.mode} mode to buy it")
    token_address = (d.market or {}).get("token_address")
    if not token_address:
        raise HTTPException(422, "this decision has no token address on record")
    payload = {"decision_id": decision_id, "token_address": token_address, "amount_usdc": body.amount_usdc}
    cmd = await _queue_command(db, user.id, "FORCE_BUY", payload)
    await repo.audit(db, user.id, user.email, "MANUAL_BUY_REQUESTED", decision_id=decision_id, token=d.token_key)
    await db.commit()
    return {"queued": True, "command_id": cmd.id,
            "note": "Your Local Runner re-checks current market data and risk limits before buying (seconds). "
                    "It can still refuse the trade if conditions changed or a safety check fails."}


AI_RETRY_WINDOW_MIN = 15          # a scan older than this is stale; the agent re-scans on its own
AI_RETRY_COOLDOWN_S = 60


@router.post("/decisions/{decision_id}/retry-ai")
async def retry_ai(decision_id: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Re-run the AI step for a token whose AI assessment failed at scan time (UNAVAILABLE / INVALID).

    Only offered while the scan is fresh (15 minutes): after that the market data behind the decision is stale, and
    the agent re-evaluates the token by itself anyway. The runner re-fetches current data, so this is a full
    re-evaluation (strategy, risk, AI) that writes a new decision."""
    d = await db.get(M.Decision, decision_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "not found")
    age_s = (datetime.now(timezone.utc) - (d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=timezone.utc))).total_seconds()
    if age_s > AI_RETRY_WINDOW_MIN * 60:
        raise HTTPException(409, f"This scan is {int(age_s // 60)} minutes old. Retry is only offered for {AI_RETRY_WINDOW_MIN} minutes after a scan; the agent will re-scan this token on its own.")
    if d.mode != user.mode:
        raise HTTPException(409, f"this decision was made in {d.mode} mode; switch to {d.mode} mode to retry it")
    ais = (await db.execute(select(M.AIAnalysisRow).where(M.AIAnalysisRow.decision_id == decision_id))).scalars().all()
    failed = any(a.status in ("UNAVAILABLE", "INVALID") for a in ais) or (not ais and "AI_UNAVAILABLE" in (d.final_reason or ""))
    if not failed:
        raise HTTPException(409, "The AI did not fail on this scan, so there is nothing to retry.")
    token_address = (d.market or {}).get("token_address")
    if not token_address:
        raise HTTPException(422, "this decision has no token address on record")
    recent = (await db.execute(select(M.RunnerCommand).where(M.RunnerCommand.user_id == user.id, M.RunnerCommand.type == "RETRY_AI")
                               .order_by(M.RunnerCommand.created_at.desc()).limit(10))).scalars().all()
    now = datetime.now(timezone.utc)
    for c in recent:
        if (c.payload or {}).get("decision_id") == decision_id:
            created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() < AI_RETRY_COOLDOWN_S:
                raise HTTPException(429, f"A retry for this token was just requested; wait {AI_RETRY_COOLDOWN_S} seconds.")
    cmd = M.RunnerCommand(user_id=user.id, type="RETRY_AI", payload={"decision_id": decision_id, "token_address": token_address})
    db.add(cmd)
    await db.flush()
    await repo.audit(db, user.id, user.email, "AI_RETRY_REQUESTED", decision_id=decision_id, token=d.token_key)
    await db.commit()
    return {"queued": True, "command_id": cmd.id, "note": "Your runner re-fetches current data and asks the AI again (seconds). A new decision replaces this one."}


@router.get("/decisions/{decision_id}")
async def decision_detail(decision_id: str, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Answers: why did the agent buy (or not buy) this token?"""
    d = await db.get(M.Decision, decision_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "not found")
    ev = lambda m, col: select(m).where(col == decision_id)  # noqa: E731
    signal = (await db.execute(ev(M.StrategySignalRow, M.StrategySignalRow.decision_id))).scalars().all()
    risks = (await db.execute(ev(M.RiskAssessmentRow, M.RiskAssessmentRow.decision_id))).scalars().all()
    ais = (await db.execute(ev(M.AIAnalysisRow, M.AIAnalysisRow.decision_id))).scalars().all()
    events = (await db.execute(select(M.EventRow).where(M.EventRow.correlation_id == decision_id).order_by(M.EventRow.at))).scalars().all()
    orders = (await db.execute(select(M.Order).where(M.Order.decision_id == decision_id))).scalars().all()
    positions = (await db.execute(select(M.Position).where(M.Position.decision_id == decision_id))).scalars().all()
    return {
        "decision": {"id": d.id, "at": d.created_at, "mode": d.mode, "token_key": d.token_key, "final_action": d.final_action,
                     "final_reason": d.final_reason, "data_label": "DEMO DATA" if d.is_demo else "LIVE DATA"},
        "what_the_agent_saw": d.market,
        "strategy": {"id": d.strategy_id, "version": d.strategy_version, "config_snapshot": d.strategy_config,
                     "signal": signal[0].data if signal else None},
        "risk": {"limits": d.risk_limits, "controls": d.controls, "wallet_policy": d.wallet_policy,
                 "assessments": [{"stage": r.stage, "decision": r.decision, "risk_score": r.risk_score, **r.data} for r in risks]},
        "ai": [{"provider": a.provider, "model": a.model, "prompt_version": a.prompt_version, "status": a.status,
                "response": a.response, "error": a.error} for a in ais],
        "sized_amount_usdc": d.sized_amount_usdc,
        "execution": [{"order_id": o.id, "status": o.status, "simulated": o.simulated, "tx_hash": o.tx_hash, "avg_price": o.avg_price,
                       "filled_quantity": o.filled_quantity, "fee_usdc": o.fee_usdc, "slippage_pct": o.slippage_pct, "error": o.error} for o in orders],
        "positions": [_pos(p) for p in positions],
        "events": [{"at": e.at, "type": e.type, "payload": {k: v for k, v in e.payload.items() if k != "decision"}} for e in events],
    }


def mode_scope(mode: str | None, user: M.User) -> str | None:
    """Which trading mode a history list shows: the account's CURRENT mode by default, PAPER or LIVE on request,
    ALL for both. PAPER and LIVE history are different things (virtual vs real money) and are never mixed unless asked."""
    m = (mode or user.mode or "PAPER").upper()
    if m == "ALL":
        return None
    if m not in ("PAPER", "LIVE"):
        raise HTTPException(422, "mode must be PAPER, LIVE or ALL")
    return m


def _utc(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


@router.get("/positions")
async def positions(status: str | None = None, mode: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    scope = mode_scope(mode, user)
    q = select(M.Position).where(M.Position.user_id == user.id).order_by(M.Position.opened_at.desc()).limit(200)
    if scope:
        q = q.where(M.Position.mode == scope)
    if status:
        q = q.where(M.Position.status == status.upper())
    rows = (await db.execute(q)).scalars().all()
    out = [_pos(p) for p in rows]
    # Exit details come from the position's SELL orders: average exit price and the proceeds actually received.
    sells = (await db.execute(select(M.Order).where(M.Order.user_id == user.id, M.Order.side == "SELL", M.Order.status == "FILLED",
                                                    *( [M.Order.mode == scope] if scope else [] ))
                              .order_by(M.Order.created_at.desc()).limit(1000))).scalars().all()
    now = datetime.now(timezone.utc)
    for p, row in zip(rows, out):
        opened, closed = _utc(p.opened_at), _utc(p.closed_at)
        mine = [o for o in sells if o.token_address.lower() == p.token_address.lower() and _utc(o.created_at) >= opened
                and (closed is None or _utc(o.created_at) <= closed + timedelta(minutes=1))]
        qty = sum(o.filled_quantity or 0.0 for o in mine)
        proceeds = sum((o.filled_quantity or 0.0) * (o.avg_price or 0.0) - (o.fee_usdc or 0.0) for o in mine)
        row["exit_price"] = (sum((o.filled_quantity or 0.0) * (o.avg_price or 0.0) for o in mine) / qty) if qty else None
        row["proceeds_usdc"] = proceeds if mine else None
        row["exit_count"] = len(mine)
        row["invested_usdc"] = p.total_invested_usdc
        row["held_seconds"] = int(((closed or now) - opened).total_seconds()) if opened else None
        row["return_pct"] = (p.realized_pnl_usdc / p.total_invested_usdc * 100.0) if (p.status == "CLOSED" and p.total_invested_usdc) else None
        row["drawdown_from_peak_pct"] = ((p.last_price / p.peak_price - 1.0) * 100.0) if p.peak_price else None
    return out


@router.get("/orders")
async def orders(limit: int = Query(100, le=500), mode: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db), request: Request = None):
    scope = mode_scope(mode, user)
    q = select(M.Order).where(M.Order.user_id == user.id).order_by(M.Order.created_at.desc()).limit(limit)
    if scope:
        q = q.where(M.Order.mode == scope)
    rows = (await db.execute(q)).scalars().all()
    ex = (C(request).settings.arc_explorer_url or "").rstrip("/")
    # An explorer link only exists for a REAL transaction. Paper orders have no transaction, and used to be given the
    # bare explorer address as if they had one.
    return [{"id": o.id, "decision_id": o.decision_id, "mode": o.mode, "side": o.side, "token_address": o.token_address, "status": o.status,
             "simulated": o.simulated, "label": "PAPER (SIMULATED)" if o.simulated else "LIVE", "tx_hash": o.tx_hash,
             "explorer_url": (f"{ex}/tx/{o.tx_hash}" if (ex and o.tx_hash and not o.simulated) else None),
             "requested_amount_usdc": o.requested_amount_usdc, "filled_quantity": o.filled_quantity, "avg_price": o.avg_price,
             "fee_usdc": o.fee_usdc, "slippage_pct": o.slippage_pct, "error": o.error, "at": o.created_at} for o in rows]


@router.get("/portfolio")
async def portfolio(request: Request, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.core.clock import utcnow
    from app.services import control
    from app.services.decision import tighten_limits
    c, now = C(request), utcnow()
    row = (await db.execute(select(M.Portfolio).where(M.Portfolio.user_id == user.id, M.Portfolio.mode == user.mode))).scalar_one_or_none()
    open_ = (await db.execute(select(M.Position).where(M.Position.user_id == user.id, M.Position.mode == user.mode, M.Position.status == "OPEN"))).scalars().all()
    policy = await control.load_policy(db, user.id)
    limits = tighten_limits(await control.load_limits(db, c.settings, user.id), policy)
    st = ((await control.active_runner(db, user.id)) or M.Runner(status={})).status or {}
    cash = row.cash_usdc if row else (policy.allocated_capital_usdc if policy else c.settings.paper_starting_usdc)
    expo = sum(p.quantity * p.last_price for p in open_)
    unreal = [p.quantity * p.last_price - p.cost_basis_usdc for p in open_]
    realized_today = row.realized_today_usdc if row and row.day == now.date().isoformat() else 0.0
    snaps = (await db.execute(select(M.PortfolioSnapshot).join(M.Portfolio, M.Portfolio.id == M.PortfolioSnapshot.portfolio_id)
                              .where(M.Portfolio.user_id == user.id, M.Portfolio.mode == user.mode).order_by(M.PortfolioSnapshot.at.desc()).limit(200))).scalars().all()
    from app.services.performance import derive_starting_value
    realized_total = row.realized_total_usdc if row else 0.0
    start, start_derived = derive_starting_value(row.starting_cash_usdc if row else cash, cash + expo, realized_total, sum(unreal))
    return {"mode": user.mode, "label": user.mode, "data_source": st.get("data_source") or "no runner connected", "reported_by_runner": row is not None,
            "cash_usdc": cash, "exposure_usdc": expo, "total_value_usdc": cash + expo,
            "starting_cash_usdc": start, "starting_cash_derived": start_derived, "realized_pnl_usdc": realized_total,
            "unrealized_pnl_usdc": sum(unreal), "daily_pnl_usdc": realized_today + sum(min(u, 0.0) for u in unreal),
            "open_positions": len(open_), "limits": limits.model_dump(mode="json"),
            "snapshots": [{"at": x.at, "total_value_usdc": x.total_value_usdc, "daily_pnl_usdc": x.daily_pnl_usdc} for x in reversed(snaps)]}


_RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "all": None}


@router.get("/portfolio/history")
async def portfolio_history(range: str = Query("24h"), user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Portfolio value over time for the dashboard chart, downsampled to ~240 points without losing peaks and dips."""
    from app.services.performance import downsample
    if range not in _RANGES:
        raise HTTPException(422, f"range must be one of {', '.join(_RANGES)}")
    q = (select(M.PortfolioSnapshot.at, M.PortfolioSnapshot.total_value_usdc)
         .join(M.Portfolio, M.Portfolio.id == M.PortfolioSnapshot.portfolio_id)
         .where(M.Portfolio.user_id == user.id, M.Portfolio.mode == user.mode))
    secs = _RANGES[range]
    if secs:
        q = q.where(M.PortfolioSnapshot.at >= datetime.now(timezone.utc) - timedelta(seconds=secs))
    rows = (await db.execute(q.order_by(M.PortfolioSnapshot.at.desc()).limit(20000))).all()
    pts = [(_utc(at).timestamp(), float(v)) for at, v in reversed(rows)]
    ds = downsample(pts, 240)
    vals = [v for _, v in pts]
    return {"range": range, "points": [{"t": t, "v": v} for t, v in ds], "samples": len(pts),
            "first": vals[0] if vals else None, "last": vals[-1] if vals else None,
            "high": max(vals) if vals else None, "low": min(vals) if vals else None}


@router.get("/portfolio/performance")
async def portfolio_performance(user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Win rate, profit factor and breakdowns from the account's closed positions in the current mode."""
    from app.services.performance import performance
    rows = (await db.execute(select(M.Position).where(M.Position.user_id == user.id, M.Position.mode == user.mode, M.Position.status == "CLOSED")
                             .order_by(M.Position.closed_at.desc()).limit(500))).scalars().all()
    return performance(rows)


@router.get("/activity")
async def activity(limit: int = Query(100, le=500), type: str | None = None, mode: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Events, newest first. Events about a decision (orders, positions, exits) carry that decision's trading mode and are
    filtered by it; system events (agent state, errors, kill switch) belong to no single mode and always appear."""
    scope = mode_scope(mode, user)
    q = select(M.EventRow, M.Decision.mode).outerjoin(M.Decision, M.Decision.id == M.EventRow.correlation_id).where(M.EventRow.user_id == user.id)
    if scope:
        q = q.where((M.Decision.mode == scope) | M.Decision.id.is_(None))
    if type:
        q = q.where(M.EventRow.type == type.upper())
    rows = (await db.execute(q.order_by(M.EventRow.at.desc()).limit(limit))).all()
    return [{"id": e.id, "type": e.type, "at": e.at, "correlation_id": e.correlation_id, "mode": dmode,
             "payload": {k: v for k, v in e.payload.items() if k != "decision"}} for e, dmode in rows]


@router.get("/audit-logs")
async def audit_logs(limit: int = Query(100, le=500), user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.AuditLog).where(M.AuditLog.user_id == user.id).order_by(M.AuditLog.at.desc()).limit(limit))).scalars().all()
    return [{"at": r.at, "actor": r.actor, "action": r.action, "detail": r.detail} for r in rows]
