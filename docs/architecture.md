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
