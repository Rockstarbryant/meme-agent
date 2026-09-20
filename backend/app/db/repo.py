"""Persistence helpers: decisions, orders, positions, portfolios, settings, audit logs."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import ExitReason, TradingMode
from app.db import models as M
from app.db.base import uid
from app.portfolio.state import PortfolioState, Position


def _dt(v: str | datetime | None) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


async def audit(s: AsyncSession, user_id: str | None, actor: str, action: str, **detail) -> None:
    s.add(M.AuditLog(user_id=user_id, actor=actor, action=action, detail=detail))


async def save_decision(s: AsyncSession, user_id: str, d: dict) -> None:
    sig, mkt = d["signal"], d["market"]
    ai = d.get("ai")
    s.add(M.Decision(id=d["id"], user_id=user_id, mode=d["mode"], token_key=d["token_key"], symbol=mkt.get("symbol"),
                     is_demo=bool(mkt.get("is_demo")), final_action=d["final_action"], final_reason=d["final_reason"],
                     strategy_id=d["strategy_id"], strategy_version=d["strategy_version"], score=sig["score"],
                     risk_score=d["pre_risk"]["risk_score"], qualified=sig["qualified"],
                     ai_status=(ai or {}).get("status", "NONE"), sized_amount_usdc=d.get("sized_amount_usdc", 0.0),
                     strategy_config=d["strategy_config"], risk_limits=d["risk_limits"], controls=d["controls"],
                     wallet_policy=d.get("wallet_policy"), market=mkt, created_at=_dt(d["created_at"])))
    await s.flush()
    s.add(M.StrategySignalRow(decision_id=d["id"], strategy_id=sig["strategy_id"], strategy_version=sig["strategy_version"],
                              score=sig["score"], qualified=sig["qualified"], data=sig))
    for stage, key in (("PRE", "pre_risk"), ("FINAL", "final_risk")):
        if d.get(key):
            r = d[key]
            s.add(M.RiskAssessmentRow(id=r["id"], decision_id=d["id"], stage=stage, decision=r["decision"],
                                      risk_score=r["risk_score"], data=r))
    if ai:
        s.add(M.AIAnalysisRow(decision_id=d["id"], provider=ai.get("provider", ""), model=ai.get("model", ""),
                              prompt_version=ai.get("prompt_version", ""), status=ai["status"],
                              response=ai.get("decision"), raw=ai.get("raw", ""), error=ai.get("error", "")))


async def save_order(s: AsyncSession, user_id: str, o: dict, decision_id: str = "") -> bool:
    ex = await s.get(M.Order, o["order_id"])
    if ex is not None and ex.user_id != user_id:
        return False  # never let one user's runner overwrite another user's row
    await s.merge(M.Order(id=o["order_id"], user_id=user_id, decision_id=decision_id, idempotency_key=o["idempotency_key"],
                          mode=o["mode"], side=o["side"], token_address=o["token_address"], status=o["status"],
                          simulated=o["simulated"], tx_hash=o.get("tx_hash"), requested_amount_usdc=o.get("requested_amount_usdc"),
                          filled_quantity=o.get("filled_quantity", 0.0), avg_price=o.get("avg_price"),
                          fee_usdc=o.get("fee_usdc", 0.0), slippage_pct=o.get("slippage_pct"), error=o.get("error"),
                          data=o, created_at=_dt(o.get("at")) or datetime.now().astimezone()))
    if o["status"] in ("FILLED", "PARTIALLY_FILLED") and (await s.execute(select(M.Trade.id).where(M.Trade.order_id == o["order_id"]))).first() is None:
        await s.flush()
        s.add(M.Trade(user_id=user_id, order_id=o["order_id"], position_id=o.get("position_id"), side=o["side"],
                      quantity=o["filled_quantity"], price=o["avg_price"] or 0.0, notional_usdc=o.get("notional_usdc", 0.0),
                      fee_usdc=o.get("fee_usdc", 0.0), simulated=o["simulated"], tx_hash=o.get("tx_hash"),
                      at=_dt(o.get("at")) or datetime.now().astimezone()))
    return True


async def upsert_position_dict(s: AsyncSession, user_id: str, d: dict) -> bool:
    ex = await s.get(M.Position, d["id"])
    if ex is not None and ex.user_id != user_id:
        return False
    await upsert_position(s, user_id, Position.model_validate(d))
    return True


async def upsert_position(s: AsyncSession, user_id: str, p: Position) -> None:
    await s.merge(M.Position(
        id=p.id, user_id=user_id, mode=p.mode.value, chain=p.chain, launchpad=p.launchpad, token_address=p.token_address,
        symbol=p.symbol, status=p.status, entry_price=p.entry_price, quantity=p.quantity, initial_quantity=p.initial_quantity,
        cost_basis_usdc=p.cost_basis_usdc, total_invested_usdc=p.total_invested_usdc, realized_pnl_usdc=p.realized_pnl_usdc,
        peak_price=p.peak_price, last_price=p.last_price, tiers_hit=list(p.tiers_hit), strategy_id=p.strategy_id,
        strategy_version=p.strategy_version, decision_id=p.decision_id, opened_at=p.opened_at,
        last_new_high_at=p.last_new_high_at, closed_at=p.closed_at, exit_reason=p.exit_reason.value if p.exit_reason else None))


async def save_portfolio(s: AsyncSession, user_id: str, pf: PortfolioState, now: datetime) -> str:
    row = (await s.execute(select(M.Portfolio).where(M.Portfolio.user_id == user_id, M.Portfolio.mode == pf.mode.value))).scalar_one_or_none()
    pf.daily_pnl(now)  # roll the day
    if row is None:
        row = M.Portfolio(user_id=user_id, mode=pf.mode.value, cash_usdc=pf.cash_usdc, starting_cash_usdc=pf.starting_cash)
        s.add(row)
    row.cash_usdc, row.realized_total_usdc = pf.cash_usdc, pf.realized_total
    row.realized_today_usdc, row.day, row.updated_at = pf.realized_today, (pf.day.isoformat() if pf.day else ""), now
    await s.flush()
    return row.id


async def load_portfolio(s: AsyncSession, user_id: str, mode: TradingMode, default_cash: float,
                         now: datetime) -> PortfolioState:
    row = (await s.execute(select(M.Portfolio).where(M.Portfolio.user_id == user_id, M.Portfolio.mode == mode.value))).scalar_one_or_none()
    pf = PortfolioState(row.cash_usdc if row else default_cash, mode)
    if row:
        pf.starting_cash, pf.realized_total = row.starting_cash_usdc, row.realized_total_usdc
        if row.day == now.date().isoformat():  # restart must not reset the daily loss counter
            pf.day, pf.realized_today = now.date(), row.realized_today_usdc
        rows = (await s.execute(select(M.Position).where(M.Position.user_id == user_id, M.Position.mode == mode.value,
                                                        M.Position.status == "OPEN"))).scalars().all()
        for r in rows:
            pf.positions[r.id] = Position(id=r.id, mode=mode, chain=r.chain, launchpad=r.launchpad, token_address=r.token_address,
                                          symbol=r.symbol, opened_at=r.opened_at, entry_price=r.entry_price, quantity=r.quantity,
                                          initial_quantity=r.initial_quantity, cost_basis_usdc=r.cost_basis_usdc,
                                          total_invested_usdc=r.total_invested_usdc, realized_pnl_usdc=r.realized_pnl_usdc,
                                          peak_price=r.peak_price, last_price=r.last_price, last_new_high_at=r.last_new_high_at,
                                          tiers_hit=list(r.tiers_hit or []), status="OPEN", strategy_id=r.strategy_id,
                                          strategy_version=r.strategy_version, decision_id=r.decision_id)
    return pf


async def get_setting(s: AsyncSession, key: str) -> dict | None:
    row = await s.get(M.SystemSetting, key)
    return row.value if row else None


async def put_setting(s: AsyncSession, key: str, value: dict, by: str) -> None:
    row = await s.get(M.SystemSetting, key)
    if row is None:
        s.add(M.SystemSetting(key=key, value=value, updated_by=by))
    else:
        row.value, row.updated_by, row.updated_at = value, by, datetime.now().astimezone()


async def save_portfolio_dict(s: AsyncSession, user_id: str, d: dict, now: datetime) -> str:
    row = (await s.execute(select(M.Portfolio).where(M.Portfolio.user_id == user_id, M.Portfolio.mode == d["mode"]))).scalar_one_or_none()
    if row is None:
        row = M.Portfolio(user_id=user_id, mode=d["mode"], cash_usdc=d["cash_usdc"], starting_cash_usdc=d["starting_cash"])
        s.add(row)
    row.cash_usdc, row.starting_cash_usdc, row.realized_total_usdc = d["cash_usdc"], d["starting_cash"], d["realized_total"]
    row.realized_today_usdc, row.day, row.updated_at = d["realized_today"], d.get("day") or "", now
    await s.flush()
    return row.id
