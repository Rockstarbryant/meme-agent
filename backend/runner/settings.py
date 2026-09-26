from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.chains.arc.network import NETWORKS, ArcNetwork


def _alias(name: str) -> AliasChoices:
    return AliasChoices(name, f"ARC_RUNNER_{name}")


class LocalCeilings(BaseModel):
    """HARD limits owned by the user's machine. A compromised control plane can never push the runner past these."""
    max_trade_usdc: float
    max_position_usdc: float
    max_daily_loss_usdc: float
    max_total_exposure_usdc: float
    max_open_positions: int
    max_slippage_pct: float
    min_liquidity_usdc: float
    allowed_chains: set[str]


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARC_RUNNER_", env_file=".env", extra="ignore")

    server_url: str = "http://localhost:8000"
    state_dir: Path = Path.home() / ".arc-runner"
    name: str = "runner"
    version: str = ""

    ceiling_max_trade_usdc: float = 25.0
    ceiling_max_position_usdc: float = 50.0
    ceiling_max_daily_loss_usdc: float = 50.0
    ceiling_max_total_exposure_usdc: float = 250.0
    ceiling_max_open_positions: int = 5
    ceiling_max_slippage_pct: float = 2.0
    ceiling_min_liquidity_usdc: float = 10_000.0
    ceiling_allowed_chains: str = "arc"

    live_enabled: bool = False  # second key: LIVE needs this AND the user's activation in the UI
    wallet_provider: Literal["none", "circle_agent_wallet", "privy"] = "none"
    circle_cli_path: str = "circle"
    circle_chain: str = "ARC"  # value for the CLI --chain flag; confirm with `circle blockchain list`
    circle_wallet_address: str = ""

    data_source: Literal["demo", "arc"] = "arc"  # demo = synthetic, labelled, PAPER only
    market_data_providers: str = Field("arc_rpc,uniswap_v4_rpc,geckoterminal,dexscreener", validation_alias=_alias("MARKET_DATA_PROVIDERS"))
    # arc_rpc and uniswap_v4_rpc are on-chain and always free; geckoterminal and
    # dexscreener are free public APIs used only for enrichment. bitquery is
    # optional and NOT in the default list — add it explicitly only if you have
    # a working subscription; see docs/market-data-failover.md.
    market_data_essential_providers: str = Field("arc_rpc,uniswap_v4_rpc", validation_alias=_alias("MARKET_DATA_ESSENTIAL_PROVIDERS"))
    market_data_max_tokens: int = Field(10, validation_alias=_alias("MARKET_DATA_MAX_TOKENS"))
    market_data_cache_s: float = Field(60.0, validation_alias=_alias("MARKET_DATA_CACHE_S"))
    market_data_timeout_s: float = Field(6.0, validation_alias=_alias("MARKET_DATA_TIMEOUT_S"))
    market_data_failure_threshold: int = Field(3, validation_alias=_alias("MARKET_DATA_FAILURE_THRESHOLD"))
    market_data_cooldown_s: float = Field(60.0, validation_alias=_alias("MARKET_DATA_COOLDOWN_S"))
    rpc_launch_scan_blocks: int = Field(43200, validation_alias=_alias("RPC_LAUNCH_SCAN_BLOCKS"))
    uniswap_v4_scan_blocks: int = Field(20000, validation_alias=_alias("UNISWAP_V4_SCAN_BLOCKS"))
    uniswap_v4_swap_scan_blocks: int = Field(1800, validation_alias=_alias("UNISWAP_V4_SWAP_SCAN_BLOCKS"))
    geckoterminal_base_url: str = Field("https://api.geckoterminal.com/api/v2", validation_alias=_alias("GECKOTERMINAL_BASE_URL"))
    geckoterminal_network: str = Field("arc", validation_alias=_alias("GECKOTERMINAL_NETWORK"))
    geckoterminal_api_key: SecretStr | None = Field(None, validation_alias=_alias("GECKOTERMINAL_API_KEY"))
    # Free, no-API-key enrichment fallback (https://docs.dexscreener.com/api/reference).
    # Coverage of a brand-new chain like Arc depends on DexScreener's own indexers;
    # if it isn't indexed yet this provider simply contributes nothing (see its docstring).
    dexscreener_chain_id: str = Field("arc", validation_alias=_alias("DEXSCREENER_CHAIN_ID"))
    dexscreener_cache_s: float = Field(60.0, validation_alias=_alias("DEXSCREENER_CACHE_S"))
    arc_network: Literal["mainnet", "testnet"] = "mainnet"
    arc_rpc_url: str | None = None
    arc_rpc_fallback_urls: str = ""
    bitquery_api_key: SecretStr | None = Field(None, validation_alias=_alias("BITQUERY_API_KEY"))
    bitquery_endpoint: str = Field("https://streaming.bitquery.io/graphql", validation_alias=_alias("BITQUERY_ENDPOINT"))
    uniswap_api_key: SecretStr | None = Field(None, validation_alias=_alias("UNISWAP_API_KEY"))
    uniswap_api_url: str = Field("https://trade-api.gateway.uniswap.org/v1", validation_alias=_alias("UNISWAP_API_URL"))
    circle_allow_contract_execute: bool = Field(False, validation_alias=_alias("CIRCLE_ALLOW_CONTRACT_EXECUTE"))
    circle_require_spending_policy: bool = Field(True, validation_alias=_alias("CIRCLE_REQUIRE_SPENDING_POLICY"))
    # Explicit acknowledgement that Circle CLI schemas + Permit2 + fill reconciliation
    # have been verified on THIS machine. Default false = fail-closed LIVE.
    live_trading_verified: bool = Field(False, validation_alias=_alias("LIVE_TRADING_VERIFIED"))

    # Privy server wallets (cloud LIVE — no local CLI)
    privy_app_id: str = Field("", validation_alias=_alias("PRIVY_APP_ID"))
    privy_app_secret: SecretStr | None = Field(None, validation_alias=_alias("PRIVY_APP_SECRET"))
    privy_wallet_id: str = Field("", validation_alias=_alias("PRIVY_WALLET_ID"))
    privy_wallet_address: str = Field("", validation_alias=_alias("PRIVY_WALLET_ADDRESS"))
    privy_api_url: str = Field("https://api.privy.io/v1", validation_alias=_alias("PRIVY_API_URL"))
    privy_caip2: str = Field("eip155:5042", validation_alias=_alias("PRIVY_CAIP2"))  # Arc mainnet
    privy_allow_execute: bool = Field(False, validation_alias=_alias("PRIVY_ALLOW_EXECUTE"))
    # Withdrawing user funds out of the Privy-managed wallet is a distinct, separately
    # gated capability from trade execution: fail-closed by default like everything else.
    privy_allow_withdraw: bool = Field(False, validation_alias=_alias("PRIVY_ALLOW_WITHDRAW"))

    max_offline_s: float = 60.0  # dead-man switch: no control-plane contact this long => no NEW entries
    poll_wait_s: int = 20
    heartbeat_interval_s: float = 5.0
    upload_interval_s: float = 1.0
    discovery_interval_s: float = 15.0
    monitor_interval_s: float = 5.0
    snapshot_interval_s: float = 60.0
    market_snapshot_every_s: float = 15.0  # how often a token's market state is reported for the price chart
    reevaluate_after_s: float = 60.0
    paper_starting_usdc: float = 1000.0

    # AI provider settings live ONLY on the runner (same names build_provider expects).
    llm_provider: Literal["none", "openrouter", "anthropic", "openai"] = Field("none", validation_alias=_alias("LLM_PROVIDER"))
    ai_model: str = Field("", validation_alias=_alias("AI_MODEL"))
    openrouter_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENROUTER_API_KEY"))
    anthropic_api_key: SecretStr | None = Field(None, validation_alias=_alias("ANTHROPIC_API_KEY"))
    openai_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENAI_API_KEY"))
    openai_base_url: str = Field("https://api.openai.com/v1", validation_alias=_alias("OPENAI_BASE_URL"))

    @property
    def ceilings(self) -> LocalCeilings:
        return LocalCeilings(max_trade_usdc=self.ceiling_max_trade_usdc, max_position_usdc=self.ceiling_max_position_usdc,
                             max_daily_loss_usdc=self.ceiling_max_daily_loss_usdc, max_total_exposure_usdc=self.ceiling_max_total_exposure_usdc,
                             max_open_positions=self.ceiling_max_open_positions, max_slippage_pct=self.ceiling_max_slippage_pct,
                             min_liquidity_usdc=self.ceiling_min_liquidity_usdc,
                             allowed_chains={c.strip().lower() for c in self.ceiling_allowed_chains.split(",") if c.strip()})

    @property
    def network(self) -> ArcNetwork:
        return NETWORKS[self.arc_network]

    @property
    def rpc_urls(self) -> list[str]:
        first = self.arc_rpc_url if self.arc_rpc_url is not None else self.network.rpc_url
        return ([first] if first else []) + [u.strip() for u in self.arc_rpc_fallback_urls.split(",") if u.strip()]


def apply_ceilings(limits, c: LocalCeilings):
    """effective = the STRICTER of what the control plane asked for and what this machine allows."""
    return limits.model_copy(update={
        "max_trade_usdc": min(limits.max_trade_usdc, c.max_trade_usdc), "max_position_usdc": min(limits.max_position_usdc, c.max_position_usdc),
        "max_daily_loss_usdc": min(limits.max_daily_loss_usdc, c.max_daily_loss_usdc),
        "max_total_exposure_usdc": min(limits.max_total_exposure_usdc, c.max_total_exposure_usdc),
        "max_open_positions": min(limits.max_open_positions, c.max_open_positions),
        "max_slippage_pct": min(limits.max_slippage_pct, c.max_slippage_pct),
        "min_liquidity_usdc": max(limits.min_liquidity_usdc, c.min_liquidity_usdc),
        "allowed_chains": limits.allowed_chains & c.allowed_chains,
        "max_chain_exposure_usdc": min(limits.max_chain_exposure_usdc, c.max_total_exposure_usdc),
        "max_launchpad_exposure_usdc": min(limits.max_launchpad_exposure_usdc, c.max_total_exposure_usdc)})


# ---- local credential files (never leave this machine except the runner token, which only talks to the control plane)
def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def save_credentials(state_dir: Path, server_url: str, runner_id: str, token: str) -> Path:
    p = state_dir / "credentials.json"
    _private_write(p, json.dumps({"server_url": server_url, "runner_id": runner_id, "token": token}))
    return p


def load_credentials(state_dir: Path) -> dict | None:
    p = state_dir / "credentials.json"
    return json.loads(p.read_text()) if p.exists() else None


def approval_secret(state_dir: Path) -> str:
    """Trade-approval HMAC key: generated locally, never sent anywhere."""
    p = state_dir / "approval.key"
    if not p.exists():
        _private_write(p, secrets.token_hex(32))
    return p.read_text().strip()
