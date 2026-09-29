# Risk engine
Categories: CONTRACT, LIQUIDITY, HOLDER, CREATOR, MARKET, EXECUTION, MEV, PORTFOLIO. Any VETO => REJECT, regardless of AI or score.
Pipeline: strategy score -> risk pre-filter (before spending AI tokens) -> AI (qualified only) -> risk + wallet-policy re-check on the
final sized order -> signed approval. Effective limits = the stricter of app limits and wallet policy.
Entry-only vetoes (emergency stop, pauses, allowlists, daily loss, exposure, duplicates, cooldown) never block exits, so stop-loss,
take-profit, trailing and stagnation exits keep running during an emergency stop or AI outage.
Daily loss = realized today + unrealized losses only (open gains cannot mask a breach).

CONTRACT/CREATOR/EXECUTION/MEV data comes from `app/enrichment/` (see docs/enrichment.md), not from the market-data
providers. Admin powers (`mint_authority_active`, `pausable`, `blacklist_capability`) are tri-state: `True` (capability
present, admin live) vetoes when configured to; `False` (absent, or capability present but renounced) passes; `None`
(capability detected in bytecode but the admin model itself — e.g. no `owner()`) could not be verified) now WARNs
rather than silently passing. `sell_simulation_ok` comes from a holder-transfer probe, not a router/pool simulation —
see `ContractInfo.sell_check_method`; a passing check is flagged `SELLABILITY_HEURISTIC_ONLY`, not treated as certain.
`mev_risk_score` is a bounded heuristic (`ContractInfo`-adjacent field on `MarketState`, method always `heuristic_v1`
today) derived from price impact and liquidity depth — not a mempool/bundle simulation.
