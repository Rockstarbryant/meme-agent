# Cloud worker audit fix — September 2026

## Problem found

The previous `CloudWorker.process_tenant()` only loaded configuration, checked Privy readiness, sent a heartbeat, and stopped. It did not instantiate the trading engine or execute discovery, strategy, risk, entries, exits, or persistence.

## Engineering changes

- Reused the existing `RunnerRuntime` instead of rebuilding the trading stack.
- Added one runtime/store per tenant.
- Added bounded concurrent tenant processing.
- Added Redis tenant leases to prevent duplicate execution across worker replicas.
- Added PostgreSQL-backed portfolio restore/save endpoints.
- Restores active order idempotency claims after worker restart.
- Runs deterministic position monitoring before and after discovery.
- Uploads the durable event outbox after each tenant cycle.
- Keeps LIVE fail-closed behind the existing explicit verification gates.
- Scoped Privy idempotency hashes to the wallet + order idempotency key to avoid cross-wallet collisions.
- Updated deployment/custody documentation for the Privy cloud model and Northflank worker.

## What was intentionally not changed

The strategy engine, risk engine, paper executor, Arc adapters, frontend, and existing local-runner architecture were not rebuilt. They are reused by the cloud worker.

## Remaining production gate

Real-money LIVE is not declared verified by static code inspection. A real Privy test wallet and Arc test transaction must still verify Privy authorization/policy behaviour, router allowlisting, simulation, receipt parsing, and restart reconciliation before enabling the production LIVE flags.


## Market-data failover update (2026-09-22)

The shared cloud worker no longer depends on Bitquery for Arc market data. The runner now supports an ordered provider registry:

1. `arc_rpc` — free Arc JSON-RPC launchpad/event discovery and ERC-20 metadata.
2. `uniswap_v4_rpc` — direct Arc Uniswap v4 `Initialize`/`Swap` log indexing from the canonical Arc PoolManager. This supplies fresh on-chain price/flow without a paid indexer.
3. `geckoterminal` — free on-chain DEX enrichment for liquidity, market-cap/FDV, volume and transaction statistics, with a 60-second token cache and request pacing below the public limit.
4. `bitquery` — optional fallback for fields not supplied by the free sources. HTTP 401/402/403/429 failures are circuit-broken so a quota problem does not create a tight retry loop.

The normalized `MarketState` model is unchanged. Providers contribute source-attributed fields and later providers only fill missing fields; conflicting non-null values are not silently overwritten.

Because GeckoTerminal's public API is rate-limited, `ARC_RUNNER_MARKET_DATA_CACHE_S=60` and `ARC_RUNNER_MARKET_DATA_MAX_TOKENS=10` are conservative defaults. Adjust them only after observing actual provider limits and worker load.
