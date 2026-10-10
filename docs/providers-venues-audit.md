# Providers, venues, agentic AI and the audit log

## Market-data providers (fallback order = `ARC_RUNNER_MARKET_DATA_PROVIDERS`)
| name | what it supplies | notes |
|---|---|---|
| `geckoterminal`, `dexpaprika`, `dexscreener` | price, liquidity, volume, windows | unchanged |
| `codex` | price, liquidity, market cap, holder count, 5m/1h/4h/12h/24h buys, sells, unique traders, volume, change | Arc = network 5042. Free key works. Discovery off by default. |
| `goldsky` | on-chain price/flow (Uniswap v4 Swap events) + launchpad metadata via Goldsky Edge RPC | now its own named provider: health, circuit breaker and audit say "goldsky"; no hidden Alchemy fallback inside it |
| `goldrush` | holder count + holder concentration only (never price) | Arc is a Frontier chain on GoldRush: no DEX prices there |

Enrichment holder chain: Blockscout -> Etherscan -> Codex -> GoldRush (`ARC_RUNNER_ENRICHMENT_HOLDER_SOURCES`). Codex's holder *list* needs a paid plan;
on the free plan its `top10HoldersPercent` + holder count are still used.

Every `MarketState` now carries `field_sources` (field -> provider). The token page shows it under "Data from".

## Why live buys were rejected
`QUOTE_DEVIATES_FROM_MARKET` compared the quote's execution price (which includes the pool fee) with the market mid price.
Arc meme pools charge 1-5% per hop, and the routes in your dashboard were two or three hops (`v4-pool:50000|v4-pool:20000` = 6.9% in fees alone).
The check is now fee-aware: `deviation_ex_fee = |quote x (1 - fee) / market - 1|`. Separate limits (Settings > Risk limits):
* **Max price impact** (`max_price_impact_pct`) is compared with the quote's price impact (before, it was compared with the slippage tolerance).
* **Max total cost** (`max_total_cost_pct`, default 5) caps pool fees + impact vs the market price on BUY.

## Venues
`uniswap` (Trading API: V2/V3/V4) and `kyberswap` (aggregator, slug `arc`). KyberSwap is **quote-only** until you set
`ARC_RUNNER_KYBERSWAP_ROUTER_ALLOWLIST` to the router you verified on-chain, add it to the wallet policy's allowed routers, and the wallet has
approved USDC to it (the venue only reads the allowance, never sends approvals).

## Agentic AI (`ARC_RUNNER_AI_AGENT_MODE=tools`)
The model may call read-only tools (`get_market_data`, `get_trading_policy`, `get_wallet_state`, `list_venues`, `quote_venues`), then answers with the
usual decision plus a `venue`. Code, not the model, enforces the result: it re-quotes every venue at the final size, trades only on one that
passes your limits and can execute, and otherwise records `NO_VENUE_WITHIN_POLICY: <why per venue>` (a WATCH, no order, no cooldown).

## Position monitoring
Deterministic stop-loss / take-profit run every monitor tick. The AI reviewer (`ARC_RUNNER_AI_EXIT_MODE=off|shadow|live`) reviews each open position
every `ARC_RUNNER_AI_EXIT_INTERVAL_S` seconds (default 300). `shadow` only logs what it would do.

## Audit log (admin only)
`GET /admin/audit`, `/admin/audit/providers`, `/admin/audit/summary`, `/admin/audit/decision/{id}`; UI at Admin > Audit log.
Admins: `users.is_admin` or `ADMIN_EMAILS`. Others get 404. Failures are always recorded; successes are sampled (1/min/provider) except AI answers,
agent steps, venue quotes and execution rejections. Identical consecutive failures collapse into one row with a counter. Secrets are scrubbed.
Retention: `OPS_AUDIT_RETENTION_DAYS` (14). Migration `0006` adds `ops_audit_log` and `users.is_admin`.
