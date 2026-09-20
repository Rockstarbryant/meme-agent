# Privy cloud-managed wallets

The cloud execution path uses **one Privy managed wallet per user** and a shared autonomous worker. This is an **app-scoped managed custody model**, not the non-custodial Circle local-session model.

## Current V1 status

- Wallet provisioning: implemented.
- Per-user wallet binding: implemented.
- Shared cloud worker: implemented.
- Paper execution: supported without signing.
- Live transaction path: implemented behind explicit fail-closed gates.
- Production LIVE verification: **not claimed by this source tree** until a real Privy/Arc test validates authorization, policy enforcement, transaction submission, simulation, receipt/fill parsing, and restart reconciliation.

## Provisioning

The control plane calls Privy's wallet API and stores only the provider wallet ID/address required to bind a tenant. It does not store a private key.

```bash
POST /wallet/cloud/provision
{ "confirm": true }
```

The user then receives a dedicated wallet address to fund with Arc USDC.

## Worker configuration

```bash
ARC_RUNNER_WALLET_PROVIDER=privy
ARC_RUNNER_PRIVY_APP_ID=...
ARC_RUNNER_PRIVY_APP_SECRET=...
ARC_RUNNER_PRIVY_WALLET_ID=<tenant wallet id>
ARC_RUNNER_PRIVY_WALLET_ADDRESS=0x...
ARC_RUNNER_PRIVY_CAIP2=eip155:5042
ARC_RUNNER_PRIVY_ALLOW_EXECUTE=false
ARC_RUNNER_LIVE_ENABLED=false
ARC_RUNNER_LIVE_TRADING_VERIFIED=false
```

The shared worker overrides the wallet ID/address per tenant; operators should **not** deploy one hard-coded wallet ID for all users.

## Application policy

The application policy includes:

- maximum trade
- maximum position
- maximum daily loss
- maximum open positions
- maximum slippage
- minimum liquidity
- allowed chains
- allowed launchpads
- allowed routers
- optional allowed function signatures

These checks are defense-in-depth. They are not a substitute for Privy's own policy controls.

## Privy policy requirement

For real-money deployment, configure a Privy-side policy that independently limits the wallet's ability to move funds or call contracts. At minimum, review the exact router/contract allowlist and transaction/value constraints used by the Arc execution adapter.

The production worker must fail closed if the operator has not completed the verification procedure for this release.

## Idempotency and restart recovery

Privy submission idempotency is scoped using the **wallet ID + application order idempotency key + transaction payload**, preventing identical calldata from different tenant wallets from sharing a provider idempotency key.

The worker also restores active order claims from PostgreSQL after restart, so an unknown transaction outcome is not blindly resubmitted.

## Custody disclosure

The UI must describe this wallet as **platform app-scoped managed custody through Privy**. Users should not be told that this wallet is equivalent to a wallet whose signing session remains exclusively on the user's device.

## Free/deployment note

Pricing and plan limits can change. Treat Privy's current pricing page and account dashboard as the source of truth before deployment.
