"""Free on-chain probes (Arc JSON-RPC only, no explorer credits).

These answer the risk-engine questions that no market-data API answers:
  * What can the token's admin do?  (mint / pause / blacklist / trading gates / limits) -> bytecode + view calls
  * Is it an upgradeable proxy?     -> EIP-1967 slots + EIP-1167 minimal proxy prefix
  * Who deployed it and through which launchpad? -> creation tx + receipt logs
  * Can an ordinary holder still transfer it?    -> eth_call transfer from a real holder

HONESTY NOTES (also surfaced in the UI through ``ContractInfo.checks_run`` / ``sell_check_method``):
  * Capability detection is selector presence in runtime bytecode (a PUSH4 in the dispatch table). It proves a
    function EXISTS, not that it is reachable by an attacker. "Active" additionally requires that ownership has not
    been renounced. When owner() is not available the answer stays conservative (active).
  * The sell check is a HOLDER TRANSFER PROBE, not a router/pool sell simulation. It catches paused tokens, blacklists
    and hard transfer locks; it cannot see pool-level taxes or hook logic. It is labelled "holder_transfer_probe"
    and never reported as a full simulation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from eth_utils import keccak

from app.chains.arc.market_data import CONTRACT_TO_LAUNCHPAD, LAUNCHPADS
from app.chains.evm import EvmRpcClient, RpcError

log = logging.getLogger("enrichment.probes")

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
PROBE_RECIPIENT = "0x00000000000000000000000000000000c0ffee01"

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
MINIMAL_PROXY_PREFIX = bytes.fromhex("363d3d373d3d3d363d73")


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


MINT_SIGS = ("mint(address,uint256)", "mint(uint256)", "mint(address)", "mintTo(address,uint256)")
PAUSE_SIGS = ("pause()", "unpause()", "setPaused(bool)", "pauseTrading()")
BLACKLIST_SIGS = (
    "blacklist(address)", "blacklist(address,bool)", "addToBlacklist(address)", "addBlacklist(address)",
    "addBlackList(address)", "setBlacklist(address,bool)", "blacklistAddress(address)", "setBots(address[])",
    "addBot(address)", "setBlockedAddress(address,bool)",
)
MAX_TX_SIGS = ("maxTxAmount()", "_maxTxAmount()", "maxTransactionAmount()", "setMaxTxAmount(uint256)")
MAX_WALLET_SIGS = ("maxWalletSize()", "_maxWalletSize()", "maxWallet()", "setMaxWallet(uint256)", "maxWalletAmount()")
TRADING_FLAG_SIGS = ("tradingEnabled()", "tradingOpen()", "tradingActive()")
BUY_TAX_SIGS = ("buyTax()", "buyFee()", "_buyTax()", "totalBuyFee()", "buyTotalFees()")
SELL_TAX_SIGS = ("sellTax()", "sellFee()", "_sellTax()", "totalSellFee()", "sellTotalFees()")

_SELECTORS = {
    "mint": [_sel(s) for s in MINT_SIGS],
    "pause": [_sel(s) for s in PAUSE_SIGS],
    "blacklist": [_sel(s) for s in BLACKLIST_SIGS],
    "max_tx": [_sel(s) for s in MAX_TX_SIGS],
    "max_wallet": [_sel(s) for s in MAX_WALLET_SIGS],
}
SEL_OWNER = _sel("owner()")
SEL_PAUSED = _sel("paused()")
SEL_TOTAL_SUPPLY = _sel("totalSupply()")
SEL_BALANCE_OF = _sel("balanceOf(address)")
SEL_TRANSFER = _sel("transfer(address,uint256)")


# ---------------------------------------------------------------------- pure helpers (unit-tested)
def scan_capabilities(code: bytes) -> dict[str, bool]:
    """Selector presence in a dispatch table (``PUSH4 <selector>``). Pure and deterministic."""
    return {name: any(b"\x63" + sel in code for sel in sels) for name, sels in _SELECTORS.items()}


def minimal_proxy_target(code: bytes) -> str | None:
    """EIP-1167 clone: ``363d3d373d3d3d363d73 <20-byte impl> 5af43d82803e903d91602b57fd5bf3``."""
    if code.startswith(MINIMAL_PROXY_PREFIX) and len(code) >= len(MINIMAL_PROXY_PREFIX) + 20:
        return "0x" + code[len(MINIMAL_PROXY_PREFIX):len(MINIMAL_PROXY_PREFIX) + 20].hex()
    return None


def word_to_address(word: str | None) -> str | None:
    if not word or word == "0x" or len(word) < 42:
        return None
    return "0x" + word[-40:].lower()


def word_to_int(word: str | None) -> int | None:
    if not word or word == "0x":
        return None
    try:
        return int(word, 16)
    except ValueError:
        return None


def normalise_tax(value: int | None) -> float | None:
    """Percent (<=100) or basis points (<=2500). Anything else is not trusted."""
    if value is None or value < 0:
        return None
    if value <= 100:
        return float(value)
    if value <= 2500:
        return value / 100.0
    return None


def match_launchpad(logs: list[dict], token: str, tx_to: str | None = None) -> tuple[str | None, str | None]:
    """Which launchpad created ``token``, from the creation tx receipt.

    Strong evidence: the factory's own token-creation event, carrying the token address in its indexed topic.
    Weak evidence: any log emitted by a launchpad factory in the same tx, or the tx being sent to a factory.
    Returns (launchpad_name, evidence) or (None, None).
    """
    token = token.lower()
    weak: str | None = None
    for lg in logs or []:
        emitter = str(lg.get("address") or "").lower()
        name = CONTRACT_TO_LAUNCHPAD.get(emitter)
        if not name:
            continue
        cfg = LAUNCHPADS[name]
        topics = [str(t).lower() for t in (lg.get("topics") or [])]
        sigs = {s.lower().removeprefix("0x") for s in cfg["sigs"]}
        idx = int(cfg["topic_index"])
        if topics and topics[0].removeprefix("0x") in sigs and len(topics) > idx and topics[idx].endswith(token.removeprefix("0x")):
            return name, "factory_creation_event"
        weak = weak or name
    if weak:
        return weak, "factory_log_in_creation_tx"
    if tx_to and tx_to.lower() in CONTRACT_TO_LAUNCHPAD:
        return CONTRACT_TO_LAUNCHPAD[tx_to.lower()], "creation_tx_sent_to_factory"
    return None, None


def is_revert(exc: Exception) -> bool:
    """True for an on-chain revert (a real answer: transfer is blocked). False/None-worthy cases (rate limits,
    connection errors, malformed RPC responses) are NOT reverts and must not be reported as a failed sell check."""
    if not isinstance(exc, RpcError):
        return False
    text = str(exc).lower()
    return any(h in text for h in ("revert", "execution reverted", "exceeds balance", "insufficient"))


# ---------------------------------------------------------------------- results
@dataclass
class ContractProbe:
    is_contract: bool = True
    is_proxy: bool | None = None
    implementation: str | None = None
    has_mint: bool = False
    has_pause: bool = False
    has_blacklist: bool = False
    owner: str | None = None
    owner_renounced: bool | None = None
    paused_now: bool | None = None
    trading_open: bool | None = None
    max_tx_limit: bool | None = None
    max_wallet_limit: bool | None = None
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None
    checks: list[str] = field(default_factory=list)

    def _admin_power(self, present: bool) -> bool | None:
        """Tri-state: False = capability absent (bytecode scanned) or admin renounced; True = capability present and an
        admin still exists; None = capability present but the admin model could not be read (no owner())."""
        if not present or self.owner_renounced is True:
            return False
        if self.owner_renounced is False:
            return True
        return None

    @property
    def mint_authority_active(self) -> bool | None:
        return self._admin_power(self.has_mint)

    @property
    def pausable(self) -> bool | None:
        return self._admin_power(self.has_pause)

    @property
    def blacklist_capability(self) -> bool | None:
        return self._admin_power(self.has_blacklist)

    @property
    def transfer_restricted(self) -> bool | None:
        if self.paused_now is True or self.trading_open is False:
            return True
        if self.paused_now is False or self.trading_open is True:
            return False
        return None


@dataclass
class CreationInfo:
    deployer: str | None = None
    created_at: datetime | None = None
    launchpad: str | None = None
    launchpad_evidence: str | None = None
    tx_to: str | None = None


# ---------------------------------------------------------------------- RPC-backed probe
class OnchainProbe:
    def __init__(self, rpc: EvmRpcClient) -> None:
        self.rpc = rpc

    async def _call(self, to: str, data: bytes | str, *, sender: str | None = None) -> str | None:
        payload: dict[str, str] = {"to": to, "data": data if isinstance(data, str) else "0x" + data.hex()}
        if sender:
            payload["from"] = sender
        return await self.rpc.call("eth_call", [payload, "latest"])

    async def _try(self, to: str, data: bytes) -> str | None:
        try:
            out = await self._call(to, data)
        except Exception:
            return None
        return out if out and out != "0x" else None

    async def _code(self, address: str) -> bytes | None:
        try:
            raw = await self.rpc.call("eth_getCode", [address, "latest"])
        except Exception:
            return None
        if not isinstance(raw, str):
            return None
        return bytes.fromhex(raw[2:]) if len(raw) >= 2 else b""

    async def contract(self, token: str) -> ContractProbe | None:
        """Capability / admin / proxy probe. None means the RPC could not be reached (unknown, not 'safe')."""
        code = await self._code(token)
        if code is None:
            return None
        p = ContractProbe()
        if not code:
            p.is_contract = False
            return p
        p.checks.append("bytecode_scan")
        caps_code = code

        impl = minimal_proxy_target(code)
        if impl is None:
            for slot in (EIP1967_IMPL_SLOT, EIP1967_BEACON_SLOT):
                try:
                    word = await self.rpc.call("eth_getStorageAt", [token, slot, "latest"])
                except Exception:
                    continue
                cand = word_to_address(word)
                if cand and cand != ZERO:
                    impl = cand
                    break
            p.checks.append("proxy_slots")
        p.is_proxy = impl is not None
        p.implementation = impl
        if impl:
            impl_code = await self._code(impl)
            if impl_code:
                caps_code = code + impl_code  # capabilities live in the implementation behind a proxy
        caps = scan_capabilities(caps_code)
        p.has_mint, p.has_pause, p.has_blacklist = caps["mint"], caps["pause"], caps["blacklist"]
        p.max_tx_limit = True if caps["max_tx"] else None
        p.max_wallet_limit = True if caps["max_wallet"] else None

        owner_word = await self._try(token, SEL_OWNER)
        if owner_word:
            p.owner = word_to_address(owner_word)
            p.owner_renounced = p.owner in (ZERO, DEAD)
            p.checks.append("owner")
        paused_word = await self._try(token, SEL_PAUSED)
        if paused_word is not None:
            v = word_to_int(paused_word)
            p.paused_now = None if v is None else bool(v)
            p.checks.append("paused_state")
        for sig in TRADING_FLAG_SIGS:
            w = await self._try(token, _sel(sig))
            v = word_to_int(w)
            if v is not None and v in (0, 1):
                p.trading_open = bool(v)
                p.checks.append("trading_flag")
                break
        for attr, sigs in (("buy_tax_pct", BUY_TAX_SIGS), ("sell_tax_pct", SELL_TAX_SIGS)):
            for sig in sigs:
                pct = normalise_tax(word_to_int(await self._try(token, _sel(sig))))
                if pct is not None:
                    setattr(p, attr, pct)
                    p.checks.append("tax_view")
                    break
        return p

    async def total_supply(self, token: str) -> int | None:
        return word_to_int(await self._try(token, SEL_TOTAL_SUPPLY))

    async def balance_of(self, token: str, holder: str) -> int | None:
        data = SEL_BALANCE_OF + bytes.fromhex(holder.lower().removeprefix("0x").rjust(64, "0"))
        return word_to_int(await self._try(token, data))

    async def creation(self, token: str, tx_hash: str | None) -> CreationInfo | None:
        """Deployer, block time and launchpad from the creation transaction. None when the tx cannot be read."""
        if not tx_hash:
            return None
        try:
            tx = await self.rpc.call("eth_getTransactionByHash", [tx_hash])
            if not tx:
                return None
            info = CreationInfo(deployer=str(tx.get("from") or "").lower() or None,
                                tx_to=str(tx.get("to") or "").lower() or None)
            try:
                receipt = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
                info.launchpad, info.launchpad_evidence = match_launchpad((receipt or {}).get("logs") or [], token, info.tx_to)
            except Exception:
                pass
            if tx.get("blockNumber"):
                try:
                    blk = await self.rpc.call("eth_getBlockByNumber", [tx["blockNumber"], False])
                    ts = word_to_int((blk or {}).get("timestamp"))
                    info.created_at = datetime.fromtimestamp(ts, timezone.utc) if ts else None
                except Exception:
                    pass
            return info
        except Exception:
            return None

    async def transfer_probe(self, token: str, holder: str, balance_raw: int | None = None) -> tuple[bool | None, str]:
        """eth_call ``transfer(probe, 1 unit)`` FROM a real holder. True = allowed, False = reverted / returned false,
        None = could not be determined (infra error). ``balance_raw`` guards against blaming the token for an
        empty balance."""
        if balance_raw is not None and balance_raw <= 0:
            return None, "holder balance is zero"
        data = SEL_TRANSFER + bytes.fromhex(PROBE_RECIPIENT[2:].rjust(64, "0")) + (1).to_bytes(32, "big")
        try:
            out = await self._call(token, data, sender=holder)
        except Exception as exc:
            if is_revert(exc):
                return False, f"transfer reverted: {str(exc)[:80]}"
            return None, f"probe unavailable: {type(exc).__name__}"
        if out in (None, "0x"):
            return True, "transfer succeeded (no return value)"
        v = word_to_int(out)
        return (True, "transfer succeeded") if v else (False, "transfer returned false")
