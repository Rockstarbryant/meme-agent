# Local Runner architecture (V1 autonomous execution)

```
User Browser -> Hosted Next.js UI -> FastAPI CONTROL PLANE -> policy / strategy / audit configuration
                                            ^   |
                       events, heartbeats   |   |  config bundle, commands (runner ALWAYS connects OUT)
                                            |   v
                              USER'S LOCAL RUNNER  (on the user's own machine)
       discover -> strategy -> deterministic risk engine -> approved execution adapter -> wallet provider -> Arc
                                                                                 (user's own Circle Agent Wallet session)
```

## Who does what
| Control plane (hosted) | Local Runner (user's machine) |
|---|---|
| authentication, users, audit display | trading loop: discovery, feature/strategy scoring, deterministic risk engine, AI analysis |
| strategy versions, on/off toggle, risk limits, wallet policy, blacklists | applies that config, then applies its OWN local ceilings on top |
| desired state: start / pause / stop, emergency stop, mode | executes PAPER or LIVE, stop-loss / take-profit / trailing / stagnation exits |
| queues close / close-all commands | executes each command exactly once, acknowledges it |
| stores what the runner reports: decisions, orders, tx hashes, positions, P&L, errors | reports everything through a durable local outbox |
| **never** trades, signs, or holds funds, keys, wallet sessions or AI keys | keeps wallet session, approval key, AI keys and state in `~/.arc-runner` (mode 600) |

There is no pooled wallet and no server-side signing code: `tests/test_runner_core.py::test_the_control_plane_holds_no_trading_or_signing_code` enforces it.

## Security properties (each one is tested)
- **Outbound only.** The runner never listens on a port (`test_the_runner_never_listens_for_inbound_connections`).
- **Runner credentials are not wallet credentials.** Pairing exchanges a single-use code (12 chars, 10 min) for a random token; only its SHA-256 is stored. It is accepted only on `/runner/*`; user JWTs are rejected there and vice versa. Revocation is immediate.
- **Local ceilings.** `effective limits = min(control-plane policy, wallet policy, local ceilings)`. A compromised or malicious control plane cannot push the runner past `ARC_RUNNER_CEILING_*`.
- **LIVE needs both keys.** (1) the user's activation in the UI (typed phrase + server kill switch + runner-reported readiness) AND (2) `ARC_RUNNER_LIVE_ENABLED=true` on the runner AND a verified wallet provider. If LIVE is requested but not permitted the runner reports `LIVE_BLOCKED` and does not trade. It never falls back to PAPER silently.
- **PAPER needs no wallet authority** at all.
- **Dead-man switch.** No control-plane contact for `ARC_RUNNER_MAX_OFFLINE_S` (60s) => no NEW entries. Exits, stop-loss and trailing logic keep running locally. Emergency stop propagates through a long-poll in about a second.
- **Durable outbox.** Events are written locally first, coalesced (position updates), uploaded in batches, replay-safe on the server (per-runner monotonic sequence), and survive restarts and outages. A poison event is skipped, never blocking the queue. A runner can only ever write rows for its own user.
- **Restart-safe.** Portfolio, daily-loss counter and idempotency keys persist locally; a restarted runner never buys the same token twice.
- **Revoked runner** stops taking new positions and keeps protecting existing ones until the process is stopped.

## Setup
1. In the web app: Agent page -> **Add runner** -> copy the pairing code.
2. On your machine: `pip install -r backend/requirements-runner.txt` (or use `Dockerfile.runner`), copy `backend/runner.env.example` to `.env`, then
   `python -m runner pair --server https://YOUR-API --code XXXX-XXXX-XXXX` and `python -m runner run`.
3. In the web app: set policy/limits, press **Start**. `python -m runner status` shows local readiness.

## Wallet / delegation providers (provider-agnostic)
The execution engine depends only on `app.wallets.base.WalletProvider`. Adding a provider = implement it and call `register_wallet_provider(name, factory)` in `runner/wallets.py`; strategy, risk and execution code do not change (`test_the_execution_layer_is_provider_agnostic`). A provider's `live_readiness()` lists every reason it cannot yet execute LIVE unattended; the runner reports those reasons to the UI.

### Circle Agent Wallet provider: implemented, DISABLED
Built from the CLI's own `--help` (`@circle-fin/cli` 1.1.3): `wallet status/list/balance/limit/limit budget`, `wallet execute <abi signature> <params...> --contract --address --chain [--estimate] [--idempotency-key]`. Argument construction is shell-free and validated (option injection, control characters, malformed addresses/signatures are rejected); the adapter has no login / terms / limit-set / limit-reset code path and strips `CIRCLE_ACCEPT_TERMS` from the environment.

It stays disabled (`VERIFIED = False`) because these cannot be established without an authenticated session, and nothing is guessed:
1. the JSON output schemas of `wallet status`, `wallet execute`, `transaction list`;
2. how an agent-wallet transaction id maps to a chain hash (`circle transaction` has only list / cancel / accelerate);
3. whether `wallet execute` (contract writes) counts toward the human-set USDC spending caps;
4. session lifetime for unattended use.
Separately, the Arc execution path (router address/ABI, quote, fill parsing, fee floor) is unverified, so `live_trading_verified=False`.

**To finish verification:** run `python -m runner circle-verify [--include-estimate]` on your machine. It is read-only (no login, no limit changes, `execute` only ever with `--estimate`), redacts secret-looking fields, and writes `~/.arc-runner/circle-capture-*.json` for review. Nothing is sent anywhere.

## Failure behaviour
| Situation | Behaviour |
|---|---|
| control plane down | events buffer locally; trading continues for up to 60s, then no new entries; exits always run; catch-up upload is batched on reconnect |
| runner offline | UI shows OFFLINE / RUNNER OFFLINE; open positions are NOT monitored by anything. Keep the runner running while positions are open |
| runner token revoked | no new entries; positions stay protected until the process stops; re-pair to reconnect |
| LIVE requested, not permitted | `LIVE_BLOCKED` with reasons; no trading |
| duplicate command / event delivery | commands run once; events skipped by sequence and by id |
| AI provider down | no new AI-dependent entries; exits unaffected |
