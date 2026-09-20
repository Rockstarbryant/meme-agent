# Deployment

## Production topology

```text
Vercel
  Next.js frontend
       |
Render
  FastAPI + PostgreSQL + Redis
       | authenticated platform-worker API
Northflank
  persistent cloud worker
       |
Privy
  per-user managed wallet
       |
Arc Mainnet
```

### Control plane — Render

Deploy `backend/Dockerfile` with PostgreSQL and Redis. Configure:

- `DATABASE_URL`
- `REDIS_URL`
- `SECRET_KEY`
- `JWT_SECRET`
- `CORS_ORIGINS`
- `CLOUD_MANAGED_ENABLED=true`
- `PLATFORM_WORKER_TOKEN=<long random secret>`
- `PRIVY_APP_ID`
- `PRIVY_APP_SECRET`

Run Alembic migrations before serving traffic.

### Frontend — Vercel

Set `NEXT_PUBLIC_API_URL` to the Render API URL and add the Vercel origin to `CORS_ORIGINS`.

### Autonomous worker — Northflank

Create a Docker service from `backend/Dockerfile.runner` and override the command with:

```bash
python -m runner cloud-worker
```

Set:

```bash
ARC_RUNNER_SERVER_URL=https://YOUR-API.onrender.com
PLATFORM_WORKER_TOKEN=<same secret as Render>
ARC_RUNNER_WALLET_PROVIDER=privy
ARC_RUNNER_PRIVY_APP_ID=<Privy App ID>
ARC_RUNNER_PRIVY_APP_SECRET=<Privy App Secret>
ARC_RUNNER_PRIVY_ALLOW_EXECUTE=false
ARC_RUNNER_LIVE_ENABLED=false
ARC_RUNNER_LIVE_TRADING_VERIFIED=false
ARC_RUNNER_ARC_RPC_URL=https://rpc.mainnet.arc.io
ARC_RUNNER_UNISWAP_API_KEY=<Uniswap Trading API key>
ARC_RUNNER_MAX_CONCURRENT_TENANTS=5
ARC_RUNNER_TENANT_LEASE_TTL_S=45
```

For PAPER mode, `PRIVY_ALLOW_EXECUTE` can remain false because no signing is required.

## Why the worker is separate

Render free web services are not the correct place for a persistent autonomous worker. The worker is therefore separated from the FastAPI control plane. The API handles user/auth/config/audit/state persistence; the Northflank process performs the autonomous loop.

## Restart behaviour

On startup the worker does not trust its previous in-memory state. It loads the tenant's portfolio from PostgreSQL, restores active execution idempotency claims, creates a fresh tenant runtime, and only then starts discovery/execution.

A worker replica must acquire the tenant lease before processing. This prevents two replicas from simultaneously running the same user's strategy.

## LIVE enablement

Keep all LIVE gates false until a real test has verified:

1. Privy wallet lookup and authorization.
2. Privy policy enforcement.
3. Arc chain ID and RPC.
4. Uniswap/approved router quote and calldata.
5. Transaction simulation.
6. Receipt confirmation.
7. Actual ERC-20 fill parsing.
8. Unknown-outcome reconciliation.
9. Restart during a pending transaction.
10. Router/function/value allowlists.

The code deliberately remains fail-closed when those conditions are not verified.

## Local development

```bash
docker compose up --build
cd frontend && npm install && npm run dev
```

For a local worker:

```bash
cd backend
pip install -r requirements-runner.txt
python -m runner run
```

For the shared cloud worker:

```bash
cd backend
python -m runner cloud-worker
```

## Verification

Run:

```bash
cd backend
python -m compileall -q app runner
python -m pytest -q
```

The full test suite requires the project's PostgreSQL/Redis test dependencies.
