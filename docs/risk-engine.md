# Risk engine
Categories: CONTRACT, LIQUIDITY, HOLDER, CREATOR, MARKET, EXECUTION, MEV, PORTFOLIO. Any VETO => REJECT, regardless of AI or score.
Pipeline: strategy score -> risk pre-filter (before spending AI tokens) -> AI (qualified only) -> risk + wallet-policy re-check on the
final sized order -> signed approval. Effective limits = the stricter of app limits and wallet policy.
Entry-only vetoes (emergency stop, pauses, allowlists, daily loss, exposure, duplicates, cooldown) never block exits, so stop-loss,
take-profit, trailing and stagnation exits keep running during an emergency stop or AI outage.
Daily loss = realized today + unrealized losses only (open gains cannot mask a breach).
