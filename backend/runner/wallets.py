"""Wallet/delegation providers for the Local Runner.

The execution engine only sees `app.wallets.base.WalletProvider`. Adding a provider = implement that interface and
`register_wallet_provider(name, factory)`; strategy and risk code never change.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.core.errors import IntegrationNotVerified
from app.core.types import WalletCapability
from app.domain.trade import UnsignedTransaction
from app.wallets.base import LiveReadiness, SubmissionResult, WalletAccount, WalletAuthorization, WalletProvider
from runner.settings import RunnerSettings

# ------------------------------------------------------------------ registry
Factory = Callable[[RunnerSettings], "WalletProvider | None"]
_FACTORIES: dict[str, Factory] = {}


def register_wallet_provider(name: str, factory: Factory) -> None:
    _FACTORIES[name] = factory


def build_wallet_provider(s: RunnerSettings) -> WalletProvider | None:
    if s.wallet_provider not in _FACTORIES:
        raise ValueError(f"unknown wallet provider {s.wallet_provider!r}; registered: {sorted(_FACTORIES)}")
    return _FACTORIES[s.wallet_provider](s)


# ------------------------------------------------------------------ Circle CLI (argv construction verified against `circle ... --help`, CLI 1.1.3)
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\([A-Za-z0-9_,\[\]() ]*\)$")
_CHAIN = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_AMOUNT = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SECRETISH = re.compile(r"token|secret|otp|private|mnemonic|seed|password|session|cookie|authorization", re.I)


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str
    json: object | None = None


def _addr(a: str) -> str:
    if not _ADDR.match(a):
        raise ValueError("invalid address")
    return a


def _chain(c: str) -> str:
    if not _CHAIN.match(c):
        raise ValueError("invalid chain name")
    return c


def _param(p: str) -> str:
    if not p or p.startswith("-") or len(p) > 4096 or any(ord(ch) < 32 for ch in p):
        raise ValueError("unsafe ABI parameter")  # a leading '-' would be parsed as a CLI option
    return p


def redact(obj):
    """Strip secret-looking fields before anything is written to a capture file."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if _SECRETISH.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class CircleCli:
    """Thin, injection-safe wrapper. It NEVER runs login/logout/terms/limit-set/limit-reset: those need the human."""

    def __init__(self, path: str = "circle", timeout_s: float = 45.0, exec_fn: Callable[[list[str]], Awaitable[CliResult]] | None = None):
        self.path, self.timeout_s, self._exec = path, timeout_s, exec_fn

    # --- argv builders (no shell, every value validated) ---
    def status(self) -> list[str]: return ["wallet", "status", "--type", "agent", "--output", "json"]
    def blockchain_list(self) -> list[str]: return ["blockchain", "list", "--output", "json"]
    def wallet_list(self, chain: str) -> list[str]: return ["wallet", "list", "--chain", _chain(chain), "--type", "agent", "--output", "json"]
    def balance(self, address: str, chain: str) -> list[str]: return ["wallet", "balance", "--address", _addr(address), "--chain", _chain(chain), "--output", "json"]
    def limit(self, address: str, chain: str) -> list[str]: return ["wallet", "limit", "--address", _addr(address), "--chain", _chain(chain), "--output", "json"]
    def budget(self, address: str) -> list[str]: return ["wallet", "limit", "budget", "--address", _addr(address), "--output", "json"]

    def execute(self, signature: str, params: list[str], contract: str, address: str, chain: str, *, amount: str = "0",
                idempotency_key: str | None = None, estimate: bool = False) -> list[str]:
        if not _SIG.match(signature):
            raise ValueError("invalid ABI function signature")
        if not _AMOUNT.match(amount):
            raise ValueError("invalid native amount")
        argv = ["wallet", "execute", signature, *[_param(p) for p in params], "--contract", _addr(contract), "--address", _addr(address),
                "--chain", _chain(chain), "--amount", amount]
        if idempotency_key:
            if not _KEY.match(idempotency_key):
                raise ValueError("invalid idempotency key")
            argv += ["--idempotency-key", idempotency_key]
        if estimate:
            argv.append("--estimate")
        return argv + ["--output", "json"]

    async def run(self, args: list[str]) -> CliResult:
        if self._exec:
            return await self._exec([self.path, *args])
        env = {k: v for k, v in os.environ.items() if k != "CIRCLE_ACCEPT_TERMS"}  # never accept Circle's Terms on the user's behalf
        try:
            proc = await asyncio.create_subprocess_exec(self.path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except FileNotFoundError:
            return CliResult(127, "", f"{self.path}: not found")
        except asyncio.TimeoutError:
            proc.kill()
            return CliResult(124, "", "timed out")
        text = out[:1_000_000].decode(errors="replace")
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        return CliResult(proc.returncode or 0, text, err[-2000:].decode(errors="replace"), parsed)


class CircleAgentWalletProvider(WalletProvider):
    """Circle Agent Wallet through the user's local Circle CLI session.

    The provider is deliberately conservative: Circle CLI is the source of truth for
    session/balance/policy state, while the application supplies a stricter local
    WalletPolicy. OTP-gated policy changes are never automated here.
    """

    name = "circle_agent_wallet"
    verified = True

    def __init__(self, cli: CircleCli, address: str, chain: str, *, allow_contract_execute: bool = False,
                 require_spending_policy: bool = True):
        self.cli, self.address, self.chain = cli, address, chain
        self.allow_contract_execute = allow_contract_execute
        self.require_spending_policy = require_spending_policy
        self._policy = None
        self._status_cache: tuple[float, bool] | None = None
        self._limit_cache: tuple[float, dict] | None = None

    def configure_policy(self, policy):
        self._policy = policy

    def capabilities(self) -> set[WalletCapability]:
        return {WalletCapability.AUTONOMOUS_DELEGATED} if self.allow_contract_execute and self._policy else set()

    async def _read(self, args: list[str]) -> object | None:
        r = await self.cli.run(args)
        if r.returncode != 0:
            raise IntegrationNotVerified("Circle CLI", r.stderr[-500:] or r.stdout[-500:])
        return r.json

    @staticmethod
    def _find(obj, keys: set[str]):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in keys and v is not None:
                    return v
            for v in obj.values():
                x = CircleAgentWalletProvider._find(v, keys)
                if x is not None:
                    return x
        elif isinstance(obj, list):
            for v in obj:
                x = CircleAgentWalletProvider._find(v, keys)
                if x is not None:
                    return x
        return None

    @staticmethod
    def _find_number(obj, keys: set[str]):
        v = CircleAgentWalletProvider._find(obj, keys)
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None

    async def get_account(self):
        if not self.address:
            rows = await self._read(self.cli.wallet_list(self.chain))
            addr = self._find(rows, {"address", "walletaddress"})
            if addr:
                self.address = str(addr)
        return WalletAccount(id=self.address or "unset", provider=self.name, address=self.address, chain="arc", kind="agent") if self.address else None

    async def get_usdc_balance(self) -> float:
        if not self.address:
            await self.get_account()
        if not self.address:
            raise IntegrationNotVerified("Circle agent wallet", "no ARC agent wallet address configured")
        body = await self._read(self.cli.balance(self.address, self.chain))
        # Prefer an amount attached to the USDC token; otherwise accept a top-level balance.
        if isinstance(body, dict):
            data = body.get("data", body)
            rows = data.get("balances", data.get("balance", data)) if isinstance(data, dict) else data
        else:
            rows = body
        candidates = rows if isinstance(rows, list) else [rows]
        for row in candidates:
            if isinstance(row, dict):
                symbol = str(row.get("symbol") or row.get("tokenSymbol") or "").upper()
                token = str(row.get("token") or row.get("tokenAddress") or row.get("contractAddress") or "").lower()
                if symbol == "USDC" or token == "0x3600000000000000000000000000000000000000":
                    amt = row.get("amount") or row.get("balance") or row.get("value")
                    if amt is not None:
                        return float(amt)
        v = self._find_number(body, {"usdc", "amount"})
        if v is None:
            raise IntegrationNotVerified("Circle wallet balance schema", "could not identify the USDC amount")
        return v

    async def _policy_limits(self) -> dict:
        if not self.address:
            await self.get_account()
        if not self.address:
            return {}
        body = await self._read(self.cli.limit(self.address, self.chain))
        # Normalize common Circle CLI spellings while preserving raw values for debugging.
        return {
            "per_tx": self._find_number(body, {"per_tx", "pertx", "pertransaction", "pertransactionlimit"}),
            "daily": self._find_number(body, {"daily", "dailylimit"}),
            "weekly": self._find_number(body, {"weekly", "weeklylimit"}),
            "monthly": self._find_number(body, {"monthly", "monthlylimit"}),
            "raw": redact(body),
        }

    async def get_authorization(self) -> WalletAuthorization | None:
        if not self.address or not self._policy:
            return None
        if not self.allow_contract_execute:
            return None
        now = datetime.now().astimezone()
        limits = await self._policy_limits()
        if self.require_spending_policy and (limits.get("per_tx") is None or limits.get("daily") is None):
            return None
        # The application policy is always the stricter policy. Circle's own caps are
        # checked independently before execute and are never raised by this provider.
        return WalletAuthorization(
            wallet_id=self.address, provider=self.name, capability=WalletCapability.AUTONOMOUS_DELEGATED,
            policy=self._policy, granted_at=now, expires_at=None, proof_ref="circle-cli-local-session"
        )

    async def revoke(self) -> None:
        # No programmatic revoke is attempted: logout/limit reset are human OTP operations.
        raise IntegrationNotVerified("Circle Agent Wallet revoke", "run circle wallet logout or change the policy in your own terminal")

    def build_execute_argv(self, tx: UnsignedTransaction, *, estimate: bool = False) -> list[str]:
        if not tx.function_signature:
            raise ValueError("Circle CLI needs an ABI function signature + parameters")
        idem = hashlib.sha256(tx.data.encode()).hexdigest()[:32]
        return self.cli.execute(tx.function_signature, tx.params, tx.to, self.address, self.chain,
                                amount=str(tx.value), idempotency_key=idem, estimate=estimate)

    async def _submit(self, tx: UnsignedTransaction, auth: WalletAuthorization) -> SubmissionResult:
        if not self.allow_contract_execute:
            raise IntegrationNotVerified("Circle wallet.execute", "set ARC_RUNNER_CIRCLE_ALLOW_CONTRACT_EXECUTE=true only after reviewing the generated transaction")
        argv = self.build_execute_argv(tx)
        body = await self._read(argv)
        tx_id = self._find(body, {"transactionid", "transaction_id", "id"})
        tx_hash = self._find(body, {"txhash", "transactionhash", "transaction_hash", "hash"})
        if tx_hash:
            return SubmissionResult(status="SUBMITTED", tx_hash=str(tx_hash))
        if tx_id:
            return SubmissionResult(status="SUBMITTED", provider_tx_id=str(tx_id))
        raise IntegrationNotVerified("Circle wallet.execute output", "no transaction id/hash found in JSON response")

    async def live_readiness(self) -> LiveReadiness:
        reasons: list[str] = []
        if not self.address:
            reasons.append("ARC_RUNNER_CIRCLE_WALLET_ADDRESS is not set")
        if not self.allow_contract_execute:
            reasons.append("CIRCLE_ALLOW_CONTRACT_EXECUTE is false")
        try:
            status = await self._read(self.cli.status())
            logged = bool(self._find(status, {"authenticated", "loggedin", "isloggedin"}))
            if isinstance(status, dict) and str(status.get("status", "")).lower() in {"authenticated", "logged_in", "loggedin"}:
                logged = True
            if not logged:
                reasons.append("Circle CLI agent session is not authenticated")
        except Exception as exc:
            reasons.append(f"Circle CLI status failed: {type(exc).__name__}")
        try:
            if self.address:
                limits = await self._policy_limits()
                if self.require_spending_policy and (limits.get("per_tx") is None or limits.get("daily") is None):
                    reasons.append("Circle agent-wallet spending policy could not be verified")
        except Exception as exc:
            reasons.append(f"Circle spending policy read failed: {type(exc).__name__}")
        return LiveReadiness(available=not reasons, reasons=reasons, provider=self.name,
                             session_authorized=not any("authenticated" in r.lower() for r in reasons), address=self.address or None)


register_wallet_provider("none", lambda s: None)
register_wallet_provider("circle_agent_wallet", lambda s: CircleAgentWalletProvider(
    CircleCli(s.circle_cli_path), s.circle_wallet_address, s.circle_chain,
    allow_contract_execute=s.circle_allow_contract_execute,
    require_spending_policy=s.circle_require_spending_policy,
))


# ------------------------------------------------------------------ Privy server wallets (cloud-native, no local CLI)
import base64
from datetime import datetime, timezone

import httpx

from app.chains.arc.network import USDC_ERC20_ADDRESS, USDC_ERC20_DECIMALS


class PrivyClient:
    """Minimal Privy REST client for server wallets (app-owned / agent wallets).

    Auth: Basic(app_id:app_secret) + header privy-app-id.
    Docs: https://docs.privy.io/basics/rest-api/quickstart
    """

    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://api.privy.io/v1", timeout_s: float = 30.0):
        if not app_id or not app_secret:
            raise ValueError("Privy app_id and app_secret are required")
        token = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Basic {token}",
            "privy-app-id": app_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._timeout = timeout_s

    async def _request(self, method: str, path: str, *, json_body: dict | None = None, idempotency_key: str | None = None) -> dict:
        headers = dict(self._headers)
        if idempotency_key:
            headers["privy-idempotency-key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.request(method, f"{self.base_url}{path}", headers=headers, json=json_body)
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {"raw": r.text[:2000]}
        if r.status_code >= 400:
            detail = body.get("error") or body.get("message") or body
            raise IntegrationNotVerified("Privy API", f"HTTP {r.status_code}: {detail}")
        return body if isinstance(body, dict) else {"data": body}

    async def get_wallet(self, wallet_id: str) -> dict:
        return await self._request("GET", f"/wallets/{wallet_id}")

    async def create_wallet(self, *, chain_type: str = "ethereum", external_id: str | None = None) -> dict:
        payload: dict = {"chain_type": chain_type}
        if external_id:
            payload["external_id"] = external_id
        return await self._request("POST", "/wallets", json_body=payload)

    async def eth_send_transaction(
        self,
        wallet_id: str,
        *,
        caip2: str,
        to: str,
        data: str = "0x",
        value: int = 0,
        chain_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        tx: dict = {"to": to, "data": data if data.startswith("0x") else f"0x{data}", "value": hex(value) if isinstance(value, int) else value}
        if chain_id is not None:
            tx["chain_id"] = chain_id
        payload = {
            "method": "eth_sendTransaction",
            "caip2": caip2,
            "chain_type": "ethereum",
            "params": {"transaction": tx},
        }
        return await self._request("POST", f"/wallets/{wallet_id}/rpc", json_body=payload, idempotency_key=idempotency_key)


class PrivyWalletProvider(WalletProvider):
    """Cloud-native agent wallet via Privy server wallets.

    No local CLI or device session. The runner holds Privy app credentials and a
    wallet_id. Suitable for cloud LIVE when the operator accepts app-scoped custody
    within Privy policies + this app's WalletPolicy ceilings.
    """

    name = "privy"
    verified = True

    def __init__(
        self,
        client: PrivyClient,
        wallet_id: str,
        *,
        caip2: str = "eip155:5042",
        chain_id: int = 5042,
        address: str = "",
        rpc_urls: list[str] | None = None,
        allow_execute: bool = False,
        allow_withdraw: bool = False,
    ):
        self.client = client
        self.wallet_id = wallet_id
        self.caip2 = caip2
        self.chain_id = chain_id
        self.address = address
        self.rpc_urls = rpc_urls or []
        self.allow_execute = allow_execute
        self.allow_withdraw = allow_withdraw
        self._policy = None
        self._address_loaded = bool(address)

    def configure_policy(self, policy) -> None:
        self._policy = policy

    def capabilities(self) -> set[WalletCapability]:
        if self.allow_execute and self._policy is not None:
            return {WalletCapability.AUTONOMOUS_DELEGATED}
        return set()

    async def _ensure_address(self) -> str:
        if self.address and self._address_loaded:
            return self.address
        body = await self.client.get_wallet(self.wallet_id)
        addr = body.get("address") or (body.get("data") or {}).get("address")
        if not addr:
            raise IntegrationNotVerified("Privy wallet", f"no address on wallet {self.wallet_id}")
        self.address = str(addr)
        self._address_loaded = True
        return self.address

    async def get_account(self):
        addr = await self._ensure_address()
        return WalletAccount(id=self.wallet_id, provider=self.name, address=addr, chain="arc", kind="agent")

    async def get_usdc_balance(self) -> float:
        addr = await self._ensure_address()
        if not self.rpc_urls:
            raise IntegrationNotVerified("Privy USDC balance", "ARC_RUNNER_ARC_RPC_URL required to read balances")
        from app.chains.evm import EvmRpcClient

        rpc = EvmRpcClient(self.rpc_urls)
        data = "0x70a08231" + addr.lower().removeprefix("0x").rjust(64, "0")
        raw = await rpc.call("eth_call", [{"to": USDC_ERC20_ADDRESS, "data": data}, "latest"])
        return int(raw, 16) / 10 ** USDC_ERC20_DECIMALS

    async def get_authorization(self) -> WalletAuthorization | None:
        if not self.allow_execute or self._policy is None:
            return None
        addr = await self._ensure_address()
        now = datetime.now(timezone.utc)
        return WalletAuthorization(
            wallet_id=self.wallet_id,
            provider=self.name,
            capability=WalletCapability.AUTONOMOUS_DELEGATED,
            policy=self._policy,
            granted_at=now,
            expires_at=None,
            proof_ref=f"privy:{self.wallet_id}",
        )

    async def revoke(self) -> None:
        # App-owned server wallets are revoked by rotating Privy credentials / wallet archive in the dashboard.
        raise IntegrationNotVerified(
            "Privy revoke",
            "archive or rotate the wallet in the Privy dashboard / disable ARC_RUNNER_PRIVY_ALLOW_EXECUTE",
        )

    async def _submit(self, tx: UnsignedTransaction, auth: WalletAuthorization) -> SubmissionResult:
        if not self.allow_execute:
            raise IntegrationNotVerified(
                "Privy execute",
                "set ARC_RUNNER_PRIVY_ALLOW_EXECUTE=true only after reviewing policy and funding the wallet",
            )
        idem = hashlib.sha256(f"{self.wallet_id}:{tx.idempotency_key}:{tx.to}:{tx.data}:{tx.value}".encode()).hexdigest()[:32]
        # Prefer full calldata path (Universal Router execute)
        body = await self.client.eth_send_transaction(
            self.wallet_id,
            caip2=self.caip2,
            to=tx.to,
            data=tx.data,
            value=int(tx.value or 0),
            chain_id=tx.chain_id or self.chain_id,
            idempotency_key=idem,
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        tx_hash = None
        provider_tx_id = None
        if isinstance(data, dict):
            tx_hash = data.get("hash") or data.get("transaction_hash")
            provider_tx_id = data.get("transaction_id") or data.get("id")
        if not tx_hash and isinstance(body, dict):
            tx_hash = body.get("hash")
            provider_tx_id = body.get("transaction_id")
        if tx_hash:
            return SubmissionResult(status="SUBMITTED", tx_hash=str(tx_hash), provider_tx_id=str(provider_tx_id) if provider_tx_id else None)
        if provider_tx_id:
            return SubmissionResult(status="SUBMITTED", provider_tx_id=str(provider_tx_id))
        raise IntegrationNotVerified("Privy eth_sendTransaction", f"no hash in response: {body}")

    async def withdraw_usdc(self, to_address: str, amount_usdc: float) -> str:
        if not self.allow_withdraw:
            raise IntegrationNotVerified(
                "Privy withdraw",
                "set ARC_RUNNER_PRIVY_ALLOW_WITHDRAW=true only after you have verified the destination "
                "address and reviewed the wallet's funding source; this moves real USDC out of custody",
            )
        to = to_address.lower().strip()
        if not re.fullmatch(r"0x[0-9a-f]{40}", to):
            raise IntegrationNotVerified("Privy withdraw", f"invalid destination address: {to_address!r}")
        addr = await self._ensure_address()
        if to == addr.lower():
            raise IntegrationNotVerified("Privy withdraw", "destination address is the wallet's own address")
        units = int(round(amount_usdc * 10 ** USDC_ERC20_DECIMALS))
        if units <= 0:
            raise IntegrationNotVerified("Privy withdraw", "amount_usdc must be greater than zero")
        # ERC-20 transfer(address,uint256) — same manual-encoding style already
        # used for the balanceOf call in get_usdc_balance() above.
        data = "0xa9059cbb" + to.removeprefix("0x").rjust(64, "0") + hex(units)[2:].rjust(64, "0")
        idem = hashlib.sha256(f"withdraw:{self.wallet_id}:{to}:{units}".encode()).hexdigest()[:32]
        body = await self.client.eth_send_transaction(
            self.wallet_id, caip2=self.caip2, to=USDC_ERC20_ADDRESS, data=data, value=0,
            chain_id=self.chain_id, idempotency_key=idem,
        )
        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        tx_hash = (payload or {}).get("hash") or (payload or {}).get("transaction_hash") or body.get("hash")
        if not tx_hash:
            raise IntegrationNotVerified("Privy withdraw", f"no transaction hash in response: {body}")
        return str(tx_hash)

    async def live_readiness(self) -> LiveReadiness:
        reasons: list[str] = []
        if not self.wallet_id:
            reasons.append("ARC_RUNNER_PRIVY_WALLET_ID is not set")
        if not self.allow_execute:
            reasons.append("ARC_RUNNER_PRIVY_ALLOW_EXECUTE is false")
        try:
            await self._ensure_address()
        except Exception as exc:
            reasons.append(f"Privy wallet lookup failed: {type(exc).__name__}: {exc}")
        if not self.rpc_urls:
            reasons.append("ARC_RUNNER_ARC_RPC_URL not set (needed for balance checks)")
        return LiveReadiness(
            available=not reasons,
            reasons=reasons,
            provider=self.name,
            session_authorized=not any("lookup failed" in r.lower() for r in reasons),
            address=self.address or None,
        )


def _build_privy(s: RunnerSettings) -> WalletProvider | None:
    app_id = (s.privy_app_id or "").strip()
    secret = s.privy_app_secret.get_secret_value() if s.privy_app_secret else ""
    wallet_id = (s.privy_wallet_id or "").strip()
    if not app_id or not secret:
        # Still return a provider so live_readiness can report missing credentials clearly
        class _UnconfiguredPrivy(WalletProvider):
            name = "privy"
            verified = True
            def capabilities(self): return set()
            async def get_account(self): return None
            async def get_usdc_balance(self): raise IntegrationNotVerified("Privy", "APP_ID/SECRET missing")
            async def get_authorization(self): return None
            async def revoke(self): return None
            async def _submit(self, tx, auth): raise IntegrationNotVerified("Privy", "APP_ID/SECRET missing")
            async def live_readiness(self):
                return LiveReadiness(available=False, provider="privy",
                    reasons=["ARC_RUNNER_PRIVY_APP_ID and ARC_RUNNER_PRIVY_APP_SECRET are required"])
        return _UnconfiguredPrivy()
    client = PrivyClient(app_id, secret, base_url=s.privy_api_url)
    return PrivyWalletProvider(
        client,
        wallet_id,
        caip2=s.privy_caip2,
        chain_id=s.network.chain_id,
        address=s.privy_wallet_address or "",
        rpc_urls=s.rpc_urls,
        allow_execute=s.privy_allow_execute,
        allow_withdraw=s.privy_allow_withdraw,
    )


register_wallet_provider("privy", _build_privy)
