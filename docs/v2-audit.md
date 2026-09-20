# V2 audit and implementation notes

V2 is a targeted rebuild of the Claude-generated project. It keeps the control plane, Local Runner, deterministic risk engine, paper/live separation, approval proof, and frontend, and replaces the disabled Arc data/DEX placeholders.

## Implemented

- Real Arc launch discovery through Bitquery V2 `EVM(network: arc).Events`.
- Verified Arc launchpad registry for Argus, RadarDEX Classic/Reflection, Tolly, Warp, Archemist V2 and PEGD V4.
- Real Arc market snapshots through Bitquery `Trading.Pairs`, `Trading.Tokens`, `Trading.Trades`.
- Holder/top-holder data through Bitquery `EVM.Holders`.
- Recent pool reserve/liquidity evidence through Bitquery `EVM.DEXPoolEvents`.
- Current Uniswap Arc deployment constants, including Universal Router, PoolManager, Quoter, PositionManager and Permit2.
- Uniswap Trading API integration restricted to V2/V3/V4 AMM routes; UniswapX is deliberately excluded from the autonomous executor because it creates a different order/settlement lifecycle.
- Universal Router calldata decoding into an ABI function signature/parameters for the Circle CLI execution interface.
- Arc `eth_call` preflight simulation of the exact generated transaction.
- Circle CLI 1.1.x wallet/status/balance/policy integration on the Local Runner.
- Explicit local acknowledgement flag for `circle wallet execute`.
- V2 `.env` configuration for Bitquery and Uniswap API credentials.

## Deliberately still fail-closed (opt-in)

Live execution is **not enabled by default**. The Arc adapter's `live_trading_verified` is controlled by
`ARC_RUNNER_LIVE_TRADING_VERIFIED` (default `false`). Fill reconciliation from ERC-20 Transfer logs is implemented
with quote fallback. Remaining steps before flipping the flag on a machine you control:

1. Run `python -m runner circle-verify [--include-estimate]` and review the capture JSON.
2. Confirm Circle CLI `wallet execute` response schema (tx id / hash) and that spending policy applies to contract calls.
3. Confirm Permit2 / typed-data path if your Universal Router flow requires it.
4. Prefer a small Arc testnet transaction before mainnet.

This is intentional. V2 must never convert an unverified signing assumption into a live trade.

## Current official integration facts used by V2

- Arc mainnet chain ID: `5042`.
- Arc mainnet RPC: `https://rpc.mainnet.arc.io`.
- Arc ERC-20 USDC view: `0x3600000000000000000000000000000000000000`, 6 decimals.
- Uniswap Arc Universal Router: `0x4fcA4a51Ab4F23A7447b3284fBd7D73289A89Fb1`.
- Uniswap Arc v4 PoolManager: `0x8366a39CC670B4001A1121B8F6A443A643e40951`.
- Uniswap Arc v4 Quoter: `0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94`.
- Uniswap Arc v4 PositionManager: `0x6049c9a0e26405C0985f9E3685C87d0aE917f82B`.
- Uniswap Permit2: `0x000000000022D473030F116dDEE9F6B43aC78BA3`.

## Data-source limitations

Bitquery's Arc Trading cube is a rolling real-time/short-history source. V2 does not pretend it is an archive. Liquidity is read from the DEX pool cube only when a recent pool-state row is available. Creator-sold history and contract-source verification remain separate risk-provider work; unknown values remain unknown and are never silently treated as safe.
