from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.errors import IntegrationNotVerified, PaperWalletCannotSubmit
from app.core.types import WalletCapability
from app.domain.trade import UnsignedTransaction
from app.wallets.base import (SubmissionResult, WalletAccount, WalletAuthorization, WalletProvider)


class PaperWalletProvider(WalletProvider):
    """Virtual wallet. It cannot submit transactions, by construction."""

    name = "paper"
    verified = True

    def __init__(self, virtual_usdc: float):
        self._bal = virtual_usdc

    def capabilities(self): return {WalletCapability.PAPER_ONLY}
    async def live_readiness(self):
        from app.wallets.base import LiveReadiness
        return LiveReadiness(available=False, provider=self.name, reasons=["paper wallet cannot submit transactions"])

    async def get_account(self): return WalletAccount(id="paper", provider=self.name, address="PAPER", chain="paper", kind="paper")
    async def get_usdc_balance(self): return self._bal
    async def get_authorization(self): return None
    async def revoke(self): return None

    async def submit(self, tx, now, expected_chain_id=None):  # type: ignore[override]
        raise PaperWalletCannotSubmit("paper wallets never submit blockchain transactions")

    async def _submit(self, tx, auth):
        raise PaperWalletCannotSubmit("paper wallets never submit blockchain transactions")


class SigningRequest(BaseModel):
    id: str
    tx: UnsignedTransaction
    created_at: datetime
    tx_hash: str | None = None
    status: str = "PENDING"  # PENDING | SIGNED | REJECTED


class BrowserWalletProvider(WalletProvider):
    """User-controlled browser wallet. The backend NEVER signs: it queues a request the frontend
    wallet must approve (eth_sendTransaction), then records the hash the user's wallet returns.
    """

    name = "browser_wallet"
    verified = True

    def __init__(self, account: WalletAccount, authorization: WalletAuthorization | None, balance_reader):
        self._account, self._auth, self._read_balance = account, authorization, balance_reader
        self.requests: dict[str, SigningRequest] = {}

    def capabilities(self): return {WalletCapability.PER_TRADE_SIGNING}
    async def get_account(self): return self._account
    async def get_usdc_balance(self): return await self._read_balance(self._account.address)
    async def get_authorization(self): return self._auth

    async def revoke(self):
        if self._auth:
            self._auth = self._auth.model_copy(update={"revoked_at": datetime.now(self._auth.granted_at.tzinfo)})

    async def _submit(self, tx: UnsignedTransaction, auth: WalletAuthorization) -> SubmissionResult:
        rid = uuid.uuid4().hex
        self.requests[rid] = SigningRequest(id=rid, tx=tx, created_at=datetime.now(auth.granted_at.tzinfo))
        return SubmissionResult(status="PENDING_SIGNATURE", signing_request_id=rid)

    def record_signed(self, request_id: str, tx_hash: str) -> SigningRequest:
        req = self.requests[request_id]
        req.tx_hash, req.status = tx_hash, "SIGNED"
        return req


class AgentWalletProvider(WalletProvider):
    """Circle Agent Wallet: real product, NOT integrated (deliberately).

    Evidence (Circle's official skills repo, read 2026-09-19): it is driven through a user's own `circle` CLI session
    (email + OTP), supports `circle wallet execute` for arbitrary contract writes, and USDC spending caps that only the
    human can set via OTP (mainnet only). Undocumented: whether swaps count toward caps, router allowlists, slippage,
    server-side enforcement, and any hosted multi-user API. Running many users' sessions in this backend would make it a
    delegate of their funds, which is a custody-model decision reserved to the project owner. See docs/circle-agent-wallet.md.
    """

    name = "agent_wallet"
    verified = False
    _MSG = "Circle/Arc agent-wallet & delegation API"
    _REQ = "Verify against current official docs, then implement _submit/get_authorization/revoke."

    def capabilities(self): return set()

    async def live_readiness(self):
        from app.wallets.base import LiveReadiness
        return LiveReadiness(available=False, provider=self.name, reasons=["hosted agent-wallet provider is not supported: use the Local Runner"])

    async def get_account(self): raise IntegrationNotVerified(self._MSG, self._REQ)
    async def get_usdc_balance(self): raise IntegrationNotVerified(self._MSG, self._REQ)
    async def get_authorization(self): raise IntegrationNotVerified(self._MSG, self._REQ)
    async def revoke(self): raise IntegrationNotVerified(self._MSG, self._REQ)
    async def _submit(self, tx, auth): raise IntegrationNotVerified(self._MSG, self._REQ)
