# Enrichment

## Why this exists

A raw market-data provider (DexPaprika, GeckoTerminal, DexScreener, Arc RPC) gives price/liquidity/volume and,
sometimes, buy/sell counts. It does not know whether a contract is verified, whether an ordinary holder can still
sell, who created the token, how concentrated the top holders are, or how exposed a trade is to MEV. Without that,
`traction_momentum`'s `required_data_present` gate and the risk engine's CONTRACT/CREATOR/HOLDER/MEV checks were
starved of data on most tokens — not because the strategy rejected them on the merits, but because the fields those
checks need were never populated. `app/enrichment/` closes that gap.

## Two bugs this also fixed

1. **`buys_5m`/`sells_5m` treated `0` as "missing".** `app/chains/arc/gecko_market_data.py` (and DexScreener's
   provider) parsed `int(_num(...) or 0) or None`, which turns a real `0` into `None`. A token with genuinely zero
   buyers in the last 5 minutes is informative — it is not the same as "no data." This silently starved
   `unique_buyers_5m` (the `buyer_growth` gate) and the buy/sell ratio on a large fraction of tokens. Fixed by a
   `_count()` helper that preserves `0`.
2. **`GlobalTokenRegistry.upsert()` double-applied `score_delta`.** `GlobalMonitoringService` computes
   `score_delta` and mutates `token.current_score` in place *before* calling `registry.upsert(token)` — but
   `list_by_status()` returns live references, so `existing is token`, and `upsert()` was recomputing the delta
   against the value it had just been given, zeroing it out (`new - new == 0`). This broke the
   IMPROVING/DECLINING momentum status used to prioritize monitoring. Fixed: `upsert()` now recognizes
   `existing is token` and skips re-deriving fields the caller already computed.

## What each source is used for

| Source | What it's used for | Notes |
|---|---|---|
| **Blockscout** (`app/integrations/blockscout.py`) | Token holders + holder count, contract verification status, creator address, creation tx | Arc's official explorer. A Pro API key raises the rate limit and is credit-budget-guarded (`BLOCKSCOUT_DAILY_CREDIT_BUDGET`, default 90K/day); without one, the keyless public route is used. Both speak the same v2 REST API. 404 is treated as "not indexed yet," not an error. |
| **Etherscan V2** (`app/integrations/etherscan.py`) | Secondary source for the same facts, plus a fallback holder list | One endpoint, `chainid=5042`. A plan-gated response (`getsourcecode` etc. requiring a paid plan on this chain) is detected and that specific call stops being retried for the rest of the process. |
| **On-chain RPC probes** (`app/enrichment/probes.py`) | Mint/pause/blacklist capability (bytecode selector scan), EIP-1167/1967 proxy detection, owner/paused/trading-flag reads, buy/sell tax reads, creation-tx → launchpad matching, a holder-transfer sell probe | Free (just RPC calls), always runs when an `EvmRpcClient` is configured, no explorer key needed. |
| **MEV heuristic** (`app/enrichment/mev.py`) | `MarketState.mev_risk_score` | A bounded 0..1 score from price impact + liquidity depth + unexplained volatility. Always labeled `mev_method="heuristic_v1"` — this is a proxy, not a mempool/bundle simulation, and is never presented as one. |

`app/enrichment/service.py::EnrichmentService` orchestrates all of the above with TTL caching (holders: 5 min,
verification/creator/launchpad: 6h, on-chain probe: 15 min) and merges results into the `MarketState` it is given.
`app/enrichment/wrapper.py::EnrichingMarketDataProvider` is a `MarketDataProvider` decorator that calls the
underlying provider then enrichment; it is what actually gets wired in as `self.market_data` in both
`runner/runtime.py` (local runner + each cloud-worker tenant's own `RunnerRuntime`) and
`app/discovery/bootstrap.py::GlobalPipeline` (shared discovery/monitoring registry).

## Honesty guarantees

- **Every source is optional.** Missing/unconfigured/erroring sources degrade to `None`, never a fabricated value.
  The reason is recorded in `MarketState.enrichment_gaps` (plain-English, e.g. `"contract verification status
  unknown (no explorer configured or reachable)"`).
- **Tri-state admin powers.** `mint_authority_active` / `pausable` / `blacklist_capability` are `True` (capability
  present, admin live), `False` (capability absent, or present but ownership renounced), or `None` (capability
  detected in bytecode but the admin model itself — e.g. no `owner()` — could not be read). The risk engine now
  treats `None` as a WARN, not a silent pass (previously it was falsy and simply didn't veto).
- **The sell check is a holder-transfer probe, not a router simulation.** `eth_call transfer(...)` from a holder
  with a known non-zero balance (the creator, when known) catches paused tokens, blacklists and hard transfer
  locks. It cannot see pool-level taxes or hook logic. `ContractInfo.sell_check_method` is always set to
  `"holder_transfer_probe"` when this is how the answer was obtained, and the risk engine surfaces a
  `SELLABILITY_HEURISTIC_ONLY` warning rather than treating a pass as certain.
- **Launchpad detection is display-only.** `MarketState.launchpad_detected` / `launchpad_evidence` come from
  matching a token's creation-tx logs against `app/chains/arc/market_data.py::LAUNCHPADS` (a best-effort
  factory-address map). This is intentionally separate from `MarketState.launchpad` and from
  `app/launchpads/registry.py::LaunchpadRegistry` (which still ships empty — see docs/launchpads.md) and never by
  itself makes a token pass `allowed_launchpads` gating.
- **The buy/sell ratio is labeled by how it was computed.** `MarketState.buy_sell_basis` is `"usd"` (real
  buy/sell USD volume from trade history), `"estimated_from_counts"` (total volume split proportionally by
  buy/sell transaction counts), or `"counts"` (only transaction counts known, no USD volume at all). The frontend
  shows a `~` prefix and a tooltip whenever the basis isn't `"usd"`.

## Configuration

All settings are on `runner/settings.py::RunnerSettings` (env prefix `ARC_RUNNER_`) — see `backend/runner.env.example`
for the full list with defaults and explanations: `ENRICHMENT_ENABLED`, `BLOCKSCOUT_*`, `ETHERSCAN_*`,
`ENRICHMENT_HOLDERS_TTL_S` / `ENRICHMENT_STATIC_TTL_S` / `ENRICHMENT_PROBE_TTL_S`. With nothing configured beyond the
defaults, enrichment still runs (Blockscout's free public route + on-chain RPC probes); it degrades further, never
fabricates, when even those are unreachable for a given token.

## Data flow to the frontend

`app/discovery/bootstrap.py::market_state_to_snapshot` / `market_state_from_snapshot` round-trip every enrichment
field through the shared discovery registry's snapshot cache. Separately, `app/services/decision.py` stores
`market=m.model_dump(mode="json")` on each `Decision` — since this is a full Pydantic dump of the (already enriched)
`MarketState`, every new field reaches the API (`GET /decisions/{id}` → `what_the_agent_saw`, `GET /opportunities` →
`_opp()`) and the frontend (`types/api.ts::Opportunity`, `opportunities.tsx`, `token-detail.tsx`,
`decision-detail.tsx`) without additional plumbing.
