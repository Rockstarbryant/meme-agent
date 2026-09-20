"""Control-plane settings. This server never trades and holds no wallet sessions, signing keys or AI keys."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    secret_key: SecretStr = SecretStr("")
    jwt_secret: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent"
    redis_url: str = "redis://localhost:6379/0"

    arc_network: Literal["mainnet", "testnet"] = "mainnet"
    # Unset (None) -> documented defaults for arc_network. Empty string -> deliberately no RPC configured.
    arc_rpc_url: str | None = None
    arc_rpc_fallback_urls: str = ""  # comma separated
    arc_ws_url: str | None = None
    arc_chain_id: int | None = None  # adapter verifies against eth_chainId at runtime
    arc_explorer_url: str | None = None
    arc_usdc_address: str | None = None  # ERC-20 view of USDC (6 decimals); default from the network module

    cors_origins: str = "http://localhost:3000"
    access_token_minutes: int = Field(60, ge=5, le=1440)
    min_password_length: int = Field(10, ge=8)
    registration_enabled: bool = True
    trust_proxy: bool = False  # honour X-Forwarded-For (set true behind Render/Vercel proxies)
    paper_starting_usdc: float = 1000.0

    paper_trading_enabled: bool = True
    live_trading_enabled: bool = False  # global kill switch: LIVE can only be activated from the UI when this is true

    risk_max_trade_usdc: float = 25.0
    risk_max_position_usdc: float = 50.0
    risk_max_daily_loss_usdc: float = 50.0
    risk_max_total_exposure_usdc: float = 250.0
    risk_max_open_positions: int = Field(5, ge=1)
    risk_max_slippage_pct: float = 2.0
    risk_min_liquidity_usdc: float = 10_000.0

    # Platform hard ceilings — no user may configure limits above these (shared-worker safety).
    platform_max_trade_usdc: float = 100.0
    platform_max_position_usdc: float = 250.0
    platform_max_daily_loss_usdc: float = 250.0
    platform_max_total_exposure_usdc: float = 1_000.0
    platform_max_open_positions: int = Field(20, ge=1)
    platform_max_slippage_pct: float = 5.0
    platform_min_liquidity_usdc: float = 5_000.0

    # Platform Privy app (wallet provisioning on API; signing on shared worker)
    privy_app_id: str = ""
    privy_app_secret: SecretStr | None = None
    privy_api_url: str = "https://api.privy.io/v1"
    # Shared cloud worker authenticates with this bearer token (not a user JWT)
    platform_worker_token: SecretStr | None = None
    cloud_managed_enabled: bool = False  # global gate for per-user Privy cloud path

    @field_validator("database_url")
    @classmethod
    def _async_url(cls, v: str) -> str:
        """Managed hosts hand out postgres:// URLs with sslmode=; SQLAlchemy+asyncpg needs postgresql+asyncpg:// and ssl=."""
        v = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", v)
        return v.replace("sslmode=", "ssl=")

    @model_validator(mode="after")
    def _arc_defaults(self) -> "Settings":
        from app.chains.arc.network import NETWORKS, USDC_ERC20_ADDRESS
        net = NETWORKS[self.arc_network]
        if self.arc_chain_id is None:
            self.arc_chain_id = net.chain_id
        elif self.arc_chain_id != net.chain_id:
            raise ValueError(f"ARC_CHAIN_ID={self.arc_chain_id} does not match ARC_NETWORK={self.arc_network} ({net.chain_id})")
        if self.arc_rpc_url is None:
            self.arc_rpc_url = net.rpc_url
        if self.arc_ws_url is None:
            self.arc_ws_url = net.ws_url
        if self.arc_explorer_url is None:
            self.arc_explorer_url = net.explorer_url
        if self.arc_usdc_address is None:
            self.arc_usdc_address = USDC_ERC20_ADDRESS
        return self

    @model_validator(mode="after")
    def _prod_guards(self) -> "Settings":
        if self.app_env == "production":
            if len(self.secret_key.get_secret_value()) < 32 or len(self.jwt_secret.get_secret_value()) < 32:
                raise ValueError("SECRET_KEY and JWT_SECRET must be >= 32 chars in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def arc_rpc_urls(self) -> list[str]:
        urls = [self.arc_rpc_url] if self.arc_rpc_url else []
        urls += [u.strip() for u in self.arc_rpc_fallback_urls.split(",") if u.strip()]
        return urls
