"""Shared multi-tenant autonomous cloud worker.

Each tenant gets an isolated RunnerRuntime + durable local store. The control
plane remains the authoritative source for configuration and persisted trading
state; the worker is deliberately stateless across process restarts apart from
its short-lived local cache. A tenant lease prevents duplicate execution when
more than one worker replica is running.
"""
from __future__ import annotations

import asyncio
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
from runner.client import ControlPlaneError, Revoked
from runner.runtime import RunnerRuntime
from runner.settings import RunnerSettings
from runner.store import LocalStore
from runner.wallets import PrivyClient, PrivyWalletProvider

log = logging.getLogger("cloud_worker")


class PlatformClient:
    def __init__(self, server_url: str, token: str, timeout: float = 60.0):
        self.base = server_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.timeout = timeout

    async def _req(self, method: str, path: str, **kw) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            try:
                r = await c.request(method, f"{self.base}{path}", headers=self.headers, **kw)
            except httpx.HTTPError as e:
                raise ControlPlaneError(0, f"unreachable: {type(e).__name__}") from e
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
        # Commands queued via /agent/close/{id}, /agent/close-all, and
        # /wallet/cloud/withdraw are delivered to cloud tenants through the
        # normal heartbeat response (see tenant_heartbeat in routes_platform.py)
        # and acknowledged here the same way the self-hosted runner does.
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

    def _wallet_for(self, privy_wallet_id: str, address: str) -> PrivyWalletProvider:
        if self._privy is None:
            raise RuntimeError("Privy credentials missing on worker")
        return PrivyWalletProvider(self._privy, privy_wallet_id, caip2=self.s.privy_caip2,
                                   chain_id=self.s.network.chain_id, address=address, rpc_urls=self.s.rpc_urls,
                                   allow_execute=self.s.privy_allow_execute, allow_withdraw=self.s.privy_allow_withdraw)

    def _tenant_settings(self, tenant: dict) -> RunnerSettings:
        # No mutable trading state is shared between tenants.
        return self.s.model_copy(update={
            "state_dir": self.state_dir / tenant["user_id"],
            "wallet_provider": "privy",
            "privy_wallet_id": tenant["privy_wallet_id"],
            "privy_wallet_address": tenant["wallet_address"],
        })

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

    async def process_tenant(self, tenant: dict) -> None:
        user_id = tenant["user_id"]
        if not await self.client.acquire_lease(user_id, self.worker_id, self.lease_ttl_s):
            return
        rt: RunnerRuntime | None = None
        try:
            bundle = await self.client.config(user_id)
            s = self._tenant_settings(tenant)
            wallet = self._wallet_for(tenant["privy_wallet_id"], tenant["wallet_address"])
            rt = RunnerRuntime(s, CloudControlPlaneClient(self.client, user_id), LocalStore(s.state_dir / "runtime.sqlite"), wallet=wallet)
            # This is a fresh, ephemeral RunnerRuntime built for exactly one cycle —
            # unlike a long-lived local runner, it has no heartbeat history yet.
            # RunnerRuntime.recompute() treats "no prior contact" as "control plane
            # unreachable" (a dead-man switch, correct for a runner that's been
            # silent for a while), which would otherwise make apply_bundle() below
            # report PAUSED for this cycle even though we are, right now, mid
            # conversation with the control plane (we just fetched `bundle` from
            # it). Seed contact here so recompute() reflects the real desired
            # state instead of momentarily reporting PAUSED before the first
            # heartbeat gets a chance to correct it.
            # Fresh ephemeral runtime has no heartbeat history. Seed contact so
            # recompute() does not treat this cycle as "control plane offline".
            rt.last_contact = rt.mono()
            await self._restore_state(rt, user_id, bundle.mode)
            await rt.apply_bundle(bundle)
            rt.recompute()

            if rt.engine is None or rt.controls.global_pause or rt.state == "LIVE_BLOCKED":
                # Still heartbeat so UI sees STOPPED/PAUSED + reasons, not stale idle.
                await rt.heartbeat_once()
                await rt.upload_once()
                await self._persist_state(rt, user_id)
                log.info(
                    "tenant %s skipped trade cycle state=%s global_pause=%s desired=%s strategies=%s data_status=%s",
                    user_id,
                    rt.state,
                    rt.controls.global_pause if rt.controls else None,
                    getattr(bundle, "desired_state", None),
                    getattr(bundle, "strategies_enabled", None),
                    rt.data_status,
                )
                return

            # Trade cycle FIRST so data_status / last_activity are real before heartbeat.
            await rt.monitor_once()
            if not rt.controls.global_pause:
                await rt.discover_once()
            await rt.monitor_once()

            # Heartbeat AFTER discover so control plane stores RUNNING + real data_status
            # instead of permanent "idle" (fresh runtime starts as idle every cycle).
            await rt.heartbeat_once()
            await rt.upload_once()
            await self._persist_state(rt, user_id)
            log.info(
                "tenant %s cycle complete mode=%s positions=%s state=%s data_status=%s",
                user_id,
                bundle.mode.value,
                rt.portfolio.open_count() if rt.portfolio else 0,
                rt.state,
                rt.data_status,
            )
        except Revoked:
            log.error("platform worker authorization revoked while processing %s", user_id)
        except Exception:
            log.exception("tenant %s cycle failed", user_id)
            if rt is not None:
                try:
                    await rt.upload_once()
                except Exception:
                    log.exception("tenant %s outbox flush failed", user_id)
        finally:
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

    async def run_forever(self, poll_s: float | None = None) -> None:
        # Default 20s: public GeckoTerminal allows ~30 req/min; a 5s poll + multi
        # provider fan-out trips 429 circuits and leaves discovery empty.
        if poll_s is None:
            poll_s = float(os.environ.get("ARC_RUNNER_CLOUD_POLL_S", "20"))
        poll_s = max(5.0, float(poll_s))
        log.info("cloud worker %s starting against %s max_concurrent=%s poll_s=%s", self.worker_id, self.s.server_url, self.max_concurrent, poll_s)
        sem = asyncio.Semaphore(self.max_concurrent)
        while True:
            started = time.monotonic()
            try:
                tenants = await self.client.active_tenants()
                async def one(t: dict) -> None:
                    async with sem:
                        await self.process_tenant(t)
                await asyncio.gather(*(one(t) for t in tenants), return_exceptions=False)
                log.info("processed %d active tenants", len(tenants))
            except Revoked:
                raise
            except Exception:
                log.exception("worker loop error")
            await asyncio.sleep(max(1.0, poll_s - (time.monotonic() - started)))


async def run_cloud_worker(settings: RunnerSettings | None = None) -> None:
    s = settings or RunnerSettings()
    token = os.environ.get("PLATFORM_WORKER_TOKEN") or os.environ.get("ARC_RUNNER_PLATFORM_WORKER_TOKEN") or ""
    if not token:
        raise SystemExit("PLATFORM_WORKER_TOKEN is required for cloud worker mode")
    if not s.server_url:
        raise SystemExit("ARC_RUNNER_SERVER_URL is required")
    await CloudWorker(s, token).run_forever()
