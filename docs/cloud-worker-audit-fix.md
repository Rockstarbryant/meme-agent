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
