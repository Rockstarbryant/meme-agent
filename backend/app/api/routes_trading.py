from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import C, current_user, get_db
from app.db import models as M

router = APIRouter(tags=["trading"])


def _ratio(m: dict):
    b, s = m.get("buy_volume_5m"), m.get("sell_volume_5m")
    return None if b is None or s in (None, 0) else round(b / s, 3)


def _opp(d: M.Decision) -> dict:
    m = d.market
    created = m.get("token_created_at")
    age = (d.created_at - datetime.fromisoformat(created)).total_seconds() if created else None
    return {"decision_id": d.id, "token_key": d.token_key, "symbol": d.symbol, "chain": m.get("chain"), "launchpad": m.get("launchpad"),
            "age_seconds": age, "price": m.get("price"), "market_cap": m.get("market_cap"), "liquidity": m.get("liquidity"),
            "volume_5m": m.get("volume_5m"), "unique_buyers_5m": m.get("unique_buyers_5m"), "buy_sell_ratio": _ratio(m),
            "holder_growth_pct": m.get("holder_growth_pct"), "top10_holder_pct": m.get("top10_holder_pct"),
            "creator_known": m.get("creator_known"), "creator_sold_pct": m.get("creator_sold_pct"),
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
async def opportunities(limit: int = Query(50, le=200), action: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.Decision).where(M.Decision.user_id == user.id).order_by(M.Decision.created_at.desc()).limit(1000))).scalars().all()
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
        raise HTTPException(404, "unknown token")
    latest = decisions[0] if decisions else None
    return {"token_key": token_key, "latest_market": (snaps[0].data if snaps else latest.market),
            "data_label": "DEMO DATA" if (snaps[0].is_demo if snaps else latest.is_demo) else "LIVE DATA",
            "price_series": [{"at": s.at, "price": s.data.get("price"), "liquidity": s.data.get("liquidity")} for s in reversed(snaps)],
            "latest_decision": _opp(latest) if latest else None,
            "decision_history": [_opp(d) for d in decisions]}


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


@router.get("/positions")
async def positions(status: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(M.Position).where(M.Position.user_id == user.id).order_by(M.Position.opened_at.desc()).limit(200)
    if status:
        q = q.where(M.Position.status == status.upper())
    return [_pos(p) for p in (await db.execute(q)).scalars().all()]


@router.get("/orders")
async def orders(limit: int = Query(100, le=500), user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db), request: Request = None):
    rows = (await db.execute(select(M.Order).where(M.Order.user_id == user.id).order_by(M.Order.created_at.desc()).limit(limit))).scalars().all()
    ex = C(request).settings.arc_explorer_url or None
    return [{"id": o.id, "decision_id": o.decision_id, "mode": o.mode, "side": o.side, "token_address": o.token_address, "status": o.status,
             "simulated": o.simulated, "label": "PAPER (SIMULATED)" if o.simulated else "LIVE", "tx_hash": o.tx_hash, "explorer_url": ex,
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
    return {"mode": user.mode, "label": user.mode, "data_source": st.get("data_source") or "no runner connected", "reported_by_runner": row is not None,
            "cash_usdc": cash, "exposure_usdc": expo, "total_value_usdc": cash + expo,
            "starting_cash_usdc": row.starting_cash_usdc if row else cash, "realized_pnl_usdc": row.realized_total_usdc if row else 0.0,
            "unrealized_pnl_usdc": sum(unreal), "daily_pnl_usdc": realized_today + sum(min(u, 0.0) for u in unreal),
            "open_positions": len(open_), "limits": limits.model_dump(mode="json"),
            "snapshots": [{"at": x.at, "total_value_usdc": x.total_value_usdc, "daily_pnl_usdc": x.daily_pnl_usdc} for x in reversed(snaps)]}


@router.get("/activity")
async def activity(limit: int = Query(100, le=500), type: str | None = None, user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(M.EventRow).where(M.EventRow.user_id == user.id).order_by(M.EventRow.at.desc()).limit(limit)
    if type:
        q = q.where(M.EventRow.type == type.upper())
    return [{"id": e.id, "type": e.type, "at": e.at, "correlation_id": e.correlation_id,
             "payload": {k: v for k, v in e.payload.items() if k != "decision"}} for e in (await db.execute(q)).scalars().all()]


@router.get("/audit-logs")
async def audit_logs(limit: int = Query(100, le=500), user: M.User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(M.AuditLog).where(M.AuditLog.user_id == user.id).order_by(M.AuditLog.at.desc()).limit(limit))).scalars().all()
    return [{"at": r.at, "actor": r.actor, "action": r.action, "detail": r.detail} for r in rows]
