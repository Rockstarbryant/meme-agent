"""Fills the enrichment gaps a bare market-data provider leaves behind: contract verification, sellability,
creator identity/behaviour, top-holder concentration, holder growth, launchpad provenance and MEV exposure.

Design:
  * Every source is OPTIONAL. Missing/unconfigured/erroring sources degrade to "unknown" (None), never to a
    fabricated value, and the reason is recorded in ``MarketState.enrichment_gaps``.
  * Static facts (verification, creator, launchpad, deployer) are effectively immutable once known, so they are
    cached for the process lifetime once resolved — this also protects the Blockscout Pro credit budget.
  * Volatile facts (holders, top-N concentration) use a short TTL cache so a burst of monitoring cycles for the
    same hot token does not re-spend explorer credits every 12-30s.
  * Cheap on-chain probes (bytecode, proxy slots, owner/paused/trading-flag view calls) always run when an RPC
    client is available — they cost nothing but a JSON-RPC round trip and need no explorer key at all.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.chains.evm import EvmRpcClient
from app.core.errors import DataUnavailable
from app.domain.market import MarketState
from app.enrichment.mev import estimate_mev_exposure
from app.enrichment.probes import ContractProbe, OnchainProbe
from app.integrations.blockscout import BlockscoutClient
from app.integrations.etherscan import EtherscanV2Client

log = logging.getLogger("enrichment.service")


@dataclass
class _CacheEntry:
    value: Any
    at: float


class _TTLCache:
    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = ttl_s
        self._d: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Any:
        e = self._d.get(key)
        if e is None or (time.monotonic() - e.at) > self.ttl_s:
            return None
        return e.value

    def set(self, key: str, value: Any) -> None:
        self._d[key] = _CacheEntry(value, time.monotonic())


@dataclass
class EnrichmentConfig:
    enabled: bool = True
    holders_ttl_s: float = 300.0          # top-N / holder count: refresh every 5 min
    static_ttl_s: float = 21_600.0        # verification / creator / launchpad: refresh every 6h (still not "forever")
    probe_ttl_s: float = 900.0            # on-chain admin-power probe: 15 min
    trade_usdc_for_mev: float = 25.0


class EnrichmentService:
    def __init__(
        self,
        *,
        blockscout: BlockscoutClient | None = None,
        etherscan: EtherscanV2Client | None = None,
        rpc: EvmRpcClient | None = None,
        config: EnrichmentConfig | None = None,
    ) -> None:
        self.blockscout = blockscout
        self.etherscan = etherscan
        self.probe = OnchainProbe(rpc) if rpc is not None else None
        self.cfg = config or EnrichmentConfig()
        self._holders = _TTLCache(self.cfg.holders_ttl_s)
        self._static = _TTLCache(self.cfg.static_ttl_s)
        self._probe_cache = _TTLCache(self.cfg.probe_ttl_s)

    def sources(self) -> list[str]:
        out = []
        if self.blockscout is not None:
            out.append("blockscout")
        if self.etherscan is not None:
            out.append("etherscan")
        if self.probe is not None:
            out.append("arc_rpc_probe")
        return out

    # ------------------------------------------------------------------ holders
    async def _holders_pct(self, token: str) -> tuple[dict[str, Any], list[str]]:
        """top5/top10/top20 holder %, holder_count, source. Cached ``holders_ttl_s``."""
        key = token.lower()
        cached = self._holders.get(key)
        if cached is not None:
            return cached
        gaps: list[str] = []
        rows: list[dict] | None = None
        source = None
        total_supply_hint: int | None = None
        if self.blockscout is not None:
            try:
                data = await self.blockscout.token_holders(token)
                items = data.get("items") if isinstance(data, dict) else None
                if items:
                    rows = items
                    source = "blockscout"
                info = self._static.get(f"tokeninfo:{key}")
                if isinstance(info, dict) and info.get("total_supply"):
                    total_supply_hint = info["total_supply"]
            except DataUnavailable as exc:
                gaps.append(f"holders: blockscout unavailable ({exc})")
        if rows is None and self.etherscan is not None:
            try:
                es_rows = await self.etherscan.token_holder_list(token, limit=20)
                if es_rows:
                    rows = [{"address": {"hash": r.get("TokenHolderAddress")},
                             "value": r.get("TokenHolderQuantity")} for r in es_rows]
                    source = "etherscan"
            except DataUnavailable as exc:
                gaps.append(f"holders: etherscan unavailable ({exc})")
        result: dict[str, Any] = {"top5": None, "top10": None, "top20": None, "holder_count": None,
                                   "source": source, "basis": None}
        if rows:
            try:
                balances = sorted((int(r.get("value") or 0) for r in rows), reverse=True)
                supply = total_supply_hint or sum(balances)
                if supply > 0:
                    def pct(n: int) -> float:
                        return round(sum(balances[:n]) / supply * 100.0, 2)
                    result["top5"], result["top10"], result["top20"] = pct(5), pct(10), pct(min(20, len(balances)))
                    result["basis"] = "top_20_rows" if len(balances) <= 20 else "top_page"
            except (TypeError, ValueError):
                gaps.append("holders: malformed holder response")
        if self.blockscout is not None:
            try:
                info = await self._token_info_cached(token)
                if info.get("holder_count") is not None:
                    result["holder_count"] = info["holder_count"]
            except DataUnavailable as exc:
                gaps.append(f"holder_count: blockscout unavailable ({exc})")
        elif self.etherscan is not None:
            try:
                result["holder_count"] = await self.etherscan.token_holder_count(token)
            except DataUnavailable as exc:
                gaps.append(f"holder_count: etherscan unavailable ({exc})")
        if result["top10"] is None and not gaps:
            gaps.append("holders: no provider returned a holder list")
        self._holders.set(key, (result, gaps))
        return result, gaps

    async def _token_info_cached(self, token: str) -> dict[str, Any]:
        key = f"tokeninfo:{token.lower()}"
        cached = self._static.get(key)
        if cached is not None:
            return cached
        info = await self.blockscout.token_info(token)
        holder_count = info.get("holders_count") or info.get("holders")
        try:
            holder_count = int(holder_count) if holder_count is not None else None
        except (TypeError, ValueError):
            holder_count = None
        supply = info.get("total_supply")
        try:
            supply = int(supply) if supply is not None else None
        except (TypeError, ValueError):
            supply = None
        out = {"holder_count": holder_count, "total_supply": supply}
        self._static.set(key, out)
        return out

    # ------------------------------------------------------------------ creator / verification
    async def _static_facts(self, token: str) -> tuple[dict[str, Any], list[str]]:
        key = f"static:{token.lower()}"
        cached = self._static.get(key)
        if cached is not None:
            return cached
        gaps: list[str] = []
        out: dict[str, Any] = {"verified": None, "is_proxy": None, "implementation": None, "verification_source": None,
                                "creator": None, "creation_tx": None, "contract_name": None}
        if self.blockscout is not None:
            try:
                addr = await self.blockscout.address_info(token)
                out["creator"] = (addr.get("creator_address_hash") or "").lower() or None
                out["creation_tx"] = addr.get("creation_transaction_hash")
                is_verified = addr.get("is_verified")
                if is_verified is not None:
                    out["verified"] = bool(is_verified)
                    out["verification_source"] = "blockscout"
                proxy_type = addr.get("proxy_type")
                if proxy_type:
                    out["is_proxy"] = True
                impls = addr.get("implementations")
                if isinstance(impls, list) and impls:
                    out["implementation"] = (impls[0] or {}).get("address")
            except DataUnavailable as exc:
                gaps.append(f"verification: blockscout unavailable ({exc})")
        if (out["verified"] is None or out["creator"] is None) and self.etherscan is not None:
            try:
                src = await self.etherscan.source_code(token)
                if out["verified"] is None:
                    out["verified"] = src["verified"]
                    out["verification_source"] = "etherscan"
                    out["is_proxy"] = out["is_proxy"] if out["is_proxy"] is not None else src["is_proxy"]
                    out["implementation"] = out["implementation"] or src["implementation"]
                    out["contract_name"] = src["contract_name"]
            except DataUnavailable as exc:
                gaps.append(f"verification: etherscan unavailable ({exc})")
            if out["creator"] is None:
                try:
                    created = await self.etherscan.contract_creation(token)
                    if created:
                        out["creator"] = created["creator"]
                        out["creation_tx"] = out["creation_tx"] or created["tx_hash"]
                except DataUnavailable as exc:
                    gaps.append(f"creator: etherscan unavailable ({exc})")
        if out["verified"] is None:
            gaps.append("contract verification status unknown (no explorer configured or reachable)")
        if out["creator"] is None:
            gaps.append("creator address unknown (no explorer configured or reachable)")
        self._static.set(key, (out, gaps))
        return out, gaps

    # ------------------------------------------------------------------ on-chain probe (contract + sellability)
    async def _probe_contract(self, token: str) -> tuple[ContractProbe | None, list[str]]:
        if self.probe is None:
            return None, ["contract probe unavailable: no RPC client configured"]
        key = f"probe:{token.lower()}"
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        gaps: list[str] = []
        try:
            p = await self.probe.contract(token)
        except Exception as exc:  # noqa: BLE001 — enrichment never raises past this point
            log.warning("contract probe failed for %s: %s", token, exc)
            p = None
        if p is None:
            gaps.append("contract probe unavailable: RPC could not read the contract")
        self._probe_cache.set(key, (p, gaps))
        return p, gaps

    async def _sell_check(self, token: str, creator: str | None) -> tuple[bool | None, str | None, list[str]]:
        """Holder-transfer probe using the known creator's balance (near-certain non-zero balance for a fresh
        token). Labelled ``sell_check_method`` — never reported as a router/pool sell simulation."""
        if self.probe is None:
            return None, None, ["sellability: no RPC client configured"]
        if not creator:
            return None, None, ["sellability: no holder with a known non-zero balance to probe"]
        try:
            bal = await self.probe.balance_of(token, creator)
        except Exception:
            bal = None
        if not bal:
            return None, None, ["sellability: no holder with a known non-zero balance to probe"]
        ok, detail = await self.probe.transfer_probe(token, creator, balance_raw=bal)
        if ok is None:
            return None, None, [f"sellability: {detail}"]
        return ok, "holder_transfer_probe", []

    # ------------------------------------------------------------------ public entrypoint
    async def enrich(self, m: MarketState, *, previous: MarketState | None = None) -> MarketState:
        """Returns the updated MarketState (mutated in place). Never raises: internal failures degrade to a gap."""
        if not self.cfg.enabled:
            return m
        gaps: list[str] = list(m.enrichment_gaps)
        checks_run: list[str] = list(m.contract.checks_run)
        token = m.token_address

        # --- holders / concentration ---------------------------------------------------------------------
        try:
            h, hgaps = await self._holders_pct(token)
            gaps += hgaps
            if h.get("top5") is not None:
                m.top5_holder_pct, m.top10_holder_pct, m.top20_holder_pct = h["top5"], h["top10"], h["top20"]
                m.holder_basis = h.get("basis")
                checks_run.append(f"holders:{h.get('source')}")
            if h.get("holder_count") is not None:
                m.holder_count = m.holder_count if m.holder_count is not None else h["holder_count"]
        except Exception as exc:  # noqa: BLE001
            log.warning("holder enrichment failed for %s: %s", token, exc)
            gaps.append("holders: enrichment failed unexpectedly")

        # --- holder growth (needs a prior snapshot with a holder_count) -----------------------------------
        if m.holder_count is not None and previous is not None and previous.holder_count:
            window_s = max(1.0, (m.timestamp - previous.timestamp).total_seconds())
            m.holder_growth_pct = round((m.holder_count - previous.holder_count) / previous.holder_count * 100.0, 2)
            m.holder_growth_window_s = window_s
        elif m.holder_count is not None and previous is None:
            gaps.append("holder_growth: no prior snapshot yet (first scan of this token)")

        # --- static facts: verification / creator / launchpad ---------------------------------------------
        try:
            facts, sgaps = await self._static_facts(token)
            gaps += sgaps
            m.contract.verified = facts["verified"]
            m.contract.verification_source = facts["verification_source"]
            if facts["is_proxy"] is not None:
                m.contract.is_proxy = m.contract.is_proxy if m.contract.is_proxy is not None else facts["is_proxy"]
            if facts["verification_source"]:
                checks_run.append(f"verification:{facts['verification_source']}")
            if facts["creator"]:
                if not m.creator_address:
                    m.creator_address = facts["creator"]
                m.creator_known = True
                checks_run.append("creator:explorer")
        except Exception as exc:  # noqa: BLE001
            log.warning("static-fact enrichment failed for %s: %s", token, exc)
            gaps.append("verification/creator: enrichment failed unexpectedly")

        # --- on-chain probe: mint/pause/blacklist/proxy/tax --------------------------------------------
        try:
            p, pgaps = await self._probe_contract(token)
            gaps += pgaps
            if p is not None:
                if not p.is_contract:
                    gaps.append("address is not a contract (EOA)")
                m.contract.mint_authority_active = p.mint_authority_active
                m.contract.pausable = p.pausable
                m.contract.blacklist_capability = p.blacklist_capability
                m.contract.transfer_restricted = p.transfer_restricted
                m.contract.max_tx_limit = p.max_tx_limit
                m.contract.max_wallet_limit = p.max_wallet_limit
                if p.buy_tax_pct is not None:
                    m.contract.buy_tax_pct = p.buy_tax_pct
                if p.sell_tax_pct is not None:
                    m.contract.sell_tax_pct = p.sell_tax_pct
                if p.is_proxy is not None:
                    m.contract.is_proxy = p.is_proxy
                checks_run += [f"probe:{c}" for c in p.checks]
        except Exception as exc:  # noqa: BLE001
            log.warning("on-chain probe failed for %s: %s", token, exc)
            gaps.append("contract probe: failed unexpectedly")

        # --- launchpad detection (display-only) + creation info -------------------------------------------
        if self.probe is not None and m.launchpad_detected is None:
            try:
                facts, _ = await self._static_facts(token)
                creation_tx = facts.get("creation_tx")
                if creation_tx:
                    info = await self.probe.creation(token, creation_tx)
                    if info:
                        m.launchpad_detected, m.launchpad_evidence = info.launchpad, info.launchpad_evidence
                        if info.created_at and not m.token_created_at:
                            m.token_created_at = info.created_at
                        if info.deployer and not m.creator_address:
                            m.creator_address = info.deployer
                            m.creator_known = True
                        checks_run.append("launchpad:creation_tx")
            except Exception as exc:  # noqa: BLE001
                log.warning("launchpad detection failed for %s: %s", token, exc)

        # --- sellability (holder transfer probe) ------------------------------------------------------------
        try:
            ok, method, sgaps = await self._sell_check(token, m.creator_address)
            gaps += sgaps
            if ok is not None:
                m.contract.sell_simulation_ok = ok
                m.contract.sell_check_method = method
                checks_run.append("sellability:holder_transfer_probe")
        except Exception as exc:  # noqa: BLE001
            log.warning("sell check failed for %s: %s", token, exc)
            gaps.append("sellability: check failed unexpectedly")

        # --- MEV heuristic (always labelled, never a substitute for a real simulation) ----------------------
        try:
            score, method = estimate_mev_exposure(m, trade_usdc=self.cfg.trade_usdc_for_mev)
            m.mev_risk_score, m.mev_method = score, method
            if score is None:
                gaps.append("mev: liquidity unknown, no heuristic possible")
        except Exception as exc:  # noqa: BLE001
            log.warning("mev heuristic failed for %s: %s", token, exc)

        m.contract.checks_run = list(dict.fromkeys(checks_run))
        m.enrichment_gaps = list(dict.fromkeys(gaps))
        m.enriched_at = datetime.now(timezone.utc)
        if m.scanned_at is None:
            m.scanned_at = m.enriched_at
        return m
