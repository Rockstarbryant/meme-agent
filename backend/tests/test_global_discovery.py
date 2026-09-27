"""Tests for global launch discovery, registry, monitoring updates, and decoupling."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus
from app.discovery.service import GlobalDiscoveryService, DiscoveryConfig
from app.discovery.monitoring import GlobalMonitoringService, MonitoringConfig
from app.discovery.store import token_to_dict, dict_to_token


def test_token_deduplication():
    reg = GlobalTokenRegistry()
    t1 = LaunchpadToken(chain="arc", token_address="0xABC", discovered_at=datetime.now(timezone.utc), launchpad="testpad")
    t2 = LaunchpadToken(chain="arc", token_address="0xabc", discovered_at=datetime.now(timezone.utc), symbol="FOO", status=TokenStatus.WATCHING)
    reg.upsert(t1)
    reg.upsert(t2)
    assert len(reg.all()) == 1
    assert reg.all()[0].symbol == "FOO"
    assert reg.all()[0].launchpad == "testpad"


def test_unique_key():
    t = LaunchpadToken(chain="arc", token_address="0xAbC", discovered_at=datetime.now(timezone.utc))
    assert t.token_key == "arc:0xabc"


def test_token_dict_roundtrip():
    now = datetime.now(timezone.utc)
    t = LaunchpadToken(
        chain="arc", token_address="0x1", discovered_at=now, status=TokenStatus.WATCHING,
        current_score=55.0, meta={"last_snapshot": {"price": 1.2, "liquidity": 9000}},
    )
    d = token_to_dict(t)
    t2 = dict_to_token(d)
    assert t2.token_key == t.token_key
    assert t2.current_score == 55.0
    assert t2.meta["last_snapshot"]["price"] == 1.2


@pytest.mark.asyncio
async def test_discovery_idempotent_and_checkpoint():
    reg = GlobalTokenRegistry()
    async def discover_fn(start, end):
        return [
            {"chain": "arc", "token_address": "0x1", "launchpad": "pad1"},
            {"chain": "arc", "token_address": "0x1", "launchpad": "pad1"},
            {"chain": "arc", "token_address": "0x2", "launchpad": "pad2"},
        ]
    svc = GlobalDiscoveryService(reg, discover_fn=discover_fn, config=DiscoveryConfig(interval_s=10, lookback_s=5))
    n = await svc.run_once()
    assert n == 3
    assert len(reg.all()) == 2
    assert svc.checkpoint.last_success_at is not None
    first = svc.checkpoint.last_success_at
    await svc.run_once()
    assert len(reg.all()) == 2
    assert svc.checkpoint.last_success_at >= first


@pytest.mark.asyncio
async def test_discovery_does_not_advance_checkpoint_on_failure():
    reg = GlobalTokenRegistry()
    async def fail_fn(start, end):
        raise RuntimeError("provider down")
    svc = GlobalDiscoveryService(reg, discover_fn=fail_fn)
    with pytest.raises(RuntimeError):
        await svc.run_once()
    assert svc.checkpoint.last_success_at is None
    assert svc.checkpoint.error is not None


@pytest.mark.asyncio
async def test_monitoring_updates_snapshot_and_score():
    """Market data must be refreshed by monitoring — not stuck at discovery."""
    reg = GlobalTokenRegistry()
    now = datetime.now(timezone.utc)
    t = LaunchpadToken(
        chain="arc", token_address="0xhot", discovered_at=now,
        status=TokenStatus.WATCHING, priority="HOT", current_score=50.0,
        meta={"last_snapshot": {"price": 1.0, "market_cap": 1000, "liquidity": 5000, "holder_count": 10}},
    )
    reg.upsert(t)
    calls = {"n": 0}

    async def fetch(tok):
        calls["n"] += 1
        return {
            "price": 2.5, "market_cap": 50000, "liquidity": 25000,
            "holder_count": 120, "volume_5m": 8000,
        }

    async def score(tok, snap):
        assert snap["price"] == 2.5
        assert snap["holder_count"] == 120
        return 72.0

    mon = GlobalMonitoringService(
        reg, fetch_state_fn=fetch, score_fn=score,
        config=MonitoringConfig(score_improve_threshold=10.0, hot_interval_s=0),
    )
    n = await mon.run_once()
    assert n == 1
    assert calls["n"] == 1
    updated = reg.get("arc", "0xhot")
    assert updated is not None
    assert updated.meta["last_snapshot"]["price"] == 2.5
    assert updated.meta["last_snapshot"]["liquidity"] == 25000
    assert updated.meta["last_snapshot"]["holder_count"] == 120
    assert updated.current_score == 72.0
    assert updated.score_delta == 22.0
    assert updated.status == TokenStatus.IMPROVING
    assert updated.last_monitored_at is not None


@pytest.mark.asyncio
async def test_monitoring_decline_downgrades():
    reg = GlobalTokenRegistry()
    now = datetime.now(timezone.utc)
    reg.upsert(LaunchpadToken(
        chain="arc", token_address="0xcold", discovered_at=now,
        status=TokenStatus.WATCHING, priority="WARM", current_score=60.0,
    ))
    async def fetch(tok):
        return {"price": 0.1}
    async def score(tok, snap):
        return 30.0  # -30 decline
    mon = GlobalMonitoringService(
        reg, fetch_state_fn=fetch, score_fn=score,
        config=MonitoringConfig(score_decline_threshold=-15.0, warm_interval_s=0),
    )
    await mon.run_once()
    u = reg.get("arc", "0xcold")
    assert u.status == TokenStatus.LOST_MOMENTUM
    assert u.priority == "COLD"


def test_list_recent_24h_72h():
    reg = GlobalTokenRegistry()
    now = datetime.now(timezone.utc)
    reg.upsert(LaunchpadToken(chain="arc", token_address="0x1", discovered_at=now - timedelta(hours=1)))
    reg.upsert(LaunchpadToken(chain="arc", token_address="0x2", discovered_at=now - timedelta(hours=30)))
    reg.upsert(LaunchpadToken(chain="arc", token_address="0x3", discovered_at=now - timedelta(hours=80)))
    assert len(reg.list_recent(now - timedelta(hours=24))) == 1
    assert len(reg.list_recent(now - timedelta(hours=72))) == 2


@pytest.mark.asyncio
async def test_discovery_not_tied_to_trade_cycle_contract():
    """Architectural contract: discovery service runs independently."""
    reg = GlobalTokenRegistry()
    ran = {"discovery": False}
    async def discover_fn(start, end):
        ran["discovery"] = True
        return []
    svc = GlobalDiscoveryService(reg, discover_fn=discover_fn, config=DiscoveryConfig(interval_s=99999))
    await svc.run_once()
    assert ran["discovery"] is True
    # Trade cycle code paths must not call this — verified by absence of
    # discover_once in cloud_worker process_tenant (see cloud_worker.py).
