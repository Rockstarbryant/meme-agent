"""Wallet abstraction. The execution engine talks to this, never to raw keys."""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.errors import IntegrationNotVerified, PolicyViolationError
from app.core.types import WalletCapability
from app.domain.trade import UnsignedTransaction


class WalletAccount(BaseModel):
    id: str
    provider: str
    address: str
    chain: str
    kind: str  # "browser" | "agent" | "external_signer" | "paper"


class WalletPolicy(BaseModel):
    allocated_capital_usdc: float
    max_trade_usdc: float
    max_position_usdc: float
    max_daily_loss_usdc: float
    max_open_positions: int
    max_slippage_pct: float
    min_liquidity_usdc: float
    allowed_chains: set[str] = Field(default_factory=lambda: {"arc"})
    allowed_launchpads: set[str] = Field(default_factory=set)
    allowed_routers: set[str] = Field(default_factory=set)  # empty == deny all
    allowed_functions: set[str] = Field(default_factory=set)  # empty == no extra restriction; else exact ABI signatures


class WalletAuthorization(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    wallet_id: str
    provider: str
    capability: WalletCapability
    policy: WalletPolicy
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    proof_ref: str | None = None  # provider-side reference (delegation id / permission id)
    # Multi-tenant binding (optional on single-user runners; required on shared workers)
    user_id: str | None = None
    wallet_address: str | None = None
    policy_version: int | None = None

    def is_active(self, now: datetime) -> bool:
        if self.revoked_at is not None and self.revoked_at <= now:
            return False
        return self.expires_at is None or now < self.expires_at


class WalletStatus(BaseModel):
    """What the UI must tell the user about the current wallet configuration."""

    capability: WalletCapability
    label: str
    autonomous: bool
    detail: str


class PolicyValidator:
    """Deterministic policy check applied by the wallet layer on every submission.

    For multi-tenant (shared worker) deployments, pass ``expected_user_id`` /
    ``expected_wallet_address`` so a token for user A can never authorize a
    trade bound to user B's wallet.
    """

    @staticmethod
    def violations(
        tx: UnsignedTransaction,
        auth: WalletAuthorization | None,
        now: datetime,
        expected_chain_id: int | None = None,
        *,
        expected_user_id: str | None = None,
        expected_wallet_id: str | None = None,
        expected_wallet_address: str | None = None,
    ) -> list[str]:
        if auth is None:
            return ["NO_AUTHORIZATION"]
        v: list[str] = []
        p = auth.policy
        if not auth.is_active(now):
            v.append("AUTHORIZATION_INACTIVE_OR_REVOKED")
        # ---- per-user binding (shared worker isolation) ----
        if expected_user_id is not None and auth.user_id is not None and auth.user_id != expected_user_id:
            v.append("AUTH_USER_ID_MISMATCH")
        if expected_wallet_id is not None and auth.wallet_id != expected_wallet_id:
            v.append("AUTH_WALLET_ID_MISMATCH")
        if expected_wallet_address is not None and auth.wallet_address is not None:
            if auth.wallet_address.lower() != expected_wallet_address.lower():
                v.append("AUTH_WALLET_ADDRESS_MISMATCH")
        if tx.chain not in p.allowed_chains:
            v.append("CHAIN_NOT_IN_POLICY")
        if expected_chain_id is not None and tx.chain_id != expected_chain_id:
            v.append("CHAIN_ID_MISMATCH")
        if tx.to.lower() not in {r.lower() for r in p.allowed_routers}:
            v.append("ROUTER_NOT_ALLOWLISTED")
        if tx.amount_usdc > p.max_trade_usdc + 1e-9:
            v.append("EXCEEDS_MAX_TRADE")
        if tx.slippage_pct > p.max_slippage_pct + 1e-9:
            v.append("EXCEEDS_MAX_SLIPPAGE")
        if p.allowed_functions and (tx.function_signature or "") not in p.allowed_functions:
            v.append("FUNCTION_NOT_ALLOWLISTED")
        if tx.built_by != "deterministic-adapter":
            v.append("TX_NOT_BUILT_BY_ADAPTER")
        return v


class SubmissionResult(BaseModel):
    status: str  # "SUBMITTED" | "PENDING_SIGNATURE"
    tx_hash: str | None = None
    signing_request_id: str | None = None
    provider_tx_id: str | None = None


class LiveReadiness(BaseModel):
    """Can this wallet provider execute LIVE, unattended, right now? Every reason a provider cannot must be listed."""

    available: bool = False
    reasons: list[str] = Field(default_factory=list)
    provider: str = ""
    session_authorized: bool = False
    address: str | None = None


class WalletProvider(ABC):
    name: str
    verified: bool = False

    @abstractmethod
    def capabilities(self) -> set[WalletCapability]: ...

    @abstractmethod
    async def get_account(self) -> WalletAccount | None: ...

    @abstractmethod
    async def get_usdc_balance(self) -> float: ...

    @abstractmethod
    async def get_authorization(self) -> WalletAuthorization | None: ...

    @abstractmethod
    async def revoke(self) -> None: ...

    @abstractmethod
    async def _submit(self, tx: UnsignedTransaction, auth: WalletAuthorization) -> SubmissionResult: ...

    async def submit(self, tx: UnsignedTransaction, now: datetime, expected_chain_id: int | None = None) -> SubmissionResult:
        """Template method: policy is enforced HERE for every provider, before any signing."""
        auth = await self.get_authorization()
        bad = PolicyValidator.violations(tx, auth, now, expected_chain_id)
        if bad:
            raise PolicyViolationError(bad)
        assert auth is not None
        if auth.capability not in self.capabilities():
            raise PolicyViolationError(["CAPABILITY_NOT_SUPPORTED_BY_PROVIDER"])
        return await self._submit(tx, auth)

    async def live_readiness(self) -> LiveReadiness:
        return LiveReadiness(available=False, provider=self.name, reasons=[f"{self.name}: unattended LIVE execution is not supported"])

    async def withdraw_usdc(self, to_address: str, amount_usdc: float) -> str:
        """Move USDC out of this wallet to a user-controlled address.

        Fail-closed by default like every other sensitive capability in this
        project (LIVE trading, autonomous delegation): a provider must
        explicitly implement and gate this. Returns the chain tx hash.
        """
        raise IntegrationNotVerified(self.name, "withdrawals are not supported by this wallet provider")

    async def resolve_tx_hash(self, provider_tx_id: str) -> str | None:
        """Providers that return their own transaction id (not a chain hash) resolve it here; None = not known yet."""
        return None

    async def status(self) -> WalletStatus:
        caps = self.capabilities()
        if WalletCapability.AUTONOMOUS_DELEGATED in caps:
            return WalletStatus(capability=WalletCapability.AUTONOMOUS_DELEGATED, label="Autonomous delegated execution",
                                autonomous=True, detail="Agent may trade unattended within the policy.")
        if WalletCapability.PER_TRADE_SIGNING in caps:
            return WalletStatus(capability=WalletCapability.PER_TRADE_SIGNING, label="Explicit per-trade signing",
                                autonomous=False, detail="Every live trade needs your wallet signature.")
        return WalletStatus(capability=WalletCapability.PAPER_ONLY, label="Paper trading only",
                            autonomous=False, detail="No live wallet capability is available.")
