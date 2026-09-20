# Launchpads
The registry (`app/launchpads/registry.py`) ships **empty**. A `LaunchpadDescriptor` cannot be `enabled` unless it is
`verified`, `mainnet_live` and has a `factory_address` (enforced by a validator + tests). Fields: name, chain, mainnet status,
factory/launch contract, token-creation event, router, pool model (bonding curve/AMM), graduation mechanism, quote asset, fees,
liquidity model, anti-snipe, API, WS/indexer, RPC requirements, contract verification, automation feasibility.
Tokens from a launchpad are only tradable if that launchpad is in the user's `allowed_launchpads` AND not blacklisted.
To add one: verify it against official docs and on-chain, load the descriptor from JSON (`LaunchpadRegistry.from_json`), implement
a `LaunchpadAdapter`. Nothing is assumed live. Arc launch venues: **none verified** (docs were unreachable at build time).
