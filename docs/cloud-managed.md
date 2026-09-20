# Cloud-managed autonomous trading

The production cloud path is now a **real multi-tenant worker**, not a readiness-only loop.

```text
Vercel frontend
    | HTTPS
Render FastAPI control plane ---- PostgreSQL + Redis
    | authenticated platform-worker API
Northflank persistent worker
    |
    +-- TenantRuntime A -> Privy wallet A -> Arc
    +-- TenantRuntime B -> Privy wallet B -> Arc
    +-- ... bounded concurrency
```

## What the worker actually does

For every active `cloud_managed` tenant, the worker:

1. Acquires a Redis-backed tenant lease so two worker replicas cannot trade the same account concurrently.
2. Pulls the authoritative versioned config.
3. Creates a **fresh `RunnerRuntime` and `LocalStore` for that tenant**. No portfolio, engine, strategy state, token cache, or idempotency store is shared between users.
4. Restores the tenant portfolio from the control-plane database before trading.
5. Seeds local idempotency claims from persisted active orders so a restart does not blindly resubmit a pending/unknown transaction.
6. Applies the user's risk policy plus worker hard ceilings.
7. Runs deterministic position protection, token discovery, strategy/risk/AI decisioning, and execution.
8. Uploads the durable event outbox.
9. Persists the resulting portfolio back to PostgreSQL.
10. Releases the tenant lease.

`ARC_RUNNER_MAX_CONCURRENT_TENANTS` defaults to `5`; increase only after load testing.

## State and restart safety

The control plane now exposes worker-only state endpoints for portfolio recovery. PostgreSQL is authoritative; the worker's SQLite file is a cache/outbox, not the source of truth.

The worker also restores execution idempotency claims for orders in `PENDING_SIGNATURE`, `SUBMITTED`, `TIMEOUT`, `FILLED`, and `PARTIALLY_FILLED` states. Unknown live transactions are therefore not automatically retried after a process restart.

## Privy custody model

The current V1 implementation uses **Privy app-scoped managed wallets**. This is different from the earlier Circle local-session design. The platform creates/records a Privy wallet per user and the cloud worker operates that wallet. Users must be shown a clear custody disclosure in the UI.

The application `WalletPolicy` remains a defense-in-depth policy. For production LIVE, a matching Privy policy/allowlist must also be configured and independently verified in Privy's infrastructure. Do not treat the application's database policy as the sole security boundary.

## LIVE fail-closed requirements

LIVE remains disabled until all of these are explicitly enabled:

```bash
ARC_RUNNER_PRIVY_ALLOW_EXECUTE=true
ARC_RUNNER_LIVE_ENABLED=true
ARC_RUNNER_LIVE_TRADING_VERIFIED=true
```

The last flag is an operator verification gate. It must only be enabled after a real Privy test wallet on Arc has been used to verify the exact transaction submission, router allowlist, simulation, receipt/fill parsing, and Privy policy behaviour used by this release.

## Worker environment

```bash
ARC_RUNNER_SERVER_URL=https://YOUR-API.onrender.com
PLATFORM_WORKER_TOKEN=...
ARC_RUNNER_WALLET_PROVIDER=privy
ARC_RUNNER_PRIVY_APP_ID=...
ARC_RUNNER_PRIVY_APP_SECRET=...
ARC_RUNNER_PRIVY_ALLOW_EXECUTE=false
ARC_RUNNER_LIVE_ENABLED=false
ARC_RUNNER_LIVE_TRADING_VERIFIED=false
ARC_RUNNER_ARC_RPC_URL=https://rpc.mainnet.arc.io
ARC_RUNNER_UNISWAP_API_KEY=...
ARC_RUNNER_MAX_CONCURRENT_TENANTS=5
ARC_RUNNER_TENANT_LEASE_TTL_S=45
```

## Northflank deployment

Use Northflank for the persistent worker because its free/sandbox service model is suitable for an always-on container, unlike platforms where free worker services sleep or are paid-only.

Create a Docker service from this repository:

- Dockerfile: `backend/Dockerfile.runner`
- Working directory / Docker context: `backend`
- Command: `python -m runner cloud-worker`
- Do not expose an HTTP port.
- Add the worker environment variables above.
- Keep the Render API URL in `ARC_RUNNER_SERVER_URL`.

The worker can use ephemeral local storage because PostgreSQL is the authoritative portfolio/state store and the local outbox is flushed to the control plane.

## User flow

1. User signs in.
2. User provisions a cloud Privy wallet.
3. UI displays the wallet address and custody disclosure.
4. User funds the wallet with Arc USDC.
5. User configures strategy/risk limits.
6. User activates PAPER first.
7. After the live integration has been independently verified, the operator can enable the production LIVE gates.

## Important limitation

A cloud worker can now execute the complete trading pipeline, but this ZIP does **not** claim that Privy LIVE is production-verified merely because the code path exists. Real-money enablement still requires a live Privy/Arc verification run and confirmation that the configured Privy policy blocks transactions outside the intended router/function/value limits.
