import type { AgentStatus, Opportunity, Portfolio, Position, RiskLimits, RunnerInfo, WalletState } from "@/types/api";

export const limits = (o: Partial<RiskLimits> = {}): RiskLimits => ({
  max_trade_usdc: 25, max_position_usdc: 50, max_daily_loss_usdc: 50, max_total_exposure_usdc: 250, max_open_positions: 5, max_slippage_pct: 2,
  min_liquidity_usdc: 10000, max_chain_exposure_usdc: 250, max_launchpad_exposure_usdc: 100, max_trade_liquidity_ratio: 0.01, cooldown_seconds: 300,
  allowed_chains: ["arc"], allowed_launchpads: [], ...o,
});

export const runnerInfo = (o: Partial<RunnerInfo> = {}): RunnerInfo => ({
  id: "r1", name: "laptop", online: true, last_seen_at: new Date().toISOString(), version: "0.1.0", state: "STOPPED", applied_config_version: 3, desired_config_version: 3,
  wallet_provider: null, live: { available: false, reasons: ["LIVE is disabled locally on this runner (ARC_RUNNER_LIVE_ENABLED=false)"], provider: null, session_authorized: false, address: null, chain_verified: false, locally_enabled: false },
  local_ceilings: { max_trade_usdc: 25, max_position_usdc: 50, max_daily_loss_usdc: 50, max_open_positions: 5 }, entries_suspended_reason: null, last_error: null, created_at: "2026-09-19T00:00:00Z", revoked_at: null, ...o,
});

export const agent = (o: Partial<AgentStatus> = {}): AgentStatus => ({
  state: "STOPPED", desired_state: "STOPPED", mode: "PAPER", mode_label: "PAPER MODE", data_source: "DEMO DATA", data_status: "ok", emergency_stop: false, global_pause: true,
  ai: { mode: "DISABLED", provider: null, model: null }, strategy: { id: "traction_momentum", version: 1 }, open_positions: 0,
  last_activity_at: null, last_decision: null, limits: limits(),
  live_blockers: ["LIVE_TRADING_ENABLED is false on the server", "Arc live integration (router/quote/swap/fill parsing) is not verified"],
  live_confirmation_phrase: "ENABLE LIVE TRADING", config_version: 3, applied_config_version: 3, runner: runnerInfo(), ...o,
});

export const wallet = (o: Partial<WalletState> = {}): WalletState => ({
  network: { chain: "arc", network: "mainnet", chain_id: 5042, explorer_url: "https://explorer.arc.io", health: { ok: false, block_number: null, detail: "rpc unavailable" },
    chain_params: { chainId: "0x13b2", chainName: "Arc", nativeCurrency: { name: "USDC", symbol: "USDC", decimals: 18 }, rpcUrls: ["https://rpc.mainnet.arc.io"], blockExplorerUrls: ["https://explorer.arc.io"] } },
  wallets: [],
  agent_wallet: { available: false, product: "Circle Agent Wallet (Circle CLI, @circle-fin/cli), operated by your Local Runner", reason: "Autonomous execution runs on YOUR Local Runner and stays disabled until verified.",
    documented: ["spending caps per-tx / daily / weekly / monthly in USDC"], not_documented: ["whether contract writes count toward the USDC caps"], constraints: [], decision_doc: "docs/local-runner.md" },
  wallet_options: [
    { id: "browser_wallet", label: "Browser wallet (EIP-1193), per-trade signing", status: "available", custody: "user", note: "You sign every trade." },
    { id: "circle_agent_wallet", label: "Circle Agent Wallet via Local Runner", status: "not_integrated", custody: "user", note: "Runs on your machine; disabled until verified." },
    { id: "circle_developer_controlled_wallet", label: "Circle Developer-Controlled Wallet", status: "forbidden", custody: "developer (custodial)", note: "Server-side custody." },
  ],
  execution_capability: { capability: "PAPER_ONLY", label: "Paper trading only", autonomous: false, detail: "Connect a wallet, set a policy and authorize per-trade signing." },
  authorization: null, policy: null, runner_wallet: null,
  usdc: { balance: null, view: "ERC-20, 6 decimals", address: "0x3600000000000000000000000000000000000000", note: "Connect a wallet to read the balance." },
  allocated_capital_usdc: null, available_trading_capital_usdc: 1000, capital_label: "PAPER (virtual USDC)", mode: "PAPER", live_blockers: ["No wallet with verified ownership"], ...o,
});

export const opp = (o: Partial<Opportunity> = {}): Opportunity => ({
  decision_id: "d1", token_key: "arc:0xde01", symbol: "DEMO-STRONG", chain: "arc", launchpad: null, age_seconds: 2400, price: 1.2345, market_cap: 740000, liquidity: 125000,
  volume_5m: 24000, unique_buyers_5m: 47, buy_sell_ratio: 3, holder_growth_pct: 14, top10_holder_pct: 30, creator_known: true, creator_sold_pct: 2, strategy_score: 82.5,
  risk_score: 0, ai_status: "NONE", final_action: "BUY", final_reason: "approved: qualified signal and risk approved", strategy_version: 1, mode: "PAPER", data_label: "DEMO DATA",
  at: "2026-09-19T12:00:00Z", ...o,
});

export const position = (o: Partial<Position> = {}): Position => ({
  id: "p1", mode: "PAPER", chain: "arc", launchpad: null, token_address: "0xde01", symbol: "DEMO-STRONG", status: "OPEN", entry_price: 1, quantity: 24, initial_quantity: 24,
  last_price: 1.3, peak_price: 1.4, cost_basis_usdc: 25, realized_pnl_usdc: 0, unrealized_pnl_usdc: 6.2, gain_pct: 30, tiers_hit: [0], strategy_id: "traction_momentum",
  strategy_version: 1, decision_id: "d1", opened_at: "2026-09-19T12:00:00Z", closed_at: null, exit_reason: null, ...o,
});

export const portfolio = (o: Partial<Portfolio> = {}): Portfolio => ({
  mode: "PAPER", label: "PAPER", data_source: "DEMO DATA", cash_usdc: 975, exposure_usdc: 31, total_value_usdc: 1006.2, starting_cash_usdc: 1000, reported_by_runner: true, realized_pnl_usdc: 0,
  unrealized_pnl_usdc: 6.2, daily_pnl_usdc: -10, open_positions: 1, limits: limits(), snapshots: [], ...o,
});

import type { ChainInfo, DecisionDetail, Order, TokenDetail } from "@/types/api";

export const chain = (o: Partial<ChainInfo> = {}): ChainInfo => ({
  id: "arc", name: "Arc Mainnet", chain_id: 5042, enabled: true, paused: false, live_trading_verified: false, network: "mainnet", explorer_url: "https://explorer.arc.io",
  network_status: { ok: true, block: 123456, detail: "" }, deployments: null, ...o,
});

export const order = (o: Partial<Order> = {}): Order => ({
  id: "o1", decision_id: "d1", mode: "PAPER", side: "BUY", token_address: "0xde01", status: "FILLED", simulated: true, label: "PAPER (SIMULATED)", tx_hash: null, explorer_url: null,
  requested_amount_usdc: 25, filled_quantity: 24, avg_price: 1.0015, fee_usdc: 0.075, slippage_pct: 0.15, error: null, at: new Date().toISOString(), ...o,
});

export const decision = (o: Partial<DecisionDetail> = {}): DecisionDetail => ({
  decision: { id: "d1", at: "2026-09-19T12:00:00Z", mode: "PAPER", token_key: "arc:0xde04", final_action: "REJECT", final_reason: "RISK_VETO: TOP10_CONCENTRATION", data_label: "DEMO DATA" },
  what_the_agent_saw: { price: 1.5, liquidity: 120000, volume_5m: 24000, unique_buyers_5m: 47, top10_holder_pct: 88, price_change_5m: 16 },
  strategy: { id: "traction_momentum", version: 1, config_snapshot: { min_score: 70 },
    signal: { score: 74.2, qualified: false, components: { momentum: 70, buyer_growth: 60, holder_distribution: null }, weights: {}, gates: { min_age: false, buy_sell_ratio: true }, reasons: ["gate failed: min_age"], data_gaps: ["holder_distribution"] } },
  risk: { limits: {}, controls: {}, wallet_policy: null, assessments: [{ stage: "PRE", decision: "REJECT", risk_score: 40, flags: [
    { rule: "TOP10_CONCENTRATION", category: "HOLDER_RISK", severity: "VETO", message: "top-10 holders too concentrated" },
    { rule: "CONTRACT_VERIFICATION_UNKNOWN", category: "CONTRACT_RISK", severity: "WARN", message: "contract verification status unknown" }] }] },
  ai: [], sized_amount_usdc: 25, execution: [], positions: [],
  events: [{ at: "2026-09-19T12:00:00Z", type: "DECISION_RECORDED", payload: {} }, { at: "2026-09-19T12:00:01Z", type: "BUY_REJECTED", payload: {} }], ...o,
});

export const tokenDetail = (o: Partial<TokenDetail> = {}): TokenDetail => ({
  token_key: "arc:0xde04", data_label: "DEMO DATA", price_series: [{ at: "2026-09-19T12:00:00Z", price: 1.0, liquidity: 120000 }],
  latest_market: { symbol: "DEMO-RUG", price: 1.5, market_cap: 900000, liquidity: 120000, holder_count: 260, top10_holder_pct: 88, creator_known: false, contract: { verified: true, mint_authority_active: false } },
  latest_decision: opp({ decision_id: "d1", token_key: "arc:0xde04", symbol: "DEMO-RUG", final_action: "REJECT" }), decision_history: [opp({ decision_id: "d1", final_action: "REJECT", final_reason: "RISK_VETO: TOP10_CONCENTRATION" })], ...o,
});
