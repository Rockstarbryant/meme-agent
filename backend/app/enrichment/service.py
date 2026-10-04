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
    probe_ttl_s: float = 21_600.0         # on-chain admin-power probe: contract facts barely change -> 6h
    trade_usdc_for_mev: float = 25.0
    holder_count_ttl_s: float = 1_800.0   # holder count (drives holder growth): 30 min
    deep_holders_ttl_s: float = 3_600.0   # multi-page holder list (percentile concentration): 1h
    holders_max_rows: int = 250           # most holder rows fetched per token (50 rows = 1 page = 20 Pro credits)
    deep_holders_min_liquidity: float = 20_000.0  # only tokens this liquid AND trading get the multi-page fetch
    # Holder-transfer sell probe. OFF: sellability is now judged from OBSERVED sellers (see MarketState.sellers_in),
    # which is stronger evidence than a heuristic transfer probe and costs no RPC calls.
    sell_probe_enabled: bool = False
    # Addresses that hold supply for protocol reasons (pool manager, burn addresses) and are excluded from
    # concentration. The token contract itself and the token's own pool are always excluded too.
    exclude_holder_addresses: tuple[str, ...] = (
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # Uniswap v4 PoolManager on Arc
    )


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
        self._count_cache = _TTLCache(self.cfg.holder_count_ttl_s)
        self._holder_hist: dict[str, list[tuple[float, int]]] = {}  # token -> [(unix_ts, holder_count)]

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
    def _excluded(self, token: str, m: MarketState) -> set[str]:
        ex = {a.lower() for a in self.cfg.exclude_holder_addresses}
        ex.add(token.lower())
        if m.pool_address:
            ex.add(str(m.pool_address).lower())
        return ex

    async def _holders_pct(self, token: str, m: MarketState) -> tuple[dict[str, Any], list[str]]:
        """Concentration + holder count. Supply shares are computed against CIRCULATING supply (total supply minus
        balances held by the token itself, its pool, the pool manager and burn addresses), and only when the total
        supply is actually known: dividing by the sum of the fetched rows (what this used to do when the supply
        was not cached yet) reported top-10 as 60-100% for almost every token."""
        key = token.lower()
        windows_active = any((w.buys or 0) + (w.sells or 0) > 0 for w in m.windows.values()) if m.windows else False
        deep = bool(m.liquidity and m.liquidity >= self.cfg.deep_holders_min_liquidity and windows_active)
        cached = self._holders.get(key)
        if cached is not None and (not deep or cached[0].get("deep")):
            return cached
        gaps: list[str] = []
        result: dict[str, Any] = {
            "top5": None, "top10": None, "top20": None, "top5pct": None, "top20pct": None, "top30pct": None,
            "holder_count": None, "sampled": None, "source": None, "basis": None, "deep": deep, "name": None,
        }
        info: dict[str, Any] = {}
        if self.blockscout is not None:
            try:
                info = await self._token_info_cached(token)
                result["holder_count"] = info.get("holder_count")
                result["name"] = info.get("name")
            except DataUnavailable as exc:
                gaps.append(f"token info: blockscout unavailable ({exc})")
        rows: list[dict] | None = None
        complete = False
        if self.blockscout is not None:
            want = self.cfg.holders_max_rows if deep else 50
            try:
                rows, complete = await self.blockscout.token_holders_rows(token, max_rows=want)
                if rows:
                    result["source"] = "blockscout"
            except DataUnavailable as exc:
                gaps.append(f"holders: blockscout unavailable ({exc})")
        if not rows and self.etherscan is not None:
            try:
                es_rows = await self.etherscan.token_holder_list(token, limit=20)
                if es_rows:
                    rows = [{"address": {"hash": r.get("TokenHolderAddress")}, "value": r.get("TokenHolderQuantity")}
                            for r in es_rows]
                    result["source"] = "etherscan"
            except DataUnavailable as exc:
                gaps.append(f"holders: etherscan unavailable ({exc})")
            if result["holder_count"] is None:
                try:
                    result["holder_count"] = await self.etherscan.token_holder_count(token)
                except DataUnavailable as exc:
                    gaps.append(f"holder_count: etherscan unavailable ({exc})")
        supply = info.get("total_supply")
        if rows and supply and supply > 0:
            try:
                ex = self._excluded(token, m)
                excluded_bal = 0
                kept: list[int] = []
                n_excluded = 0
                for r in rows:
                    addr = str(((r.get("address") or {}).get("hash") if isinstance(r.get("address"), dict)
                                else r.get("address")) or "").lower()
                    bal = int(r.get("value") or 0)
                    if addr in ex:
                        excluded_bal += bal
                        n_excluded += 1
                    else:
                        kept.append(bal)
                kept.sort(reverse=True)
                circ = supply - excluded_bal
                if circ > 0 and kept:
                    def pct(n: int) -> float:
                        return round(sum(kept[:n]) / circ * 100.0, 2)
                    result["top5"], result["top10"] = pct(5), pct(10)
                    result["top20"] = pct(20) if len(kept) >= 20 else None
                    h = result["holder_count"]
                    h_eff = max(1, (h - n_excluded)) if h else (len(kept) if complete else None)
                    if h_eff:
                        for name, share in (("top5pct", 5.0), ("top20pct", 20.0), ("top30pct", 30.0)):
                            n_need = max(1, int(-(-h_eff * share // 100)))
                            if len(kept) >= n_need:
                                result[name] = pct(n_need)
                            else:
                                gaps.append(f"holders: {name.replace('pct', '%')} needs {n_need} holder rows, "
                                            f"fetched {len(kept)}")
                    result["sampled"] = len(kept)
                    result["basis"] = (f"circulating supply, {len(kept)} holder rows"
                                       + (f", {n_excluded} protocol/burn holders excluded" if n_excluded else ""))
            except (TypeError, ValueError):
                gaps.append("holders: malformed holder response")
        elif rows and not supply:
            gaps.append("holders: total supply unknown, concentration not computed")
        if result["top10"] is None and not gaps:
            gaps.append("holders: no provider returned a holder list")
        self._holders._d[key] = _CacheEntry((result, gaps), time.monotonic())
        # A deep result lives longer; shallow results expire on the normal holders TTL.
        if deep:
            self._holders._d[key].at = time.monotonic() - self._holders.ttl_s + self.cfg.deep_holders_ttl_s
        return result, gaps

    async def _token_info_cached(self, token: str) -> dict[str, Any]:
        """name / symbol / total supply are static (6h); the holder COUNT has its own short TTL because holder
        growth is measured from it."""
        key = f"tokeninfo:{token.lower()}"
        static = self._static.get(key)
        count = self._count_cache.get(key)
        if static is not None and count is not None:
            return {**static, "holder_count": count.get("holder_count")}
        info = await self.blockscout.token_info(token)

        def _int(v: Any) -> int | None:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        static = {"total_supply": _int(info.get("total_supply")),
                  "name": info.get("name") if isinstance(info.get("name"), str) else None,
                  "symbol": info.get("symbol") if isinstance(info.get("symbol"), str) else None}
        self._static.set(key, static)
        hc = {"holder_count": _int(info.get("holders_count") if info.get("holders_count") is not None else info.get("holders"))}
        self._count_cache.set(key, hc)
        return {**static, **hc}

    def _holder_growth(self, token: str, now_ts: float, count: int) -> dict[str, float]:
        """Percent change in holder count over 1h / 6h / 24h, from OUR OWN history (a window is reported only
        once the history reaches back about that far)."""
        hist = self._holder_hist.setdefault(token.lower(), [])
        if not hist or count != hist[-1][1] or now_ts - hist[-1][0] >= 300:
            hist.append((now_ts, int(count)))
        cutoff = now_ts - 26 * 3600
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        out: dict[str, float] = {}
        for w, secs in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            target = now_ts - secs
            cands = [(abs(t - target), c) for t, c in hist if secs * 0.85 <= now_ts - t <= secs * 1.5 and c > 0]
            if cands:
                ref = min(cands)[1]
                out[w] = round((count - ref) / ref * 100.0, 2)
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

    async def _sell_check(self, token: str, creator: str | None,
                          extra_holders: list[str] | None = None) -> tuple[bool | None, str | None, list[str]]:
        """Holder-transfer probe. Tries creator first, then any known holders with balance.

        Labelled ``sell_check_method`` — never reported as a router/pool sell simulation.
        """
        if self.probe is None:
            return None, None, ["sellability: no RPC client configured"]
        candidates: list[str] = []
        for addr in [creator, *(extra_holders or [])]:
            if addr and addr.lower() not in {c.lower() for c in candidates}:
                candidates.append(addr)
        if not candidates:
            return None, None, ["sellability: no holder with a known non-zero balance to probe"]
        last_detail = "no holder with a known non-zero balance to probe"
        for holder in candidates[:5]:
            try:
                bal = await self.probe.balance_of(token, holder)
            except Exception:
                bal = None
            if not bal:
                continue
            ok, detail = await self.probe.transfer_probe(token, holder, balance_raw=bal)
            if ok is None:
                last_detail = detail or last_detail
                continue
            return ok, "holder_transfer_probe", []
        return None, None, [f"sellability: {last_detail}"]

    # ------------------------------------------------------------------ public entrypoint
    async def enrich(self, m: MarketState, *, previous: MarketState | None = None) -> MarketState:
        """Returns the updated MarketState (mutated in place). Never raises: internal failures degrade to a gap."""
        if not self.cfg.enabled:
            return m
        gaps: list[str] = list(m.enrichment_gaps)
        checks_run: list[str] = list(m.contract.checks_run)
        token = m.token_address

        # --- holders / concentration / holder growth ---------------------------------------------------------
        try:
            h, hgaps = await self._holders_pct(token, m)
            gaps += hgaps
            if h.get("top10") is not None:
                m.top5_holder_pct, m.top10_holder_pct, m.top20_holder_pct = h["top5"], h["top10"], h["top20"]
                m.top_5pct_holders_pct, m.top_20pct_holders_pct = h["top5pct"], h["top20pct"]
                m.top_30pct_holders_pct = h["top30pct"]
                m.holders_sampled = h.get("sampled")
                m.holder_basis = h.get("basis")
                checks_run.append(f"holders:{h.get('source')}")
            if h.get("holder_count") is not None:
                m.holder_count = m.holder_count if m.holder_count is not None else h["holder_count"]
            if h.get("name") and not m.token_name:
                m.token_name = h["name"]
        except Exception as exc:  # noqa: BLE001
            log.warning("holder enrichment failed for %s: %s", token, exc)
            gaps.append("holders: enrichment failed unexpectedly")

        try:
            if m.holder_count:
                growth = self._holder_growth(token, m.timestamp.timestamp(), m.holder_count)
                m.holder_growth = growth
                best = next(((w, growth[w]) for w in ("24h", "6h", "1h") if w in growth), None)
                if best is not None:
                    m.holder_growth_pct = best[1]
                    m.holder_growth_window_s = {"1h": 3600.0, "6h": 21600.0, "24h": 86400.0}[best[0]]
                else:
                    gaps.append("holder_growth: history has not reached 1h yet (builds up while the worker runs)")
        except Exception as exc:  # noqa: BLE001 - a bad value must not take down the rest of enrichment
            log.warning("holder-growth computation failed for %s: %s", token, exc)
            gaps.append("holder_growth: computation failed unexpectedly")

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

        # --- sellability: judged from OBSERVED sellers (MarketState.sellers_in / has_two_way_trading). The old
        # holder-transfer probe is off by default (see EnrichmentConfig.sell_probe_enabled).
        if self.cfg.sell_probe_enabled:
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
