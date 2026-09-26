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

    live_enabled: bool = False
    wallet_provider: Literal["none", "circle_agent_wallet", "privy"] = "none"
    circle_cli_path: str = "circle"
    circle_chain: str = "ARC"
    circle_wallet_address: str = ""

    data_source: Literal["demo", "arc"] = "arc"

    # ------------------------------------------------------------------ providers
    # Order is provider *preference*: the registry tries them in the order they
    # are listed here when enriching a token. Discovery fans out to all of them.
    #
    # Defaults now favor free providers with real Arc coverage:
    #   dexpaprika     -> free, keyless 15/min, key 30/min, Arc supported
    #   goldsky        -> free Edge RPC, 20k-block eth_getLogs on Arc
    #   geckoterminal  -> free enrichment (rate limited)
    #   dexscreener    -> free enrichment (may not index Arc yet)
    #
    # arc_rpc and uniswap_v4_rpc still exist but now point at Goldsky instead
    # of Alchemy when you set ARC_RUNNER_ARC_RPC_URL to the Goldsky endpoint.
    market_data_providers: str = Field(
        "dexpaprika,goldsky,geckoterminal,dexscreener",
        validation_alias=_alias("MARKET_DATA_PROVIDERS"),
    )
    market_data_essential_providers: str = Field(
        "dexpaprika",
        validation_alias=_alias("MARKET_DATA_ESSENTIAL_PROVIDERS"),
    )
    market_data_max_tokens: int = Field(20, validation_alias=_alias("MARKET_DATA_MAX_TOKENS"))
    market_data_cache_s: float = Field(120.0, validation_alias=_alias("MARKET_DATA_CACHE_S"))
    market_data_timeout_s: float = Field(6.0, validation_alias=_alias("MARKET_DATA_TIMEOUT_S"))
    market_data_failure_threshold: int = Field(3, validation_alias=_alias("MARKET_DATA_FAILURE_THRESHOLD"))
    market_data_cooldown_s: float = Field(60.0, validation_alias=_alias("MARKET_DATA_COOLDOWN_S"))

    # ------------------------------------------------------------------ dexpaprika
    dexpaprika_base_url: str = Field("https://api.dexpaprika.com", validation_alias=_alias("DEXPAPRIKA_BASE_URL"))
    dexpaprika_network: str = Field("arc", validation_alias=_alias("DEXPAPRIKA_NETWORK"))
    dexpaprika_api_key: SecretStr | None = Field(None, validation_alias=_alias("DEXPAPRIKA_API_KEY"))
    dexpaprika_min_txns_24h: int = Field(1, validation_alias=_alias("DEXPAPRIKA_MIN_TXNS_24H"))
    dexpaprika_min_volume_24h_usd: float = Field(100.0, validation_alias=_alias("DEXPAPRIKA_MIN_VOLUME_24H_USD"))
    dexpaprika_lookback_hours: int = Field(24, validation_alias=_alias("DEXPAPRIKA_LOOKBACK_HOURS"))

    # ------------------------------------------------------------------ goldsky
    # When set, ARC_RUNNER_ARC_RPC_URL is expected to point at Goldsky and the
    # old scan-window settings below become irrelevant (Goldsky allows 20k).
    goldsky_api_key: SecretStr | None = Field(None, validation_alias=_alias("GOLDSKY_API_KEY"))
    goldsky_arc_rpc_url: str = Field(
        "https://edge.goldsky.com/standard/evm/5042",
        validation_alias=_alias("GOLDSKY_ARC_RPC_URL"),
    )
    # If true, build_goldsky_rpc() will be used automatically and Alchemy is
    # appended as a fallback. Set ARC_RUNNER_ARC_RPC_URL= (empty) to disable.
    prefer_goldsky_rpc: bool = Field(True, validation_alias=_alias("PREFER_GOLDSKY_RPC"))

    # ------------------------------------------------------------------ legacy RPC
    rpc_launch_scan_blocks: int = Field(10000, validation_alias=_alias("RPC_LAUNCH_SCAN_BLOCKS"))
    uniswap_v4_scan_blocks: int = Field(5000, validation_alias=_alias("UNISWAP_V4_SCAN_BLOCKS"))
    uniswap_v4_swap_scan_blocks: int = Field(1800, validation_alias=_alias("UNISWAP_V4_SWAP_SCAN_BLOCKS"))

    # ------------------------------------------------------------------ gecko
    geckoterminal_base_url: str = Field("https://api.geckoterminal.com/api/v2", validation_alias=_alias("GECKOTERMINAL_BASE_URL"))
    geckoterminal_network: str = Field("arc", validation_alias=_alias("GECKOTERMINAL_NETWORK"))
    geckoterminal_api_key: SecretStr | None = Field(None, validation_alias=_alias("GECKOTERMINAL_API_KEY"))

    # ------------------------------------------------------------------ dexscreener
    dexscreener_chain_id: str = Field("arc", validation_alias=_alias("DEXSCREENER_CHAIN_ID"))
    dexscreener_cache_s: float = Field(60.0, validation_alias=_alias("DEXSCREENER_CACHE_S"))

    # ------------------------------------------------------------------ network
    arc_network: Literal["mainnet", "testnet"] = "mainnet"
    arc_rpc_url: str | None = None
    arc_rpc_fallback_urls: str = ""

    # ------------------------------------------------------------------ paid providers (optional)
    bitquery_api_key: SecretStr | None = Field(None, validation_alias=_alias("BITQUERY_API_KEY"))
    bitquery_endpoint: str = Field("https://streaming.bitquery.io/graphql", validation_alias=_alias("BITQUERY_ENDPOINT"))
    uniswap_api_key: SecretStr | None = Field(None, validation_alias=_alias("UNISWAP_API_KEY"))
    uniswap_api_url: str = Field("https://trade-api.gateway.uniswap.org/v1", validation_alias=_alias("UNISWAP_API_URL"))

    # ------------------------------------------------------------------ live trading gates
    circle_allow_contract_execute: bool = Field(False, validation_alias=_alias("CIRCLE_ALLOW_CONTRACT_EXECUTE"))
    circle_require_spending_policy: bool = Field(True, validation_alias=_alias("CIRCLE_REQUIRE_SPENDING_POLICY"))
    live_trading_verified: bool = Field(False, validation_alias=_alias("LIVE_TRADING_VERIFIED"))

    privy_app_id: str = Field("", validation_alias=_alias("PRIVY_APP_ID"))
    privy_app_secret: SecretStr | None = Field(None, validation_alias=_alias("PRIVY_APP_SECRET"))
    privy_wallet_id: str = Field("", validation_alias=_alias("PRIVY_WALLET_ID"))
    privy_wallet_address: str = Field("", validation_alias=_alias("PRIVY_WALLET_ADDRESS"))
    privy_api_url: str = Field("https://api.privy.io/v1", validation_alias=_alias("PRIVY_API_URL"))
    privy_caip2: str = Field("eip155:5042", validation_alias=_alias("PRIVY_CAIP2"))
    privy_allow_execute: bool = Field(False, validation_alias=_alias("PRIVY_ALLOW_EXECUTE"))
    privy_allow_withdraw: bool = Field(False, validation_alias=_alias("PRIVY_ALLOW_WITHDRAW"))

    # ------------------------------------------------------------------ timing
    max_offline_s: float = 60.0
    poll_wait_s: int = 20
    heartbeat_interval_s: float = 5.0
    upload_interval_s: float = 1.0
    discovery_interval_s: float = 15.0
    monitor_interval_s: float = 5.0
    snapshot_interval_s: float = 60.0
    market_snapshot_every_s: float = 15.0
    reevaluate_after_s: float = 60.0
    paper_starting_usdc: float = 1000.0

    # ------------------------------------------------------------------ AI
    llm_provider: Literal["none", "openrouter", "anthropic", "openai"] = Field("none", validation_alias=_alias("LLM_PROVIDER"))
    ai_model: str = Field("", validation_alias=_alias("AI_MODEL"))
    openrouter_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENROUTER_API_KEY"))
    anthropic_api_key: SecretStr | None = Field(None, validation_alias=_alias("ANTHROPIC_API_KEY"))
    openai_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENAI_API_KEY"))
    openai_base_url: str = Field("https://api.openai.com/v1", validation_alias=_alias("OPENAI_BASE_URL"))

    @property
    def ceilings(self) -> LocalCeilings:
        return LocalCeilings(
            max_trade_usdc=self.ceiling_max_trade_usdc, max_position_usdc=self.ceiling_max_position_usdc,
            max_daily_loss_usdc=self.ceiling_max_daily_loss_usdc, max_total_exposure_usdc=self.ceiling_max_total_exposure_usdc,
            max_open_positions=self.ceiling_max_open_positions, max_slippage_pct=self.ceiling_max_slippage_pct,
            min_liquidity_usdc=self.ceiling_min_liquidity_usdc,
            allowed_chains={c.strip().lower() for c in self.ceiling_allowed_chains.split(",") if c.strip()},
        )

    @property
    def network(self) -> ArcNetwork:
        return NETWORKS[self.arc_network]

    @property
    def rpc_urls(self) -> list[str]:
        first = self.arc_rpc_url if self.arc_rpc_url is not None else self.network.rpc_url
        return ([first] if first else []) + [u.strip() for u in self.arc_rpc_fallback_urls.split(",") if u.strip()]


def apply_ceilings(limits, c: LocalCeilings):
    return limits.model_copy(update={
        "max_trade_usdc": min(limits.max_trade_usdc, c.max_trade_usdc),
        "max_position_usdc": min(limits.max_position_usdc, c.max_position_usdc),
        "max_daily_loss_usdc": min(limits.max_daily_loss_usdc, c.max_daily_loss_usdc),
        "max_total_exposure_usdc": min(limits.max_total_exposure_usdc, c.max_total_exposure_usdc),
        "max_open_positions": min(limits.max_open_positions, c.max_open_positions),
        "max_slippage_pct": min(limits.max_slippage_pct, c.max_slippage_pct),
        "min_liquidity_usdc": max(limits.min_liquidity_usdc, c.min_liquidity_usdc),
        "allowed_chains": limits.allowed_chains & c.allowed_chains,
        "max_chain_exposure_usdc": min(limits.max_chain_exposure_usdc, c.max_total_exposure_usdc),
        "max_launchpad_exposure_usdc": min(limits.max_launchpad_exposure_usdc, c.max_total_exposure_usdc),
    })


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
    p = state_dir / "approval.key"
    if not p.exists():
        _private_write(p, secrets.token_hex(32))
    return p.read_text().strip()