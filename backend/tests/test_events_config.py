import pytest

from app.config import Settings
from app.events.bus import EventBus, EventType as E, InMemoryAuditSink, InMemoryIdempotencyStore


async def test_audit_is_written_even_if_subscriber_crashes():
    sink = InMemoryAuditSink()
    bus = EventBus([sink])

    async def bad(_):
        raise RuntimeError("boom")
    bus.subscribe(bad)
    await bus.publish(E.RISK_ALERT, "c1", kind="x")
    assert len(sink.for_correlation("c1")) == 1


async def test_idempotency_claim_release_complete():
    s = InMemoryIdempotencyStore()
    assert await s.claim("a") and not await s.claim("a")
    await s.release("a")
    assert await s.claim("a")
    await s.complete("a", {"ok": 1})
    assert (await s.get("a")) == {"ok": 1}


def test_live_is_off_by_default_and_prod_needs_strong_secrets(monkeypatch):
    s = Settings(_env_file=None)
    assert s.live_trading_enabled is False and s.paper_trading_enabled is True and s.arc_chain_id == 5042
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_env="production", secret_key="short", jwt_secret="short")
    assert Settings(_env_file=None, arc_rpc_url="http://a", arc_rpc_fallback_urls="http://b, http://c").arc_rpc_urls == ["http://a", "http://b", "http://c"]


def test_database_url_is_normalised_for_asyncpg():
    assert Settings(_env_file=None, database_url="postgres://u:p@h:5432/d?sslmode=require").database_url == "postgresql+asyncpg://u:p@h:5432/d?ssl=require"
    assert Settings(_env_file=None, database_url="postgresql://u:p@h/d").database_url == "postgresql+asyncpg://u:p@h/d"
    assert Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h/d").database_url == "postgresql+asyncpg://u:p@h/d"
