"""Wire contract between the hosted control plane and a user's Local Runner.

Direction: the RUNNER always connects OUT to the control plane (no inbound ports on the user's machine).
Nothing in this protocol carries private keys, wallet sessions, OTPs or signing material, by design.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.types import TradingMode

DesiredState = Literal["RUNNING", "PAUSED", "STOPPED"]
ReportedState = Literal["STARTING", "RUNNING", "PAUSED", "STOPPED", "LIVE_BLOCKED"]
CommandType = Literal["CLOSE_POSITION", "CLOSE_ALL", "WITHDRAW_USDC"]


class PairRequest(BaseModel):
    code: str = Field(min_length=8, max_length=40)
    name: str = Field("runner", max_length=64)
    version: str = Field("", max_length=32)


class PairResponse(BaseModel):
    runner_id: str
    token: str  # shown once; only its hash is stored server-side


class ConfigBundle(BaseModel):
    """Everything the runner needs. The runner validates it and then applies its OWN local ceilings on top.

    Multi-tenant fields (``user_id``, wallet binding, ``platform_ceilings``) let a shared
    worker enforce per-user policy isolation. Single-user local runners may ignore them
    but still benefit from platform-clamped ``risk_limits``.
    """

    version: int
    runner_id: str
    mode: TradingMode
    desired_state: DesiredState
    emergency_stop: bool
    strategy: dict
    strategies_enabled: list[str]
    risk_limits: dict
    wallet_policy: dict | None = None
    controls: dict
    issued_at: datetime
    # Per-user isolation (shared worker + managed Privy wallets)
    user_id: str | None = None
    wallet_id: str | None = None
    wallet_address: str | None = None
    wallet_provider: str | None = None
    policy_version: int | None = None
    platform_ceilings: dict | None = None


class ConfigPoll(BaseModel):
    changed: bool
    bundle: ConfigBundle | None = None


class LiveStatus(BaseModel):
    available: bool = False
    reasons: list[str] = Field(default_factory=list)
    provider: str | None = None
    session_authorized: bool = False
    address: str | None = None
    chain_verified: bool = False
    locally_enabled: bool = False


class Heartbeat(BaseModel):
    version: str = ""
    state: ReportedState
    mode: TradingMode
    applied_config_version: int = 0
    strategy_version: int = 0
    data_source: str = ""
    data_status: str = ""
    ai: dict = Field(default_factory=dict)
    open_positions: int = 0
    last_activity_at: datetime | None = None
    last_decision: dict | None = None
    live: LiveStatus = Field(default_factory=LiveStatus)
    wallet_provider: str | None = None
    local_ceilings: dict = Field(default_factory=dict)
    entries_suspended_reason: str | None = None
    last_error: str | None = None


class Command(BaseModel):
    id: str
    type: CommandType
    payload: dict = Field(default_factory=dict)


class HeartbeatResponse(BaseModel):
    server_time: datetime
    desired_config_version: int
    commands: list[Command] = Field(default_factory=list)


class CommandAck(BaseModel):
    status: Literal["DONE", "FAILED"]
    detail: str = Field("", max_length=500)


class RunnerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)
    id: str = Field(min_length=1, max_length=64)
    type: str = Field(max_length=48)
    at: datetime
    correlation_id: str = Field("", max_length=64)
    payload: dict = Field(default_factory=dict)


class EventBatch(BaseModel):
    events: list[RunnerEvent] = Field(max_length=200)


class EventAck(BaseModel):
    last_seq: int
    accepted: int
    skipped: int
