# Arc multi-source market data

## Provider order

Default runner order:

```text
arc_rpc -> uniswap_v4_rpc -> geckoterminal -> bitquery
```

### `arc_rpc`

- Reads Arc JSON-RPC directly.
- Discovers configured Arc launchpad-created token contracts from their on-chain events.
- Reads ERC-20 metadata and creator/creation-block information.
- Does not fabricate price or liquidity.

### `uniswap_v4_rpc`

- Reads the canonical Arc Uniswap v4 PoolManager directly.
- Discovers USDC pools from `Initialize` events.
- Reads recent `Swap` events and derives price, 1m/5m/15m flow, buy/sell counts and buy/sell USD pressure.
- This is an on-chain source, not an aggregator.
- Pool ID is the Uniswap v4 pool identifier; the PoolManager address is retained as the event source address.

### `geckoterminal`

- Optional enrichment provider.
- Supplies pool liquidity/reserves, volume, price-change statistics, FDV/market cap when available, and transaction/buyer/seller statistics.
- Requests are paced and token snapshots are cached to stay below the public free API rate limit.
- The network slug is configurable because provider coverage can change.

### `bitquery`

- Optional indexed-data fallback.
- Never required for the default runner configuration.
- HTTP 401/402/403/429 errors are treated as provider failures and circuit-break the provider instead of causing a retry storm.

## Merge policy

The registry never overwrites a non-null value from an earlier provider with a conflicting non-null value from a later provider. Later providers only fill missing fields. Every returned `MarketState` keeps `data_sources` so the UI/audit log can show provenance.

## Safety

- Missing liquidity, price, or other required strategy/risk data remains missing.
- Missing data is handled by the existing deterministic risk/strategy gates; no synthetic values are introduced.
- Market-data providers are never used as the source of truth for trade execution. Live execution still requires a verified quote/simulation/fill path from the execution adapter.
- Keep LIVE disabled until the Privy wallet policy and Arc execution path have been separately verified.

## Rate-limit defaults

```env
ARC_RUNNER_MARKET_DATA_MAX_TOKENS=10
ARC_RUNNER_MARKET_DATA_CACHE_S=60
ARC_RUNNER_MARKET_DATA_TIMEOUT_S=6
ARC_RUNNER_MARKET_DATA_FAILURE_THRESHOLD=3
ARC_RUNNER_MARKET_DATA_COOLDOWN_S=60
ARC_RUNNER_RPC_LAUNCH_SCAN_BLOCKS=43200
ARC_RUNNER_UNISWAP_V4_SCAN_BLOCKS=20000
ARC_RUNNER_UNISWAP_V4_SWAP_SCAN_BLOCKS=1800
```

These are conservative V1 defaults. Increase scan breadth or token count only after measuring RPC/provider usage.
