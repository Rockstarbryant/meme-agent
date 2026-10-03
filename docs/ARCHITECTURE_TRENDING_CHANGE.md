# Architecture: Gecko trending → Discovery → Opportunities

## Root cause (why Opportunities stayed empty / wrong)
Cloud worker discovery is **global**, but the per-tenant trade cycle only called
`monitor_once()` (open positions). It never evaluated global registry tokens
into **Decisions**. The Opportunities UI only lists Decisions — so trending
tokens never appeared there even when Discovery had them.

## What this patch does
1. **Bridge**: each tenant cycle evaluates active global-registry addresses via
   `evaluate_global_candidates()` → creates Decision rows → Opportunities fills.
2. **LAUNCHPAD_NOT_ALLOWED removed** — only launchpad **blacklist** still vetoes.
3. Gecko trending (5m/1h/6h/24h) is primary discovery; DexPaprika/Goldsky secondary.
4. Monitoring caps + dead-token pruning; DB retry; longer market-data cache.
5. `scanned_at` stamped / falls back to market `timestamp` on Opportunities.

## Deploy
1. Deploy control plane + **restart platform-shared-worker** (VPS).
2. Env:
   ```
   ARC_RUNNER_MARKET_DATA_PROVIDERS=geckoterminal,dexpaprika,goldsky,dexscreener
   ARC_RUNNER_MARKET_DATA_ESSENTIAL_PROVIDERS=geckoterminal,dexpaprika
   ARC_RUNNER_MARKET_DATA_CACHE_S=120
   ARC_RUNNER_MARKET_DATA_CACHE_TTL_S=45
   ```
3. Expect logs like:
   `global-candidate token=0x… action=…`
   `tenant … evaluated=N cycle_s=…`

## Note on risk
Tokens can still REJECT for real reasons (e.g. `LIQUIDITY_BELOW_MIN` if liq <
$10k). That is intentional risk, not a discovery bug.
