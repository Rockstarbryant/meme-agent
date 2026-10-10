"""Shared multi-tenant autonomous cloud worker.

Each tenant gets an isolated RunnerRuntime + durable local store. The control
plane remains the authoritative source for configuration and persisted trading
state; the worker is deliberately stateless across process restarts apart from
its short-lived local cache. A tenant lease prevents duplicate execution when
more than one worker replica is running.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.types import TradingMode
from app.domain.runner_protocol import (
    ConfigBundle, ConfigPoll, EventAck, EventBatch, Heartbeat, HeartbeatResponse, LiveStatus, RunnerEvent,
)
from app.events.bus import IdempotencyStore
from app.risk.per_user_policy import PerUserPolicyEnforcer
from runner import __version__
from runner.client import ControlPlaneError, Revoked
from runner.runtime import RunnerRuntime
from runner.settings import RunnerSettings
from runner.store import LocalStore
from runner.wallets import PrivyClient, PrivyWalletProvider
from app.discovery.bootstrap import GlobalPipeline
from app.discovery.ranking import RankingConfig, rank_candidates
from app.core.clock import utcnow

log = logging.getLogger("cloud_worker")


class PlatformClient:
    def __init__(self, server_url: str, token: str, timeout: float = 60.0):
        self.base = server_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.timeout = timeout

    async def _req(self, method: str, path: str, **kw) -> Any:
        r = None
        last: Exception | None = None
        # ConnectError means no request was sent (DNS blip, refused connection), so retrying is always safe.
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                try:
                    r = await c.request(method, f"{self.base}{path}", headers=self.headers, **kw)
                    break
                except httpx.ConnectError as e:
                    last = e
                except httpx.HTTPError as e:
                    raise ControlPlaneError(0, f"unreachable: {type(e).__name__}") from e
            await asyncio.sleep(0.5 * (attempt + 1))
        if r is None:
            raise ControlPlaneError(0, f"unreachable: {type(last).__name__}") from last
        if r.status_code == 401:
            raise Revoked(401, "platform worker token rejected")
        if r.status_code >= 400:
            raise ControlPlaneError(r.status_code, r.text[:500])
        return r.json() if r.content else {}

    async def active_tenants(self) -> list[dict]:
        return await self._req("GET", "/platform/tenants/active")

    async def config(self, user_id: str) -> ConfigBundle:
        return ConfigBundle.model_validate(await self._req("GET", f"/platform/tenants/{user_id}/config"))

    async def get_config(self, user_id: str, since: int) -> ConfigPoll:
        b = await self.config(user_id)
        return ConfigPoll(changed=b.version > since, bundle=b if b.version > since else None)

    async def heartbeat(self, user_id: str, hb: Heartbeat) -> HeartbeatResponse:
        body = await self._req("POST", f"/platform/tenants/{user_id}/heartbeat", json=hb.model_dump(mode="json"))
        from datetime import datetime, timezone
        return HeartbeatResponse(server_time=datetime.fromisoformat(body["server_time"]) if body.get("server_time") else datetime.now(timezone.utc),
                                 desired_config_version=int(body.get("desired_config_version", hb.applied_config_version)),
                                 commands=body.get("commands", []))

    async def post_events(self, user_id: str, batch: EventBatch) -> EventAck:
        return EventAck(**await self._req("POST", f"/platform/tenants/{user_id}/events", json=batch.model_dump(mode="json")))

    async def ack_command(self, user_id: str, cid: str, status: str, detail: str = "") -> None:
        await self._req("POST", f"/platform/tenants/{user_id}/commands/{cid}/ack", json={"status": status, "detail": detail})

    async def get_state(self, user_id: str, mode: str) -> dict:
        return await self._req("GET", f"/platform/tenants/{user_id}/state", params={"mode": mode})

    async def save_state(self, user_id: str, mode: str, portfolio: dict) -> dict:
        return await self._req("POST", f"/platform/tenants/{user_id}/state", json={"mode": mode, "portfolio": portfolio})

    async def acquire_lease(self, user_id: str, worker_id: str, ttl_s: int) -> bool:
        body = await self._req("POST", f"/platform/tenants/{user_id}/lease", json={"worker_id": worker_id, "ttl_s": ttl_s})
        return bool(body.get("acquired"))

    async def release_lease(self, user_id: str, worker_id: str) -> None:
        try:
            await self._req("DELETE", f"/platform/tenants/{user_id}/lease", params={"worker_id": worker_id})
        except Exception:
            log.exception("failed releasing lease for %s", user_id)


class CloudControlPlaneClient:
    """RunnerRuntime-compatible adapter for the shared worker."""
    def __init__(self, platform: PlatformClient, user_id: str):
        self.platform, self.user_id = platform, user_id

    async def get_config(self, since: int, wait: int) -> ConfigPoll:
        return await self.platform.get_config(self.user_id, since)

    async def heartbeat(self, hb: Heartbeat) -> HeartbeatResponse:
        return await self.platform.heartbeat(self.user_id, hb)

    async def post_events(self, batch: EventBatch) -> EventAck:
        return await self.platform.post_events(self.user_id, batch)

    async def ack_command(self, cid: str, status: str, detail: str = "") -> None:
        await self.platform.ack_command(self.user_id, cid, status, detail)


class CloudWorker:
    def __init__(self, settings: RunnerSettings, platform_token: str):
        self.s = settings
        self.client = PlatformClient(settings.server_url, platform_token)
        self.worker_id = os.environ.get("ARC_RUNNER_WORKER_ID") or f"nf-{secrets.token_hex(8)}"
        self.max_concurrent = max(1, int(os.environ.get("ARC_RUNNER_MAX_CONCURRENT_TENANTS", "5")))
        self.lease_ttl_s = max(30, int(os.environ.get("ARC_RUNNER_TENANT_LEASE_TTL_S", "45")))
        self.state_dir = Path(settings.state_dir) / "cloud-tenants"
        self._privy: PrivyClient | None = None
        if settings.privy_app_id and settings.privy_app_secret:
            self._privy = PrivyClient(settings.privy_app_id, settings.privy_app_secret.get_secret_value(), base_url=settings.privy_api_url)
        # Last heartbeat produced by each tenant's real cycle. Replayed during
        # the sleep between cycles so the control plane's "runner online"
        # indicator does not flap when a cycle takes longer than the server's
        # heartbeat-timeout window (the market-data fan-out can easily run
        # 60-90s per cycle, vs. a typical 30-60s timeout).
        self._last_heartbeats: dict[str, Heartbeat] = {}
        # Per-tenant "when did we last evaluate token X". RunnerRuntime is rebuilt every cycle, so its own
        # ``_evaluated`` dict is always empty; without this cache every cycle re-evaluated the same top-N tokens
        # and never reached the rest.
        self._eval_at: dict[str, dict[str, Any]] = {}
        self._pipeline: GlobalPipeline | None = None
        # Live view of every tenant, refreshed by the heartbeat loop independently of trade cycles.
        self._tenants: dict[str, dict] = {}
        self._live: dict[str, RunnerRuntime] = {}          # runtimes that are mid-cycle
        self._busy: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._next_cycle: dict[str, float] = {}
        self._wake: set[str] = set()                       # a command arrived for an idle tenant: run a cycle now
        self._applied: dict[str, tuple] = {}               # (desired_state, emergency_stop, config_version) last applied
        self._ai_info: dict = {}
        self._lease_at: dict[str, float] = {}
        self._sem: asyncio.Semaphore | None = None
        self.cycle_interval_s = 30.0

    def _wallet_for(self, privy_wallet_id: str, address: str) -> PrivyWalletProvider:
        if self._privy is None:
            raise RuntimeError("Privy credentials missing on worker")
        return PrivyWalletProvider(self._privy, privy_wallet_id, caip2=self.s.privy_caip2,
                                   chain_id=self.s.network.chain_id, address=address, rpc_urls=self.s.rpc_urls,
                                   allow_execute=self.s.privy_allow_execute, allow_withdraw=self.s.privy_allow_withdraw)

    def _tenant_settings(self, tenant: dict) -> RunnerSettings:
        has_wallet = bool(tenant.get("privy_wallet_id") and tenant.get("wallet_address"))
        update: dict[str, Any] = {"state_dir": self.state_dir / tenant["user_id"]}
        if has_wallet:
            update.update({"wallet_provider": "privy", "privy_wallet_id": tenant["privy_wallet_id"],
                           "privy_wallet_address": tenant["wallet_address"]})
        else:
            update.update({"wallet_provider": "none"})   # PAPER account without a wallet
        return self.s.model_copy(update=update)

    async def _restore_state(self, rt: RunnerRuntime, user_id: str, mode: TradingMode) -> None:
        remote = await self.client.get_state(user_id, mode.value)
        portfolio = remote.get("portfolio")
        if portfolio:
            rt.store.kv_set(f"portfolio:{user_id}:{mode.value}", portfolio)
        for item in remote.get("active_idempotency", []):
            key = str(item.get("key") or "")
            if key:
                rt.store.idem_seed(key, {"remote_status": item.get("status")})
                rt.store.idem_seed(f"live:{key}", {"remote_status": item.get("status")})

    async def _persist_state(self, rt: RunnerRuntime, user_id: str) -> None:
        if rt.portfolio is not None:
            await self.client.save_state(user_id, rt.portfolio.mode.value, rt.portfolio.to_dict())

    def _ranking_config(self) -> RankingConfig:
        return RankingConfig(
            min_liquidity_usdc=float(self.s.candidate_min_liquidity_usdc),
            min_market_cap_usdc=float(self.s.candidate_min_market_cap_usdc),
            max_age_s=float(self.s.retention_hours) * 3600.0,
            eval_min_interval_s=float(self.s.eval_min_interval_s),
            max_per_cycle=int(self.s.eval_max_per_cycle),
        )

    def _global_candidate_addresses(self, user_id: str) -> list[str]:
        """Pick which registry tokens to evaluate this cycle for one tenant.

        Balanced (liquidity / market cap / holders) and trending tokens rank first, but every eligible token is
        rotated through: tokens not evaluated within ``eval_min_interval_s`` get a staleness bonus, tokens
        evaluated more recently are skipped, and nothing older than the retention window is considered.
        """
        pipe = self._pipeline
        if pipe is None or getattr(pipe, "registry", None) is None:
            return []
        eval_at = self._eval_at.setdefault(user_id, {})
        tokens = pipe.registry.all()
        addrs = rank_candidates(tokens, eval_at, utcnow(), self._ranking_config())
        log.info("global candidates for evaluation: %d due of %d registry tokens (top=%s)",
                 len(addrs), len(tokens), addrs[:3])
        return addrs

    async def process_tenant(self, tenant: dict) -> None:
        user_id = tenant["user_id"]
        if not await self.client.acquire_lease(user_id, self.worker_id, self.lease_ttl_s):
            return
        rt: RunnerRuntime | None = None
        try:
            bundle = await self.client.config(user_id)
            s = self._tenant_settings(tenant)
            wallet = (self._wallet_for(tenant["privy_wallet_id"], tenant["wallet_address"])
                      if tenant.get("privy_wallet_id") and tenant.get("wallet_address") else None)
            rt = RunnerRuntime(s, CloudControlPlaneClient(self.client, user_id), LocalStore(s.state_dir / "runtime.sqlite"), wallet=wallet)
            # Fresh ephemeral runtime has no heartbeat history; seed contact so
            # recompute() does not treat this cycle as "control plane offline".
            rt.last_contact = rt.mono()
            await self._restore_state(rt, user_id, bundle.mode)
            await rt.apply_bundle(bundle)
            rt.recompute()
            self._applied[user_id] = (bundle.desired_state, bool(bundle.emergency_stop), bundle.version)
            # The tenant list is newer than the bundle we just fetched when the user pressed something in between.
            latest = self._tenants.get(user_id) or tenant
            rt.apply_live_state(str(latest.get("desired_state") or bundle.desired_state), bool(latest.get("emergency_stop")))
            self._live[user_id] = rt   # from now on the heartbeat loop reports THIS runtime and applies live changes

            if rt.engine is not None and rt.controls.global_pause and rt.portfolio is not None and rt.portfolio.open_count():
                # Pause / Stop halt NEW entries only: open positions must keep their stop-loss / take-profit
                # protection. (This branch used to return before monitoring, so paused accounts were unprotected.)
                with contextlib.suppress(Exception):
                    await rt.drain_commands()
                    await rt.monitor_once()

            if rt.engine is None or rt.controls.global_pause or rt.state == "LIVE_BLOCKED":
                await rt.heartbeat_once()
                await rt.upload_once()
                await self._persist_state(rt, user_id)
                # Cache the freshly-built heartbeat for replay during the sleep.
                try:
                    self._last_heartbeats[user_id] = rt.heartbeat()
                except Exception:
                    pass
                log.info(
                    "tenant %s skipped trade cycle state=%s global_pause=%s desired=%s strategies=%s data_status=%s",
                    user_id, rt.state,
                    rt.controls.global_pause if rt.controls else None,
                    getattr(bundle, "desired_state", None),
                    getattr(bundle, "strategies_enabled", None),
                    rt.data_status,
                )
                return

            # Global discovery fills the shared registry; this cycle evaluates
            # those candidates into Decisions so Opportunities mirrors Discovery.
            cycle_started = time.monotonic()
            deadline = rt.mono() + float(self.s.eval_time_budget_s)
            evaluated = 0
            try:
                await rt.drain_commands()
                await rt.monitor_once()
                addrs = self._global_candidate_addresses(user_id)
                if not addrs:
                    log.warning(
                        "tenant %s: no global candidates (pipeline=%s registry=%s) — Opportunities will not refresh",
                        user_id,
                        self._pipeline is not None,
                        len(list(self._pipeline.registry.all())) if self._pipeline is not None else 0,
                    )
                else:
                    # Share the global market-data stack (same cache as discovery/monitoring) for BOTH evaluation
                    # and open-position monitoring; the per-cycle runtime's own providers have an empty cache.
                    pipe = self._pipeline
                    if pipe is not None and getattr(pipe, "market_data", None) is not None:
                        rt.use_shared_market_data(pipe.market_data)   # never closed by rt.aclose()
                    # Persist the evaluation throttle across cycles (see _eval_at above).
                    rt._evaluated = self._eval_at.setdefault(user_id, {})
                    state_fn = None
                    if pipe is not None:
                        pipe.begin_cycle(int(self.s.eval_fetch_budget))
                        state_fn = pipe.market_state_for
                    evaluated = await rt.evaluate_global_candidates(
                        addrs, max_n=int(self.s.eval_max_per_cycle),
                        state_fn=state_fn, min_interval_s=float(self.s.eval_min_interval_s), deadline=deadline,
                    )
                await rt.drain_commands()
                await rt.monitor_once()
            finally:
                pass
            await rt.heartbeat_once()
            await rt.upload_once()
            await self._persist_state(rt, user_id)
            try:
                self._last_heartbeats[user_id] = rt.heartbeat()
                self._ai_info = dict(self._last_heartbeats[user_id].ai or self._ai_info)
            except Exception:
                pass
            log.info(
                "tenant %s cycle complete mode=%s positions=%s state=%s data_status=%s evaluated=%d cycle_s=%.1f",
                user_id, bundle.mode.value,
                rt.portfolio.open_count() if rt.portfolio else 0,
                rt.state, rt.data_status,
                evaluated,
                time.monotonic() - cycle_started,
            )
        except Revoked:
            log.error("platform worker authorization revoked while processing %s", user_id)
        except Exception:
            log.exception("tenant %s cycle failed", user_id)
            if rt is not None:
                # A failed cycle must not leave an older (possibly PAUSED) heartbeat cached for replay.
                try:
                    self._last_heartbeats[user_id] = rt.heartbeat()
                except Exception:
                    pass
                try:
                    await rt.upload_once()
                except Exception:
                    log.exception("tenant %s outbox flush failed", user_id)
        finally:
            self._live.pop(user_id, None)
            if rt is not None:
                try:
                    await self._persist_state(rt, user_id)
                except Exception:
                    log.exception("tenant %s state persistence failed", user_id)
                try:
                    await rt.aclose()
                except Exception:
                    log.exception("tenant %s runtime close failed", user_id)
                try:
                    rt.store.close()
                except Exception:
                    pass
            await self.client.release_lease(user_id, self.worker_id)

    @staticmethod
    def _state_for(tenant: dict) -> str:
        d = str(tenant.get("desired_state") or "STOPPED")
        if tenant.get("emergency_stop") and d == "RUNNING":
            return "PAUSED"
        return d if d in ("RUNNING", "PAUSED", "STOPPED") else "STOPPED"

    def _idle_heartbeat(self, tenant: dict) -> Heartbeat:
        """Heartbeat for a tenant with no cycle in progress: the last real one, restated with the CURRENT state.
        A tenant that never ran a cycle yet (fresh worker, new account) gets a minimal one, so it never shows
        OFFLINE just because the worker has not reached it."""
        uid = tenant["user_id"]
        cached = self._last_heartbeats.get(uid)
        if cached is not None:
            return self._with_current_state(cached, tenant)
        return Heartbeat(version=__version__, state=self._state_for(tenant), mode=TradingMode(str(tenant.get("mode") or "PAPER")),
                         applied_config_version=int((self._applied.get(uid) or (0, 0, 0))[2]), data_source="cloud worker",
                         data_status="idle", ai=dict(self._ai_info))

    async def _beat(self, tenant: dict) -> None:
        """One heartbeat for one tenant, from the heartbeat loop (never from inside a cycle)."""
        uid = tenant["user_id"]
        rt = self._live.get(uid)
        try:
            if rt is not None:
                rt.apply_live_state(str(tenant.get("desired_state") or ""), bool(tenant.get("emergency_stop")))
                hb = rt.heartbeat()
                # A cycle can outlast the tenant lease; keep it so a second worker replica does not take over.
                if time.monotonic() - self._lease_at.get(uid, 0.0) > self.lease_ttl_s / 3:
                    self._lease_at[uid] = time.monotonic()
                    with contextlib.suppress(Exception):
                        await self.client.acquire_lease(uid, self.worker_id, self.lease_ttl_s)
            else:
                hb = self._idle_heartbeat(tenant)
            resp = await self.client.heartbeat(uid, hb)
        except Revoked:
            raise
        except Exception:
            log.debug("heartbeat failed for %s", uid)
            return
        if resp.commands:
            if rt is not None:
                rt.enqueue_commands(resp.commands)       # runs between tokens of the cycle in progress
            elif uid not in self._busy:
                self._wake.add(uid)                      # idle tenant: run a cycle now so the command is handled

    def _needs_cycle_now(self, tenant: dict) -> bool:
        uid = tenant["user_id"]
        if uid in self._wake:
            return True
        applied = self._applied.get(uid)
        want = (str(tenant.get("desired_state") or ""), bool(tenant.get("emergency_stop")), int(tenant.get("config_version") or 0))
        return applied is None or applied != want

    async def _run_cycle(self, tenant: dict) -> None:
        uid = tenant["user_id"]
        self._busy.add(uid)
        self._wake.discard(uid)
        try:
            assert self._sem is not None
            async with self._sem:
                await self.process_tenant(tenant)
        except Revoked:
            log.error("platform worker authorization revoked while processing %s", uid)
        except Exception:
            log.exception("tenant %s cycle task failed", uid)
        finally:
            self._busy.discard(uid)
            self._live.pop(uid, None)
            self._next_cycle[uid] = time.monotonic() + self.cycle_interval_s

    @staticmethod
    def _with_current_state(hb: Heartbeat, tenant: dict) -> Heartbeat:
        """Make a replayed heartbeat agree with the control plane's CURRENT desired state.

        The cached heartbeat was produced at the end of an earlier cycle. If the user pressed Start/Pause since
        then, replaying it verbatim reports the OLD state ("Requested: RUNNING, Reported: PAUSED") until the next
        full cycle, and a cycle that fails before refreshing the cache keeps reporting it indefinitely, which shows
        up as the agent flipping between PAUSED and RUNNING. Only the plain running/paused/stopped states are
        rewritten; LIVE_BLOCKED and similar states are left alone.
        """
        if hb.state not in ("RUNNING", "PAUSED", "STOPPED"):
            return hb
        desired = str(tenant.get("desired_state") or "")
        if tenant.get("emergency_stop") or desired == "PAUSED":
            want = "PAUSED"
        elif desired == "STOPPED":
            want = "STOPPED"
        elif desired == "RUNNING":
            want = "RUNNING"
        else:
            return hb
        return hb if want == hb.state else hb.model_copy(update={"state": want})

    async def run_forever(self, poll_s: float | None = None) -> None:
        # ``poll_s`` is the *full trade cycle* interval, not the outer loop
        # interval. Heartbeats go out every ``heartbeat_s`` seconds so the
        # control plane never sees the runner as offline between cycles.
        if poll_s is None:
            poll_s = float(os.environ.get("ARC_RUNNER_CLOUD_POLL_S", "30"))
        poll_s = max(10.0, float(poll_s))
        heartbeat_s = max(4.0, float(os.environ.get("ARC_RUNNER_CLOUD_HEARTBEAT_S", "8")))
        log.info("cloud worker %s starting against %s max_concurrent=%s poll_s=%s heartbeat_s=%s",
                 self.worker_id, self.s.server_url, self.max_concurrent, poll_s, heartbeat_s)
        sem = asyncio.Semaphore(self.max_concurrent)
        last_full_cycle = 0.0

        # GLOBAL discovery & monitoring — independent of tenant trade cycles.
        # Shared market-data gateway + Postgres/Redis persistence so price/mcap/
        # liquidity/holders are refreshed by monitoring, not frozen at discovery.
        redis = None
        session_factory = None
        try:
            from redis.asyncio import Redis
            redis = Redis.from_url(getattr(self.s, "redis_url", "redis://localhost:6379/0"), decode_responses=True)
            await redis.ping()
        except Exception:
            log.warning("cloud worker: Redis unavailable for global discovery cache; continuing in-memory")
            redis = None
        db_url = getattr(self.s, "database_url", None) or __import__("os").environ.get("DATABASE_URL")
        if db_url:
            try:
                from app.db.session import make_engine, make_session_factory
                # normalize like control plane
                import re
                db_url = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", db_url).replace("sslmode=", "ssl=")
                engine = make_engine(db_url)
                session_factory = make_session_factory(engine)
            except Exception:
                log.exception("cloud worker: could not open database for global registry")
                session_factory = None

        self._pipeline = GlobalPipeline(self.s, redis=redis, session_factory=session_factory)
        await self._pipeline.start()
        audit_task = None
        if session_factory is not None:
            # Operational audit trail: this worker has database access, so persist directly (all tenants + system records).
            from app.observability.audit import RECORDER
            from app.services import audit_store
            RECORDER.add_sink(audit_store.make_db_sink(session_factory))

            async def _audit_loop() -> None:
                while True:
                    await asyncio.sleep(3.0)
                    try:
                        await RECORDER.flush()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.debug("audit flush failed", exc_info=True)

            audit_task = asyncio.create_task(_audit_loop(), name="audit-flush")
        stop = asyncio.Event()
        disc_task = asyncio.create_task(self._pipeline.discovery.scheduler_loop(stop), name="global-discovery")
        mon_task = asyncio.create_task(self._pipeline.monitoring.scheduler_loop(stop), name="global-monitoring")
        log.info(
            "global discovery interval=%.0fs + monitoring started (not per-tenant); registry_size=%d",
            self._pipeline.discovery.config.interval_s,
            len(self._pipeline.registry.all()),
        )

        self.cycle_interval_s = poll_s
        self._sem = sem
        last_prune = time.monotonic()
        try:
            while True:
                loop_started = time.monotonic()
                try:
                    tenants = await self.client.active_tenants()
                    now = time.monotonic()
                    current = {t["user_id"] for t in tenants if t.get("user_id")}
                    for uid in list(self._tenants):
                        if uid not in current:                 # account switched runner / deactivated
                            self._tenants.pop(uid, None)
                            self._last_heartbeats.pop(uid, None)
                            self._applied.pop(uid, None)
                    for t in tenants:
                        if t.get("user_id"):
                            self._tenants[t["user_id"]] = t
                    # 1) heartbeats for EVERY tenant, every tick, whatever its cycle is doing (this is what keeps the
                    #    runner ONLINE and the reported state equal to the user's command).
                    await asyncio.gather(*(self._beat(t) for t in tenants), return_exceptions=True)
                    # 2) cycles run as independent tasks: a slow tenant no longer blocks the others or the heartbeats.
                    for t in tenants:
                        uid = t.get("user_id")
                        if not uid or uid in self._busy:
                            continue
                        if now >= self._next_cycle.get(uid, 0.0) or self._needs_cycle_now(t):
                            self._tasks[uid] = asyncio.create_task(self._run_cycle(t), name=f"tenant-{uid[:8]}")
                    # 3) housekeeping, independent of cycles
                    if now - last_prune > 600 and self._pipeline is not None:
                        last_prune = now
                        try:
                            await self._pipeline.prune()
                            for cache in self._eval_at.values():
                                for k in [k for k, _ in cache.items() if self._pipeline.registry.get("arc", k) is None]:
                                    cache.pop(k, None)
                        except Exception:
                            log.exception("retention prune failed")
                except Revoked:
                    raise
                except Exception:
                    log.exception("worker loop error")
                elapsed = time.monotonic() - loop_started
                await asyncio.sleep(max(1.0, heartbeat_s - elapsed))
        finally:
            stop.set()
            for task in list(self._tasks.values()):
                task.cancel()
            disc_task.cancel()
            mon_task.cancel()
            if audit_task is not None:
                audit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await disc_task
                await mon_task
            for task in list(self._tasks.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


async def run_cloud_worker(settings: RunnerSettings | None = None) -> None:
    s = settings or RunnerSettings()
    token = os.environ.get("PLATFORM_WORKER_TOKEN") or os.environ.get("ARC_RUNNER_PLATFORM_WORKER_TOKEN") or ""
    if not token:
        raise SystemExit("PLATFORM_WORKER_TOKEN is required for cloud worker mode")
    if not s.server_url:
        raise SystemExit("ARC_RUNNER_SERVER_URL is required")
    await CloudWorker(s, token).run_forever()