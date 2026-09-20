# Arc autonomous trading agent (Local Runner architecture)

A policy-controlled AI trading agent for Arc. The hosted app is the control plane; autonomous execution can run in a **shared cloud worker** with isolated per-user runtimes and Privy wallets, or in the original local-runner mode.

    Browser -> Next.js UI -> FastAPI control plane (policy, strategy, audit) <== outbound HTTPS only ==> Local Runner -> wallet provider -> Arc
                                                                                 (strategy + deterministic risk engine + execution + exits)

## Run it
    # control plane + DB + Redis
    docker compose up --build                                   # API :8000 (docs at /docs)
    cd frontend && cp .env.example .env.local && npm install && npm run dev      # UI :3000
    # in the UI: Agent -> Add runner (pairing code), then on the machine that will trade:
    cd backend && pip install -r requirements-runner.txt && cp runner.env.example .env
    python -m runner pair --server http://localhost:8000 --code XXXX-XXXX-XXXX
    python -m runner run                                        # ARC_RUNNER_DATA_SOURCE=demo gives labelled DEMO DATA in PAPER
    # tests
    cd backend && pip install -r requirements-dev.txt && python -m pytest -q     # needs local Postgres + Redis
    cd frontend && npm test && npm run lint && npm run typecheck && npm run build

## What is built and tested
- **Control plane:** auth (bcrypt, JWT + revocation, rate limits), runner pairing and revocation, versioned config with ~1s long-poll delivery
  (pause / emergency stop / limits / strategy toggle / blacklists), heartbeats, command queue (close / close-all), replay-safe event ingestion, read APIs,
  decision reconstruction ("why did it buy?"), SSE, Alembic + 27 tables, Redis rate limits.
- **Local Runner:** discovery, Traction Momentum scoring, fail-closed risk engine, optional AI (keys stay local), paper execution, deterministic exits,
  local ceilings, dead-man switch, durable outbox, restart-safe portfolio/idempotency, remote-command dedupe, provider-agnostic wallet registry.
- **Frontend:** 11 routes, mobile-first, persistent PAPER / LIVE / DEMO DATA / EMERGENCY STOP / RUNNER banners, Runner pairing panel, desired-vs-reported agent state.
- **Verification:** backend 180 tests, frontend 82 tests, ESLint, `tsc`, production build, plus real-process checks: `kill -9` of the runner mid-position and
  restart (no duplicate buys), revocation of a running runner, 0 API/type mismatches over 19 endpoints, 0 server errors under concurrent first contact.

## Honest limits
- **LIVE is fail-closed by default.** Quotes, Universal Router calldata, eth_call simulation, and Transfer-log fill reconciliation are implemented.
  Circle CLI remains a separate local-runner path. For cloud-managed Privy LIVE, the same verification gate is used: the operator must verify Privy authorization/policy behaviour, Arc transaction submission, simulation, receipt/fill reconciliation, and restart recovery before setting `ARC_RUNNER_LIVE_TRADING_VERIFIED=true`.
- Market data: Bitquery (Arc launches + pairs) when `ARC_RUNNER_BITQUERY_API_KEY` is set; otherwise the real-data path fails closed. Demo data is available only when `ARC_RUNNER_DATA_SOURCE=demo` and is labelled PAPER/DEMO.
- **Keep the runner running while positions are open**: if it is offline nothing monitors them. One active runner per account.
- Cloud-managed execution is implemented as a real multi-tenant worker. The recommended worker host is Northflank; LIVE remains fail-closed until the Privy/Arc transaction and policy path is independently verified.
- Money values are floats; no CSP yet; JWT in sessionStorage; notifications not implemented.

Docs: `docs/local-runner.md`, `docs/deployment.md` (Render + Vercel + cloud runner), `security.md`, `architecture.md`, `arc-integration.md`, `circle-agent-wallet.md`, `frontend.md`, `api.md`.

## Execution modes

| Mode | Who runs the process | Signing |
|------|----------------------|---------|
| `self_hosted` | User Local Runner | Circle CLI / user machine |
| `cloud_managed` | Shared platform worker | Per-user Privy wallet |

Enable cloud: `POST /wallet/cloud/provision` → fund address → shared worker (`python -m runner cloud-worker`). Docs: `docs/cloud-managed.md`, `docs/per-user-policy.md`.

## Wallet providers

| Provider | Local install? | Cloud LIVE? | Notes |
|----------|----------------|-------------|-------|
| `none` | No | PAPER only | Default |
| `circle_agent_wallet` | Yes (Circle CLI) | No | Max non-custodial |
| `privy` | No | Implemented, verification-gated | Per-user cloud-managed wallet; see `docs/privy-wallet.md` |

## Cloud-ready deployment

1. Push this repo to GitHub.
2. **Backend (Render):** deploy the FastAPI control plane and PostgreSQL/Redis.
3. **Frontend (Vercel):** root `frontend`, set `NEXT_PUBLIC_API_URL` to the Render API URL.
4. **Worker (Northflank):** deploy `backend/Dockerfile.runner` with command `python -m runner cloud-worker`.
5. **Runner:**
   - Local / VPS: `python -m runner pair ...` then `python -m runner run` (or Docker / systemd).
   - The old Render worker blueprint is retained as a reference, but the recommended shared worker is the Northflank service running `python -m runner cloud-worker`.

See `docs/deployment.md` for full steps.

## V2 integration status

V2 wires real Arc launch discovery and market data through Bitquery and Arc swaps through the Uniswap Trading API.
Fill parsing uses ERC-20 Transfer log deltas with quote fallback. LIVE remains opt-in and verification-gated; cloud-managed LIVE requires Privy/Arc verification.
See `docs/v2-audit.md`.
