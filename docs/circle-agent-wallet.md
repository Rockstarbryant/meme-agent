# Circle Agent Wallet: decision record

**Decision (accepted): the Local Runner architecture.** Each user runs the execution agent on their own machine next to their own
Circle session; the hosted app is a control plane only (docs/local-runner.md). Hosted sessions were rejected: they would make the server a
delegate of every user's funds. Per-trade browser signing remains an optional manual fallback, not the autonomous mode.

## What Circle documents (CLI 1.1.3 `--help`, official skills repo)
- Non-custodial agent-controlled wallets, Arc default chain; email + OTP login by the human; mainnet and testnet sessions are independent.
- `circle wallet execute <abiFunctionSignature> [params...] --contract --address --chain [--amount] [--idempotency-key] [--estimate]`.
- `circle wallet limit` / `limit budget` show per-tx / daily / weekly / monthly USDC caps; `limit set` / `reset` need a human OTP; mainnet only.
- `circle transaction` verbs: list, cancel, accelerate (no `get`).

## Still unverified (blocks autonomous LIVE)
JSON output schemas (status / execute / transaction list); agent-wallet transaction id -> chain hash; whether `execute` counts toward the caps;
session lifetime for unattended use; router/contract allowlists and slippage (enforced by this app instead).
Run `python -m runner circle-verify` on your machine to capture the real outputs; then review and flip `CircleAgentWalletProvider.VERIFIED`.


## Enabling LIVE after verification

1. `python -m runner circle-verify --include-estimate`
2. Review `~/.arc-runner/circle-capture-*.json`
3. Set on the runner env:
   - `ARC_RUNNER_LIVE_ENABLED=true`
   - `ARC_RUNNER_WALLET_PROVIDER=circle_agent_wallet`
   - `ARC_RUNNER_CIRCLE_WALLET_ADDRESS=0x...`
   - `ARC_RUNNER_CIRCLE_ALLOW_CONTRACT_EXECUTE=true`
   - `ARC_RUNNER_LIVE_TRADING_VERIFIED=true`
4. Activate LIVE in the web UI.

Cloud-hosted runners (e.g. Render worker) should keep LIVE disabled unless you can maintain an authenticated Circle CLI session there (OTP login is interactive).
