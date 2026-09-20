export type Mode = "PAPER" | "LIVE";
export type Action = "BUY" | "WATCH" | "REJECT" | "HOLD" | "SELL";

export interface User { id: string; email: string; mode: Mode; created_at: string }
export interface AuthResponse { access_token: string; token_type: string; expires_in: number; user: User }

export interface RiskLimits {
  max_trade_usdc: number; max_position_usdc: number; max_daily_loss_usdc: number; max_total_exposure_usdc: number;
  max_open_positions: number; max_slippage_pct: number; min_liquidity_usdc: number;
  max_chain_exposure_usdc: number; max_launchpad_exposure_usdc: number; max_trade_liquidity_ratio: number;
  cooldown_seconds: number; allowed_chains: string[]; allowed_launchpads: string[];
}

export type AgentState = "RUNNING" | "PAUSED" | "STOPPED" | "OFFLINE" | "STARTING" | "LIVE_BLOCKED";
export interface LiveStatus {
  available: boolean; reasons: string[]; provider: string | null; session_authorized: boolean; address: string | null;
  chain_verified: boolean; locally_enabled: boolean;
}
export interface RunnerInfo {
  id: string; name: string; online: boolean; last_seen_at: string | null; version: string; state: string | null;
  applied_config_version: number; desired_config_version: number; wallet_provider: string | null; live: LiveStatus | null;
  local_ceilings: Record<string, unknown>; entries_suspended_reason: string | null; last_error: string | null;
  created_at?: string; revoked_at?: string | null;
}

export interface LastDecision { id: string; token: string; symbol: string | null; action: Action; reason: string; at: string }
export interface AgentStatus {
  state: AgentState; desired_state: "RUNNING" | "PAUSED" | "STOPPED"; mode: Mode; mode_label: string; data_source: string; data_status: string;
  emergency_stop: boolean; global_pause: boolean; ai: { mode: string; provider: string | null; model: string | null };
  strategy: { id: string; version: number }; open_positions: number; last_activity_at: string | null;
  last_decision: LastDecision | null; limits: RiskLimits; live_blockers: string[]; live_confirmation_phrase: string;
  config_version: number; applied_config_version: number; runner: RunnerInfo | null;
}

export interface Snapshot { at: string; total_value_usdc: number; daily_pnl_usdc: number }
export interface Portfolio {
  mode: Mode; label: string; data_source: string; cash_usdc: number; exposure_usdc: number; total_value_usdc: number;
  starting_cash_usdc: number; reported_by_runner: boolean; realized_pnl_usdc: number; unrealized_pnl_usdc: number; daily_pnl_usdc: number;
  open_positions: number; limits: RiskLimits; snapshots: Snapshot[];
}

export interface Opportunity {
  decision_id: string; token_key: string; symbol: string | null; chain: string | null; launchpad: string | null;
  age_seconds: number | null; price: number | null; market_cap: number | null; liquidity: number | null;
  volume_5m: number | null; unique_buyers_5m: number | null; buy_sell_ratio: number | null;
  holder_growth_pct: number | null; top10_holder_pct: number | null; creator_known: boolean | null;
  creator_sold_pct: number | null; strategy_score: number; risk_score: number; ai_status: string;
  final_action: Action; final_reason: string; strategy_version: number; mode: Mode;
  data_label: "DEMO DATA" | "LIVE DATA"; at: string;
}

export interface Position {
  id: string; mode: Mode; chain: string; launchpad: string | null; token_address: string; symbol: string | null;
  status: "OPEN" | "CLOSED"; entry_price: number; quantity: number; initial_quantity: number; last_price: number;
  peak_price: number; cost_basis_usdc: number; realized_pnl_usdc: number; unrealized_pnl_usdc: number;
  gain_pct: number | null; tiers_hit: number[]; strategy_id: string; strategy_version: number; decision_id: string;
  opened_at: string; closed_at: string | null; exit_reason: string | null;
}

export interface Order {
  id: string; decision_id: string; mode: Mode; side: "BUY" | "SELL"; token_address: string; status: string;
  simulated: boolean; label: string; tx_hash: string | null; explorer_url: string | null;
  requested_amount_usdc: number | null; filled_quantity: number; avg_price: number | null; fee_usdc: number;
  slippage_pct: number | null; error: string | null; at: string;
}

export interface WalletOption { id: string; label: string; status: string; custody: string; note: string }
export interface WalletPolicy {
  version?: number; allocated_capital_usdc: number; max_trade_usdc: number; max_position_usdc: number;
  max_daily_loss_usdc: number; max_open_positions: number; max_slippage_pct: number; min_liquidity_usdc: number;
}
export interface ChainParams {
  chainId: string; chainName: string; nativeCurrency: { name: string; symbol: string; decimals: number };
  rpcUrls: string[]; blockExplorerUrls: string[];
}
export interface WalletState {
  network: { chain: string; network: string; chain_id: number; explorer_url: string | null; chain_params: ChainParams;
    health: { ok: boolean; block_number: number | null; detail: string } };
  wallets: { id: string; provider: string; address: string; ownership_verified: boolean }[];
  agent_wallet: { available: boolean; product: string; reason: string; documented: string[]; not_documented: string[];
    constraints: string[]; decision_doc: string };
  wallet_options: WalletOption[];
  cloud_wallet: { id: string; address: string; privy_wallet_id: string | null; active: boolean } | null;
  execution_mode: "self_hosted" | "cloud_managed";
  execution_capability: { capability: string; label: string; autonomous: boolean; detail: string };
  authorization: { id: string; capability: string; granted_at: string; expires_at: string | null } | null;
  policy: WalletPolicy | null;
  usdc: { balance: number | null; view: string; address: string; note: string };
  runner_wallet: { provider: string | null; online: boolean; live: LiveStatus | null } | null;
  allocated_capital_usdc: number | null; available_trading_capital_usdc: number | null; capital_label: string;
  mode: Mode; live_blockers: string[];
}
export interface SigningRequest {
  id: string; status: string; tx_hash: string | null; created_at: string;
  tx: { to: string; data: string; value: number; chain_id: number; amount_usdc: number; side: string };
}

export interface ChainInfo {
  id: string; name: string; chain_id: number; enabled: boolean; paused: boolean; live_trading_verified: boolean;
  network: string | null; explorer_url: string | null;
  network_status: { ok: boolean; block: number | null; detail: string } | null;
  deployments: { status: string; usable_for: string; addresses: Record<string, string> } | null;
}
export interface LaunchpadInfo { id: string; chain: string; name: string; verified: boolean; enabled: boolean; descriptor: Record<string, unknown> }

export interface StreamEvent { id: string; type: string; at: string; correlation_id: string; payload: Record<string, unknown> }
export interface AuditLog { at: string; actor: string; action: string; detail: Record<string, unknown> }
export interface StrategyVersion { version: number; scope: string; created_at: string; config: Record<string, unknown> }

export interface RiskFlag { rule: string; category: string; severity: "INFO" | "WARN" | "VETO"; message: string; value?: number | string | null; threshold?: number | string | null }
export interface DecisionDetail {
  decision: { id: string; at: string; mode: Mode; token_key: string; final_action: Action; final_reason: string; data_label: string };
  what_the_agent_saw: Record<string, unknown>;
  strategy: { id: string; version: number; config_snapshot: Record<string, unknown>;
    signal: { score: number; qualified: boolean; components: Record<string, number | null>; weights: Record<string, number>;
      gates: Record<string, boolean>; reasons: string[]; data_gaps: string[] } | null };
  risk: { limits: Record<string, unknown>; controls: Record<string, unknown>; wallet_policy: Record<string, unknown> | null;
    assessments: { stage: string; decision: string; risk_score: number; flags: RiskFlag[] }[] };
  ai: { provider: string; model: string; prompt_version: string; status: string; error: string;
    response: { action: Action; confidence: number; reasoning_summary: string; positive_signals: string[]; negative_signals: string[]; risk_flags: string[] } | null }[];
  sized_amount_usdc: number;
  execution: { order_id: string; status: string; simulated: boolean; tx_hash: string | null; avg_price: number | null; filled_quantity: number; fee_usdc: number; slippage_pct: number | null; error: string | null }[];
  positions: Position[];
  events: { at: string; type: string; payload: Record<string, unknown> }[];
}
export interface TokenDetail {
  token_key: string; latest_market: Record<string, unknown>; data_label: string;
  price_series: { at: string; price: number | null; liquidity: number | null }[];
  latest_decision: Opportunity | null; decision_history: Opportunity[];
}

export interface PairingCode { code: string; expires_in: number; command: string }
export interface StrategyListItem { id: string; name: string; description: string; enabled: boolean; active: boolean }
