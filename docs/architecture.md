# Architecture
See docs/local-runner.md for the full picture.

Control plane (`backend/app`): auth, policy/strategy/limits config with a monotonically increasing config version, runner pairing,
long-poll config delivery, heartbeats, command queue, event ingestion, read APIs, SSE to the UI. Storage: PostgreSQL + Redis.
Local Runner (`backend/runner`): MarketDataProvider -> MarketState -> TractionMomentum -> RiskEngine (veto) -> AIAnalyzer (qualified only)
-> DecisionPipeline (sizing, re-check, signed approval) -> TradingEngine -> ExecutionAdapter (Paper | ArcExecutionEngine) -> WalletProvider.
Exits: PositionManager (deterministic) -> TradingEngine. Everything is emitted as events into a durable outbox and reported to the control plane.
The trading core (`app/strategies|risk|portfolio|execution|wallets|ai|chains|events`) is shared code with no web/DB dependencies;
the runner imports only that core (enforced by a test that blocks FastAPI/SQLAlchemy/Redis at import time).
BNB, Solana and Robinhood Chain remain extension points (ChainAdapter / MarketDataProvider / LaunchpadAdapter), not integrations.


### Multi-source market data

Market data is provider-agnostic and normalized through `MarketDataRegistry`. Arc RPC owns launchpad provenance; GeckoTerminal can enrich DEX market fields; Bitquery remains optional fallback. Provider failures are isolated by a circuit breaker. The trading engine never receives fabricated values, and the execution quote still comes from the verified execution adapter rather than a market-data aggregator.

### Enrichment layer

`app/enrichment/` sits between the raw market-data providers and the strategy/risk engine (`EnrichingMarketDataProvider`
wraps `self.market_data` in both `runner/runtime.py` and `app/discovery/bootstrap.py::GlobalPipeline`). It fills what
no market-data provider supplies: contract verification and sellability, top-holder concentration and growth, creator
identity, launchpad provenance, and a MEV-exposure heuristic. See docs/enrichment.md.
