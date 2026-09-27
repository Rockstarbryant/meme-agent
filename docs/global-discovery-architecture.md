# Global Token Discovery Architecture

## Separation of concerns

| Concern | Scope | Schedule | Trade cycle? |
|---------|-------|----------|--------------|
| Token Discovery | Global | Default **3 hours** | **No** |
| Token Monitoring | Global HOT/WARM/COLD | Priority intervals | **No** |
| Trade / Agent cycle | Per tenant | Runner poll interval | This *is* the cycle |

## Live market data after discovery

Monitoring **refreshes** price, market_cap, liquidity, holders, volume on every
pass via `fetch_state_fn` → `SharedMarketDataGateway` → providers.

Results are written to:
- `launchpad_tokens.last_snapshot` (Postgres)
- `market_snapshots` history rows
- Redis keys `gsnap:{chain}:{address}` (short TTL)

So values are **not** frozen at discovery time.

## Flow

```
GLOBAL LAUNCH DISCOVERY  (GlobalDiscoveryService, own schedule + Redis lock)
        ↓
GLOBAL TOKEN REGISTRY    (Postgres launchpad_tokens + Redis mirror)
        ↓
GLOBAL CANDIDATE POOL    (status WATCHING / IMPROVING / QUALIFIED …)
        ↓
SHARED MARKET-DATA GATEWAY  (one provider call, many consumers)
        ↓
MONITORING               (updates snapshots + scores + priority)
        ↓
USER STRATEGY EVAL       (per tenant, reads shared snapshot)
        ↓
RISK ENGINE              (never bypassed by BUY ANYWAY)
        ↓
TRADE EXECUTION
```

## Configuration

- `ARC_RUNNER_GLOBAL_DISCOVERY_INTERVAL_S` (default 10800)
- `ARC_RUNNER_GLOBAL_DISCOVERY_LOOKBACK_S` (default 900)
- `ARC_RUNNER_MONITOR_HOT_INTERVAL_S` / `WARM` / `COLD`
- `ARC_RUNNER_REDIS_URL` / `DATABASE_URL` on the cloud worker
- `ARC_RUNNER_ENABLE_LOCAL_DISCOVERY=1` only for local-runner demos

## Premium

`users.premium_scanner` gates automatic global discovery access messaging.
Search, bookmarks, and historical pages remain available without premium.

## Migration

`alembic upgrade head` applies `0005_launchpad_tokens_and_bookmarks`.
