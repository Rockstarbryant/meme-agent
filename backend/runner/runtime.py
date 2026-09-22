from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable

from app.ai.analyzer import AIAnalyzer
from app.ai.provider import build_provider
from app.chains.arc.adapter import ArcAdapter
from app.chains.arc.market_data import UnavailableArcMarketData
from app.chains.arc.market_data import BitqueryArcMarketData
from app.chains.arc.uniswap import UniswapArcAdapter
from app.integrations.bitquery import BitqueryClient
from app.chains.base import MarketDataProvider
from app.chains.demo import DemoMarketData
from app.chains.evm import EvmRpcClient
from app.core.clock import utcnow
from app.core.errors import DataUnavailable
from app.core.types import AIMode, TradingMode
from app.domain.runner_protocol import (Command, ConfigBundle, EventBatch, Heartbeat, LiveStatus, RunnerEvent)
from app.events.bus import AuditSink, Event, EventBus, EventType as E
from app.execution.live import ArcExecutionEngine, LiveSettings
from app.execution.paper import PaperExecutionEngine
from app.portfolio.controls import ControlState
from app.portfolio.exits import PositionManager
from app.portfolio.state import PortfolioState
from app.risk.approval import TradeApprover
from app.risk.engine import RiskEngine, RiskLimits
from app.services.decision import DecisionPipeline, tighten_limits
from app.services.engine import TradingEngine
from app.strategies.traction_momentum import TractionMomentum, TractionMomentumConfig
from app.wallets.base import WalletPolicy, WalletProvider
from runner import __version__
from runner.client import ControlPlaneClient, ControlPlaneError, Revoked
from runner.settings import RunnerSettings, apply_ceilings, approval_secret
from runner.store import LocalStore, SqliteIdempotencyStore
from runner.wallets import build_wallet_provider

log = logging.getLogger("arc-runner")
_UNSET = object()
_POSITION_EVENTS = {E.ORDER_FILLED, E.POSITION_OPENED, E.POSITION_UPDATED, E.POSITION_CLOSED}


class OutboxSink(AuditSink):
    """Every event is written to the durable local outbox FIRST (and the portfolio is saved), then uploaded when possible."""

    def __init__(self, rt: "RunnerRuntime"):
        self.rt = rt

    async def write(self, ev: Event) -> None:
        rt, p = self.rt, dict(ev.payload)
        pid = (p.get("order") or {}).get("position_id") or ev.correlation_id
        if ev.type in _POSITION_EVENTS and rt.portfolio and pid in rt.portfolio.positions:
            p["position"] = rt.portfolio.positions[pid].model_dump(mode="json")
        if ev.type in (E.ORDER_FILLED, E.POSITION_CLOSED) and rt.portfolio:
            p["portfolio"] = rt.portfolio_summary()
        dedupe = f"pu:{pid}" if ev.type == E.POSITION_UPDATED else None
        rt.store.outbox_add({"id": ev.id, "type": ev.type.value, "at": ev.at.isoformat(), "correlation_id": ev.correlation_id, "payload": p}, dedupe)
        if ev.type in _POSITION_EVENTS and rt.portfolio:
            key = getattr(rt, "_portfolio_key", None) or f"portfolio:{rt.portfolio.mode.value}"
            rt.store.kv_set(key, rt.portfolio.to_dict())


class RunnerRuntime:
    def __init__(self, settings: RunnerSettings, client: ControlPlaneClient, store: LocalStore, *, market_data=_UNSET, wallet=_UNSET,
                 chain=_UNSET, llm=_UNSET, clock: Callable[[], datetime] = utcnow, mono: Callable[[], float] = time.monotonic):
        self.s, self.client, self.store, self.clock, self.mono = settings, client, store, clock, mono
        self.ceilings = settings.ceilings
        self.wallet: WalletProvider | None = build_wallet_provider(settings) if wallet is _UNSET else wallet  # type: ignore[assignment]
        self._bitquery = None
        self._uniswap = None
        if chain is _UNSET:
            rpc = EvmRpcClient(settings.rpc_urls)
            # Prefer explicit wallet address from the active provider settings
            wallet_addr = (
                settings.circle_wallet_address
                or settings.privy_wallet_address
                or ""
            )
            if settings.uniswap_api_key and wallet_addr:
                self._uniswap = UniswapArcAdapter(
                    settings.uniswap_api_key.get_secret_value(), rpc, wallet_addr, settings.uniswap_api_url
                )
            self.chain = ArcAdapter(
                rpc, settings.network.chain_id, settings.network.explorer_url,
                dex=self._uniswap, live_trading_verified=settings.live_trading_verified,
            )
        else:
            self.chain = chain
        self.llm = build_provider(settings) if llm is _UNSET else llm
        self.market_data: MarketDataProvider = market_data if market_data is not _UNSET else (
            DemoMarketData() if settings.data_source == "demo" else self._build_arc_market_data(settings))  # type: ignore[assignment]
        self.data_kind = "DEMO DATA" if isinstance(self.market_data, DemoMarketData) else "arc/bitquery"
        self.approver = TradeApprover(approval_secret(settings.state_dir) if settings.state_dir else "x" * 32)
        self.idem = SqliteIdempotencyStore(store)
        self.bus = EventBus([OutboxSink(self)])
        self.bundle: ConfigBundle | None = None
        self.applied_version = 0
        self.engine: TradingEngine | None = None
        self.portfolio: PortfolioState | None = None
        self.controls = ControlState(global_pause=True)
        self.mode_eff: TradingMode | None = None
        self.state = "STARTING"
        self.live = LiveStatus()
        self._live_at = -1e9
        self.last_contact: float | None = None
        self.entries_suspended_reason: str | None = None
        self.last_error: str | None = None
        self.last_activity_at: datetime | None = None
        self.last_decision: dict | None = None
        self.data_status = "idle"
        self.revoked = False
        self._evaluated: dict[str, datetime] = {}
        self._mkt_at: dict[str, float] = {}
        self._pf_at = -1e9
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def _build_arc_market_data(self, settings: RunnerSettings) -> MarketDataProvider:
        if not settings.bitquery_api_key:
            return UnavailableArcMarketData()
        self._bitquery = BitqueryClient(settings.bitquery_api_key.get_secret_value(), settings.bitquery_endpoint)
        return BitqueryArcMarketData(self._bitquery)

    # ------------------------------------------------------------ helpers
    def portfolio_summary(self) -> dict:
        pf = self.portfolio
        assert pf is not None
        return {"mode": pf.mode.value, "cash_usdc": pf.cash_usdc, "starting_cash": pf.starting_cash, "realized_today": pf.realized_today,
                "realized_total": pf.realized_total, "day": pf.day.isoformat() if pf.day else None}

    def contact_ok(self) -> bool:
        return self.last_contact is not None and (self.mono() - self.last_contact) <= self.s.max_offline_s

    def _touch(self) -> None:
        self.last_contact = self.mono()

    def _failed(self, e: Exception) -> None:
        self.last_error = str(e)[:200]

    async def live_status(self, force: bool = False) -> LiveStatus:
        if not force and self.mono() - self._live_at < 30:
            return self.live
        reasons: list[str] = []
        if not self.s.live_enabled:
            reasons.append("LIVE is disabled locally on this runner (ARC_RUNNER_LIVE_ENABLED=false)")
        if self.chain is None or not getattr(self.chain, "live_trading_verified", False):
            reasons.append(
                "LIVE remains fail-closed until ARC_RUNNER_LIVE_TRADING_VERIFIED=true "
                "(run `python -m runner circle-verify`, review capture, then set the flag)"
            )
        ready = None
        if self.wallet is None:
            reasons.append("no wallet provider configured (ARC_RUNNER_WALLET_PROVIDER)")
        else:
            ready = await self.wallet.live_readiness()
            reasons += ready.reasons
        if self.llm is None:
            reasons.append("LIVE entries require an AI provider configured on this runner")
        self.live = LiveStatus(available=not reasons and bool(ready and ready.available), reasons=reasons, provider=self.wallet.name if self.wallet else None,
                               session_authorized=bool(ready and ready.session_authorized), address=ready.address if ready else None,
                               chain_verified=bool(getattr(self.chain, "live_trading_verified", False)), locally_enabled=self.s.live_enabled)
        self._live_at = self.mono()
        return self.live

    # ------------------------------------------------------------ config
    async def apply_bundle(self, b: ConfigBundle) -> None:
        self.bundle = b
        live = await self.live_status(force=True)
        eff = TradingMode.PAPER if b.mode == TradingMode.PAPER else (TradingMode.LIVE if live.available else None)
        if eff is None:  # LIVE requested but not permitted here: NEVER fall back to paper silently, just do not trade
            self.mode_eff, self.engine, self.portfolio, self.state = None, None, None, "LIVE_BLOCKED"
            self.applied_version = b.version
            return
        if eff != self.mode_eff or self.engine is None:
            self._load_portfolio(eff, b)
        self._assemble(b)
        self.applied_version = b.version
        self.recompute()
        self.store.kv_set("config", b.model_dump(mode="json"))

    def _load_portfolio(self, mode: TradingMode, b: ConfigBundle) -> None:
        # Isolate portfolio state per user when the control plane sends user_id (shared worker)
        scope = b.user_id or "local"
        key = f"portfolio:{scope}:{mode.value}"
        saved = self.store.kv_get(key) or self.store.kv_get(f"portfolio:{mode.value}")  # backward compatible
        cash = (b.wallet_policy or {}).get("allocated_capital_usdc") or self.s.paper_starting_usdc
        self.portfolio = PortfolioState.from_dict(saved) if saved else PortfolioState(cash, mode)
        self.mode_eff = mode
        self._portfolio_key = key

    def _assemble(self, b: ConfigBundle) -> None:
        assert self.portfolio is not None and self.mode_eff is not None
        from app.risk.per_user_policy import PlatformCeilings, UserPolicyContext, effective_limits

        policy = WalletPolicy(**b.wallet_policy) if b.wallet_policy else None
        if policy is not None and self.wallet is not None and hasattr(self.wallet, "configure_policy"):
            self.wallet.configure_policy(policy)
        platform = PlatformCeilings(**b.platform_ceilings) if b.platform_ceilings else None
        # Control plane already clamped; re-apply platform + local ceilings fail-closed on the worker
        base = RiskLimits(**b.risk_limits)
        limits = effective_limits(base, policy, platform, self.ceilings)
        if b.user_id and b.wallet_id and b.wallet_address:
            self.user_policy_ctx = UserPolicyContext(
                user_id=b.user_id,
                wallet_id=b.wallet_id,
                wallet_address=b.wallet_address,
                wallet_provider=b.wallet_provider or (self.wallet.name if self.wallet else "none"),
                policy_version=b.policy_version or 0,
                risk_limits=limits,
                wallet_policy=policy,
                emergency_stop=b.emergency_stop,
                global_pause=b.desired_state != "RUNNING",
                mode=b.mode.value if hasattr(b.mode, "value") else str(b.mode),
            )
        else:
            self.user_policy_ctx = None
        cfg = TractionMomentumConfig(**b.strategy)
        self.strategy_version = cfg.version
        pipeline = DecisionPipeline(TractionMomentum(cfg), RiskEngine(), limits, self.approver, AIAnalyzer(self.llm),
                                    AIMode.ENABLED if self.llm else AIMode.DISABLED, entry_window_seconds=cfg.entry_window_seconds, wallet_policy=policy)
        if self.mode_eff == TradingMode.PAPER:
            executor = PaperExecutionEngine(self.portfolio, self.market_data, self.approver, clock=self.clock)
        else:
            executor = ArcExecutionEngine(self.chain, self.wallet, self.approver, self.portfolio, self.idem, LiveSettings(live_trading_enabled=self.s.live_enabled))  # type: ignore[arg-type]
        self.controls = ControlState(**b.controls)
        self.effective_limits = limits
        self.engine = TradingEngine(mode=self.mode_eff, portfolio=self.portfolio, controls=self.controls, pipeline=pipeline, executor=executor,
                                    approver=self.approver, exit_manager=PositionManager(cfg.exit), market_data=self.market_data, bus=self.bus, idempotency=self.idem)

    def recompute(self) -> None:
        """Entries need: control plane says RUNNING, strategy enabled, control plane reachable. Exits never depend on any of it."""
        b = self.bundle
        if b is None or self.engine is None:
            return
        offline = not self.contact_ok()
        self.controls.emergency_stop = b.emergency_stop
        self.controls.global_pause = b.desired_state != "RUNNING" or "traction_momentum" not in b.strategies_enabled or offline or self.revoked
        self.entries_suspended_reason = ("control plane unreachable for more than %ds: no NEW entries until it is back" % self.s.max_offline_s) if offline else None
        self.state = "RUNNING" if not self.controls.global_pause else ("STOPPED" if b.desired_state == "STOPPED" else "PAUSED")

    async def poll_config_once(self, wait: int = 0) -> bool:
        try:
            poll = await self.client.get_config(self.applied_version, wait)
        except Revoked:
            self.revoked = True
            self.recompute()
            raise
        except ControlPlaneError as e:
            self._failed(e)
            self.recompute()
            return False
        self._touch()
        if poll.changed and poll.bundle:
            await self.apply_bundle(poll.bundle)
            return True
        self.recompute()
        return False

    # ------------------------------------------------------------ heartbeat / commands
    def heartbeat(self) -> Heartbeat:
        return Heartbeat(version=__version__, state=self.state, mode=self.bundle.mode if self.bundle else TradingMode.PAPER,  # type: ignore[arg-type]
                         applied_config_version=self.applied_version, strategy_version=getattr(self, "strategy_version", 0), data_source=self.data_kind,
                         data_status=self.data_status, ai={"mode": "ENABLED" if self.llm else "DISABLED", "provider": getattr(self.llm, "name", None), "model": getattr(self.llm, "model", None)},
                         open_positions=self.portfolio.open_count() if self.portfolio else 0, last_activity_at=self.last_activity_at,
                         last_decision=self.last_decision, live=self.live, wallet_provider=self.wallet.name if self.wallet else None,
                         local_ceilings=self.ceilings.model_dump(mode="json"), entries_suspended_reason=self.entries_suspended_reason, last_error=self.last_error)

    async def heartbeat_once(self) -> None:
        await self.live_status()
        try:
            resp = await self.client.heartbeat(self.heartbeat())
        except Revoked:
            self.revoked = True
            self.recompute()
            raise
        except ControlPlaneError as e:
            self._failed(e)
            self.recompute()
            return
        # The successful heartbeat is the proof that the control plane is
        # reachable again. Recompute immediately so the local runtime resumes
        # NEW-entry eligibility without waiting for the next config poll.
        self._touch()
        self.recompute()
        for cmd in resp.commands:
            await self.handle_command(cmd)

    async def handle_command(self, cmd: Command) -> None:
        if self.store.command_seen(cmd.id):
            status, detail = "DONE", "already executed"
        else:
            try:
                if self.engine is None or self.portfolio is None:
                    raise RuntimeError("no active engine (LIVE blocked or not configured)")
                if cmd.type == "CLOSE_POSITION":
                    pid = str(cmd.payload.get("position_id", ""))
                    pos = self.portfolio.positions.get(pid)
                    if pos is None or not pos.is_open:
                        detail = "position already closed"
                    else:
                        await self.engine.monitor_positions(manual_close={pid})
                        detail = "close executed" if not pos.is_open else "close attempted; position still open (will retry on next request)"
                else:
                    await self.engine.monitor_positions(emergency_close=True)
                    detail = f"close-all executed; {self.portfolio.open_count()} still open"
                status = "DONE"
            except Exception as e:  # noqa: BLE001
                status, detail = "FAILED", f"{type(e).__name__}: {e}"
            self.store.mark_command(cmd.id)
        try:
            await self.client.ack_command(cmd.id, status, detail)
        except ControlPlaneError:
            pass  # server keeps it PENDING; we re-ack from the dedupe table next heartbeat

    # ------------------------------------------------------------ events
    async def upload_once(self, max_batches: int = 20) -> int:
        """Drain the outbox in consecutive batches (a long outage must catch up quickly, not 100 events per second)."""
        total = 0
        for _ in range(max_batches):
            rows = self.store.outbox_pending(100)
            if not rows:
                break
            batch = EventBatch(events=[RunnerEvent(seq=seq, **ev) for seq, ev in rows])
            try:
                await self.client.post_events(batch)
            except Revoked:
                self.revoked = True
                raise
            except ControlPlaneError as e:
                self._failed(e)
                return total
            self._touch()
            self.store.outbox_ack(rows[-1][0])  # the server processed (or safely skipped) the whole batch
            total += len(rows)
        return total

    # ------------------------------------------------------------ trading loops
    async def discover_once(self) -> int:
        if self.engine is None or self.controls.global_pause or self.portfolio is None:
            return 0
        n = 0
        try:
            tokens = await self.market_data.discover_tokens()
        except DataUnavailable as e:
            self.data_status = f"unavailable: {e}"
            return 0
        self.data_status = "ok"
        for addr in tokens:
            if self.controls.global_pause:
                break
            if self.portfolio.has_open_position(addr):
                continue
            now = self.clock()
            last = self._evaluated.get(addr.lower())
            if last and (now - last).total_seconds() < self.s.reevaluate_after_s:
                continue
            try:
                m = await self.market_data.get_market_state(addr)
            except DataUnavailable:
                continue
            self._evaluated[addr.lower()] = now
            if self.mono() - self._mkt_at.get(m.key, -1e9) >= self.s.market_snapshot_every_s:
                self._mkt_at[m.key] = self.mono()
                await self.bus.publish(E.MARKET_SNAPSHOT, "", market=m.model_dump(mode="json"))
            rec = await self.engine.handle_market_state(m, now)
            self.last_activity_at = now
            self.last_decision = {"id": rec.id, "token": rec.token_key, "symbol": m.symbol, "action": rec.final_action.value, "reason": rec.final_reason, "at": now.isoformat()}
            n += 1
        return n

    async def monitor_once(self) -> None:
        if self.engine is None or self.portfolio is None:
            return
        await self.engine.monitor_positions(self.clock())
        if self.mono() - self._pf_at >= self.s.snapshot_interval_s:
            self._pf_at = self.mono()
            await self.bus.publish(E.PORTFOLIO_SNAPSHOT, "", portfolio=self.portfolio_summary(), summary=self.portfolio.snapshot(self.clock()))

    # ------------------------------------------------------------ process lifecycle
    async def _loop(self, name: str, fn, interval: float) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except Revoked:
                log.error("runner token revoked: no new entries; existing positions stay protected. Re-pair to reconnect.")
                interval = max(interval, 30)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{name}: {type(e).__name__}: {e}"[:200]
                log.exception("%s loop error", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        cached = self.store.kv_get("config")
        if cached:
            await self.apply_bundle(ConfigBundle(**cached))  # keep protecting positions even if the control plane is down at boot
        s = self.s
        loops = [("config", lambda: self.poll_config_once(s.poll_wait_s), 0.2), ("heartbeat", self.heartbeat_once, s.heartbeat_interval_s),
                 ("upload", self.upload_once, s.upload_interval_s), ("discovery", self.discover_once, s.discovery_interval_s),
                 ("monitor", self.monitor_once, s.monitor_interval_s)]
        self._tasks = [asyncio.create_task(self._loop(n, f, i), name=n) for n, f, i in loops]
        await self._stop.wait()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self.upload_once(), timeout=5)  # best-effort final flush; the outbox persists anyway
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
