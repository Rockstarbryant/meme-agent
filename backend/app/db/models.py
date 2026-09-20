"""All persistent state. PostgreSQL in production (JSONB)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now, uid

PK = dict(primary_key=True, default=uid)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), **PK)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")  # LIVE is never the default
    created_at: Mapped[datetime] = mapped_column(default=now)


class Chain(Base):
    __tablename__ = "chains"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    chain_id: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    live_trading_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Launchpad(Base):
    __tablename__ = "launchpads"
    id: Mapped[str] = mapped_column(String(32), **PK)
    chain_id: Mapped[str] = mapped_column(ForeignKey("chains.id"))
    name: Mapped[str] = mapped_column(String(64))
    descriptor: Mapped[dict] = mapped_column(default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("chain_id", "name"),)


class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[str] = mapped_column(String(32), **PK)
    chain_id: Mapped[str] = mapped_column(ForeignKey("chains.id"))
    address: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creator_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    launchpad: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(default=now)
    __table_args__ = (UniqueConstraint("chain_id", "address"),)


class Pool(Base):
    __tablename__ = "pools"
    id: Mapped[str] = mapped_column(String(32), **PK)
    chain_id: Mapped[str] = mapped_column(ForeignKey("chains.id"))
    address: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str] = mapped_column(ForeignKey("tokens.id"))
    launchpad: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("chain_id", "address"),)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[str] = mapped_column(String(32), **PK)
    token_key: Mapped[str] = mapped_column(String(200), index=True)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column()


class Wallet(Base):
    __tablename__ = "wallets"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    ownership_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Provider-side id (e.g. Privy wallet id). Null for browser/paper wallets.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    __table_args__ = (UniqueConstraint("user_id", "chain", "address"),)


class WalletPolicyRow(Base):
    __tablename__ = "wallet_policies"
    id: Mapped[str] = mapped_column(String(32), **PK)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    policy: Mapped[dict] = mapped_column()
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=now)


class WalletAuthorizationRow(Base):
    __tablename__ = "wallet_authorizations"
    id: Mapped[str] = mapped_column(String(32), **PK)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id"), index=True)
    capability: Mapped[str] = mapped_column(String(32))
    policy_id: Mapped[str] = mapped_column(ForeignKey("wallet_policies.id"))
    granted_at: Mapped[datetime] = mapped_column(default=now)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    proof_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)


class SigningRequestRow(Base):
    __tablename__ = "signing_requests"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tx: Mapped[dict] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    tx_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=now)


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[str] = mapped_column(String(32), **PK)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=now)
    __table_args__ = (UniqueConstraint("strategy_id", "user_id", "version"),)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))
    token_key: Mapped[str] = mapped_column(String(200), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    final_action: Mapped[str] = mapped_column(String(8))
    final_reason: Mapped[str] = mapped_column(Text)
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[float] = mapped_column(Float)
    qualified: Mapped[bool] = mapped_column(Boolean)
    ai_status: Mapped[str] = mapped_column(String(16), default="NONE")
    sized_amount_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    strategy_config: Mapped[dict] = mapped_column()
    risk_limits: Mapped[dict] = mapped_column()
    controls: Mapped[dict] = mapped_column()
    wallet_policy: Mapped[dict | None] = mapped_column(nullable=True)
    market: Mapped[dict] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(index=True)
    __table_args__ = (Index("ix_decisions_user_token_created", "user_id", "token_key", "created_at"),)


class StrategySignalRow(Base):
    __tablename__ = "strategy_signals"
    id: Mapped[str] = mapped_column(String(32), **PK)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    qualified: Mapped[bool] = mapped_column(Boolean)
    data: Mapped[dict] = mapped_column()


class RiskAssessmentRow(Base):
    __tablename__ = "risk_assessments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    stage: Mapped[str] = mapped_column(String(8))
    decision: Mapped[str] = mapped_column(String(8))
    risk_score: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column()


class AIAnalysisRow(Base):
    __tablename__ = "ai_analyses"
    id: Mapped[str] = mapped_column(String(32), **PK)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16))
    response: Mapped[dict | None] = mapped_column(nullable=True)
    raw: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    decision_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(4))
    token_address: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))
    simulated: Mapped[bool] = mapped_column(Boolean)
    tx_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    requested_amount_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    # DB-level duplicate backstop: one live/pending/filled order per key; definitive failures may be retried.
    __table_args__ = (Index("uq_orders_idem_active", "idempotency_key", unique=True,
                            postgresql_where=text("status NOT IN ('FAILED','REJECTED')")),)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))
    chain: Mapped[str] = mapped_column(String(32))
    launchpad: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_address: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(8), index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    initial_quantity: Mapped[float] = mapped_column(Float)
    cost_basis_usdc: Mapped[float] = mapped_column(Float)
    total_invested_usdc: Mapped[float] = mapped_column(Float)
    realized_pnl_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    peak_price: Mapped[float] = mapped_column(Float)
    last_price: Mapped[float] = mapped_column(Float)
    tiers_hit: Mapped[list] = mapped_column(default=list)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_version: Mapped[int] = mapped_column(Integer, default=0)
    decision_id: Mapped[str] = mapped_column(String(32), default="")
    opened_at: Mapped[datetime] = mapped_column()
    last_new_high_at: Mapped[datetime] = mapped_column()
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Portfolio(Base):
    __tablename__ = "portfolios"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(8))
    cash_usdc: Mapped[float] = mapped_column(Float)
    starting_cash_usdc: Mapped[float] = mapped_column(Float)
    realized_total_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    realized_today_usdc: Mapped[float] = mapped_column(Float, default=0.0)  # survives restarts: daily-loss limit
    day: Mapped[str] = mapped_column(String(10), default="")
    updated_at: Mapped[datetime] = mapped_column(default=now)
    __table_args__ = (UniqueConstraint("user_id", "mode"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[str] = mapped_column(String(32), **PK)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    cash_usdc: Mapped[float] = mapped_column(Float)
    exposure_usdc: Mapped[float] = mapped_column(Float)
    total_value_usdc: Mapped[float] = mapped_column(Float)
    daily_pnl_usdc: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    position_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    notional_usdc: Mapped[float] = mapped_column(Float)
    fee_usdc: Mapped[float] = mapped_column(Float)
    simulated: Mapped[bool] = mapped_column(Boolean)
    tx_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    at: Mapped[datetime] = mapped_column(default=now, index=True)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    at: Mapped[datetime] = mapped_column(index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    payload: Mapped[dict] = mapped_column()


class AuditLog(Base):
    """User/operator actions: mode switches, emergency stop, limit changes, wallet actions."""
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(32), **PK)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict] = mapped_column(default=dict)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(default=now)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")


class Runner(Base):
    """A user's Local Runner. Only the token HASH is stored; the runner never receives keys and the server never holds any."""
    __tablename__ = "runners"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)  # highest event sequence ingested (replay protection)
    status: Mapped[dict] = mapped_column(default=dict)         # latest heartbeat
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RunnerCommand(Base):
    __tablename__ = "runner_commands"
    id: Mapped[str] = mapped_column(String(32), **PK)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)  # PENDING | DONE | FAILED
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class AgentConfig(Base):
    """Desired state + config version per user. Every change that affects the runner bumps `version`."""
    __tablename__ = "agent_configs"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    desired_state: Mapped[str] = mapped_column(String(8), default="STOPPED")
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    strategies_enabled: Mapped[list] = mapped_column(default=lambda: ["traction_momentum"])
    # self_hosted = user pairs Local Runner; cloud_managed = platform shared worker + per-user Privy wallet
    execution_mode: Mapped[str] = mapped_column(String(16), default="self_hosted")
    updated_at: Mapped[datetime] = mapped_column(default=now)
