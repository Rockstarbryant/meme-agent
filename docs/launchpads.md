# Launchpads
The registry (`app/launchpads/registry.py`) ships **empty**. A `LaunchpadDescriptor` cannot be `enabled` unless it is
`verified`, `mainnet_live` and has a `factory_address` (enforced by a validator + tests). Fields: name, chain, mainnet status,
factory/launch contract, token-creation event, router, pool model (bonding curve/AMM), graduation mechanism, quote asset, fees,
liquidity model, anti-snipe, API, WS/indexer, RPC requirements, contract verification, automation feasibility.
Tokens from a launchpad are only tradable if that launchpad is in the user's `allowed_launchpads` AND not blacklisted.
To add one: verify it against official docs and on-chain, load the descriptor from JSON (`LaunchpadRegistry.from_json`), implement
a `LaunchpadAdapter`. Nothing is assumed live. Arc launch venues: **none verified** (docs were unreachable at build time).

Separately, `app/chains/arc/market_data.py::LAUNCHPADS` holds a best-effort, code-level map of factory contract addresses to
names, used only to label a token's creation-tx logs for display (`MarketState.launchpad_detected` /
`launchpad_evidence`, populated by `app/enrichment/probes.py::match_launchpad`). This is deliberately **not** the same
as `LaunchpadRegistry` above and never feeds `allowed_launchpads` gating on its own — a detected name is provenance
shown to the user, not a verified/enabled launchpad. Promoting one from "detected" to "tradable" still requires adding
a proper, verified `LaunchpadDescriptor` here.
