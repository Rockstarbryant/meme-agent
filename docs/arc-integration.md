# Arc / Circle integration: what is verified, what is not

## Sources actually read
1. **User packet** `arc-circle-implementation-docs.md` (dated 2026-09-19).
2. **Circle's official skills repo** (github.com/circlefin/skills, fetched live): `use-arc`, `use-circle-wallets`,
   `use-modular-wallets`, `use-user-controlled-wallets`, `use-agent-wallet`, `agent-wallet-policy`, `fund-agent-wallet`,
   `use-circle-cli`, `swap-tokens`, plus the repo README.
3. **Still unreachable from the build sandbox:** docs.arc.io, developers.circle.com, Uniswap docs/API, pools.xyz.
   The packet's own rule is "if it conflicts with a live official page, re-check the live page". No conflict was found, but
   nothing here was checked against the live docs sites.

## Verified (two independent sources agree) and implemented
| Fact | Where in code |
|---|---|
| Mainnet: chain id 5042 (0x13B2), RPC `https://rpc.mainnet.arc.io`, WS `wss://rpc.mainnet.arc.io`, explorer `https://explorer.arc.io`, CCTP domain 26 | `chains/arc/network.py`, `Settings.arc_network` |
| Testnet: 5042002 (0x4CEF52), `rpc/explorer.testnet.arc.io`, faucet `faucet.circle.com` | same; `ARC_NETWORK=testnet` |
| Settings refuse a chain id that contradicts `ARC_NETWORK` (no accidental mainnet/testnet mix-up) | `Settings._arc_defaults`, tested |
| USDC = ONE pool of funds, two views: native (18 dec, gas/`msg.value` only) and ERC-20 (6 dec) at `0x3600...0000` | `network.py`, `ArcAdapter.usdc_balance` reads ONLY the ERC-20 view; no native/ERC-20 conversion helper exists |
| Never call `decimals()` on native sentinels; never treat USDC and native as separate assets | `erc20_balance` refuses sentinels; risk veto `NON_TRADABLE_ASSET` |
| Sub-second deterministic finality: a successful receipt is final, no N-confirmation wait | poll 0.25s, `LiveSettings.confirm_timeout_s=20`; no receipt => `TIMEOUT` (never success) |
| Arc mainnet + testnet both live; the "testnet only" sentence in the Arc LLM index is stale (packet, section 2) | default `ARC_NETWORK=mainnet`, live still gated |

## Established by one official source, not implemented
| Fact | Consequence |
|---|---|
| Circle **Swap Kit / App Kit** supports `Arc`/`Arc_Testnet`; routes via third-party aggregators (currently LiFi); Viem adapter for browser wallets (user signs) | Best candidate for the **per-trade signing** path (browser, intent-based). Caveat: aggregator routing means a **router allowlist cannot be enforced** on that path; policy must be on token/amount/slippage. |
| Circle **Modular Wallets** support Arc (passkeys, gasless, ERC-6900 modules) | No documented session-key/spend-limit module for unattended agents. Not integrated. |
| Circle **User-Controlled Wallets**: every sensitive operation needs the user's PIN/OTP approval | Per-trade by design, not autonomous. |
| Circle **Developer-Controlled Wallets**: server-side custody | **Forbidden** by this project. |
| Circle **Agent Wallet** (CLI 1.1.3, verified from `--help`): non-custodial, Arc default, `wallet execute <abi signature> <params>` (not raw calldata; `--estimate`, `--idempotency-key`), `wallet limit` / `limit budget`, USDC caps set only via human OTP, mainnet-only; `circle transaction` has only list/cancel/accelerate | **Decision made: Local Runner** (docs/local-runner.md). Adapter implemented, LIVE disabled until schemas / tx-id->hash / cap semantics are verified from a real session (`python -m runner circle-verify`). |

## Evidence only (kept disabled)
- **Uniswap** v2/v3/v4/UniswapX live on Arc; candidate factory/PoolManager addresses in `chains/arc/deployments.py`
  marked `unverified_evidence`. **No router address is known**: the execution router allowlist stays empty (deny all).
- **Pools** (Uniswap v4 pools paired with USDC, locked liquidity, no launchpad fees, optional 1-hour Crowd Launch) is registered
  as a **disabled, unverified** launchpad candidate. Contracts/events/APIs are unknown.

## Not verifiable with the material available
Router addresses and ABI for execution, Pools contracts/events, holder/data indexers, the `maxFeePerGas` floor value, the
details of Arc's USDC Transfer system emitter (so fills must NOT be parsed from USDC Transfer logs), and whether Agent Wallet
caps apply to `wallet execute`, the Circle CLI JSON schemas, and the agent-wallet tx-id to chain-hash mapping. `ChainAdapter.quote/build_swap_tx/parse_fill` therefore still raise `IntegrationNotVerified`
and `live_trading_verified` stays `False`: **LIVE remains impossible by design until these are verified.**

## Arc EVM differences to honour (names only; numbers are on docs.arc.io and were not read)
See `EVM_DIFFERENCES_TO_HANDLE` in `network.py`.
