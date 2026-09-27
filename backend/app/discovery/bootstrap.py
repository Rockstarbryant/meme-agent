"""Build shared market-data providers and wire discovery + monitoring for the cloud worker.

One global MarketDataRegistry + SharedMarketDataGateway serves all tenants.
Discovery and monitoring use the same providers; trade cycles only read
cached/shared state and evaluate user strategy.

Monitoring refreshes price / market_cap / liquidity / holders on every pass
so values are never frozen at discovery time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.clock import utcnow
from app.core.errors import DataUnavailable
from app.discovery.monitoring import GlobalMonitoringService, MonitoringConfig
from app.discovery.registry import GlobalTokenRegistry, LaunchpadToken, TokenStatus
from app.discovery.service import DiscoveryCheckpoint, DiscoveryConfig, GlobalDiscoveryService
from app.discovery.store import PersistentTokenStore, hydrate_registry
from app.domain.market import MarketState
from app.market_data.gateway import SharedMarketDataGateway
from app.strategies.traction_momentum import TractionMomentum, TractionMomentumConfig

log = logging.getLogger("discovery.bootstrap")


def build_global_market_data(settings: Any):
    """Mirror RunnerRuntime provider construction (without LocalStore health persistence)."""
    from app.chains.arc.market_data import UnavailableArcMarketData, BitqueryArcMarketData
    from app.market_data.registry import MarketDataRegistry

    providers: list[tuple[str, Any]] = []
    wanted = [
        x.strip().lower()
        for x in getattr(settings, "market_data_providers", "dexpaprika").split(",")
        if x.strip()
    ]
    data_source = getattr(settings, "data_source", "arc")
    timeout = float(getattr(settings, "market_data_timeout_s", 20.0))
    max_tokens = int(getattr(settings, "market_data_max_tokens", 40))
    cache_s = float(getattr(settings, "market_data_cache_s", 60.0))

    if data_source == "demo":
        try:
            from app.chains.demo import DemoMarketData
            return DemoMarketData()
        except Exception:
            log.exception("demo market data failed")
            return UnavailableArcMarketData()

    # DexPaprika (primary free discovery + enrichment)
    if "dexpaprika" in wanted:
        try:
            from app.integrations.dexpaprika import DexPaprikaClient
            from app.chains.arc.dexpaprika_market_data import DexPaprikaArcMarketData

            key = None
            if getattr(settings, "dexpaprika_api_key", None):
                key = settings.dexpaprika_api_key.get_secret_value()
            # DexPaprikaClient is keyword-only: timeout_s=, api_key=
            client = DexPaprikaClient(api_key=key, timeout_s=timeout)
            providers.append(("dexpaprika", DexPaprikaArcMarketData(
                client,
                max_tokens=max_tokens,
                cache_s=cache_s,
                min_txns_24h=int(getattr(settings, "dexpaprika_min_txns_24h", 1)),
                min_volume_24h_usd=float(getattr(settings, "dexpaprika_min_volume_24h_usd", 100)),
                lookback_hours=int(getattr(settings, "dexpaprika_lookback_hours", 24)),
                network=getattr(settings, "dexpaprika_network", "arc"),
                scan_new_launches=bool(getattr(settings, "scan_new_launches", True)),
                scan_established=bool(getattr(settings, "scan_established", True)),
                established_min_age_hours=float(getattr(settings, "established_min_age_hours", 24)),
            )))
        except Exception:
            log.exception("dexpaprika provider init failed")

    # GeckoTerminal enrichment
    if "geckoterminal" in wanted:
        try:
            from app.integrations.geckoterminal import GeckoTerminalClient
            from app.chains.arc.gecko_market_data import GeckoTerminalArcMarketData

            key = (
                settings.geckoterminal_api_key.get_secret_value()
                if getattr(settings, "geckoterminal_api_key", None)
                else None
            )
            gecko = GeckoTerminalClient(
                getattr(settings, "geckoterminal_base_url", "https://api.geckoterminal.com/api/v2"),
                getattr(settings, "geckoterminal_network", "arc"),
                timeout_s=timeout,
                api_key=key,
            )
            providers.append(("geckoterminal", GeckoTerminalArcMarketData(
                gecko, max_tokens=max_tokens, cache_s=cache_s,
            )))
        except Exception:
            log.exception("gecko provider init failed")

    # DexScreener enrichment
    if "dexscreener" in wanted:
        try:
            from app.integrations.dexscreener import DexScreenerClient
            from app.chains.arc.dexscreener_market_data import DexScreenerArcMarketData

            ds = DexScreenerClient(timeout_s=timeout)
            providers.append(("dexscreener", DexScreenerArcMarketData(
                ds,
                chain_id=getattr(settings, "dexscreener_chain_id", "arc"),
                cache_s=float(getattr(settings, "dexscreener_cache_s", 60.0)),
            )))
        except Exception:
            log.exception("dexscreener provider init failed")

    # Bitquery
    if "bitquery" in wanted and getattr(settings, "bitquery_api_key", None):
        try:
            from app.integrations.bitquery import BitqueryClient

            client = BitqueryClient(
                settings.bitquery_api_key.get_secret_value(),
                getattr(settings, "bitquery_endpoint", "https://streaming.bitquery.io/graphql"),
                timeout_s=timeout,
            )
            providers.append(("bitquery", BitqueryArcMarketData(client, max_tokens=max_tokens)))
        except Exception:
            log.exception("bitquery provider init failed")

    if not providers:
        log.warning("no market-data providers configured for global discovery")
        return UnavailableArcMarketData()

    essential = [
        x.strip().lower()
        for x in getattr(settings, "market_data_essential_providers", "dexpaprika").split(",")
        if x.strip()
    ]
    return MarketDataRegistry(
        providers,
        failure_threshold=int(getattr(settings, "market_data_failure_threshold", 3)),
        cooldown_s=float(getattr(settings, "market_data_cooldown_s", 60.0)),
        essential=essential,
    )


def market_state_to_snapshot(m: MarketState) -> dict:
    return {
        "chain": m.chain,
        "token_address": m.token_address,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "launchpad": m.launchpad,
        "pool_address": m.pool_address,
        "symbol": m.symbol,
        "creator_address": m.creator_address,
        "token_created_at": m.token_created_at.isoformat() if m.token_created_at else None,
        "price": m.price,
        "market_cap": m.market_cap,
        "liquidity": m.liquidity,
        "volume_5m": m.volume_5m,
        "volume_15m": m.volume_15m,
        "buys_5m": m.buys_5m,
        "sells_5m": m.sells_5m,
        "unique_buyers_5m": m.unique_buyers_5m,
        "unique_sellers_5m": m.unique_sellers_5m,
        "holder_count": m.holder_count,
        "holder_growth_pct": m.holder_growth_pct,
        "top10_holder_pct": m.top10_holder_pct,
        "price_change_5m": m.price_change_5m,
        "price_change_15m": m.price_change_15m,
        "contract": m.contract.model_dump() if m.contract else {},
    }


def _parse(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


class GlobalPipeline:
    """Registry + discovery + monitoring + gateway + durable store — one per worker."""

    def __init__(self, settings: Any, *, redis: Any = None, session_factory: Any = None) -> None:
        self.settings = settings
        self.redis = redis
        self.store = PersistentTokenStore(session_factory, redis)
        self.registry = GlobalTokenRegistry()
        self.gateway = SharedMarketDataGateway(
            redis=redis,
            default_ttl_s=float(getattr(settings, "market_data_cache_ttl_s", 15.0)),
        )
        self.market_data = build_global_market_data(settings)
        self.strategy = TractionMomentum(TractionMomentumConfig())

        disc_cfg = DiscoveryConfig(
            interval_s=float(getattr(settings, "global_discovery_interval_s", 3 * 3600)),
            lookback_s=float(getattr(settings, "global_discovery_lookback_s", 15 * 60)),
            min_liquidity=float(getattr(settings, "risk_min_liquidity_usdc", 10_000) or 10_000),
            candidate_score_threshold=float(getattr(settings, "global_candidate_score_threshold", 40.0)),
        )
        mon_cfg = MonitoringConfig(
            hot_interval_s=float(getattr(settings, "monitor_hot_interval_s", 30)),
            warm_interval_s=float(getattr(settings, "monitor_warm_interval_s", 120)),
            cold_interval_s=float(getattr(settings, "monitor_cold_interval_s", 600)),
        )
        self.discovery = GlobalDiscoveryService(
            self.registry,
            redis=redis,
            config=disc_cfg,
            discover_fn=self._discover,
            screen_fn=self._screen,
            store=self.store,
        )
        self.monitoring = GlobalMonitoringService(
            self.registry,
            redis=redis,
            config=mon_cfg,
            fetch_state_fn=self._fetch_state,
            score_fn=self._score,
            store=self.store,
        )

    async def start(self) -> None:
        n = await hydrate_registry(self.registry, self.store)
        log.info("hydrated %d tokens into global registry", n)
        cp = await self.store.load_checkpoint("global")
        if cp:
            self.discovery.checkpoint = DiscoveryCheckpoint(
                last_success_at=_parse(cp.get("last_success_at")),
                last_attempt_at=_parse(cp.get("last_attempt_at")),
                last_window_start=_parse(cp.get("last_window_start")),
                last_window_end=_parse(cp.get("last_window_end")),
                tokens_found=int(cp.get("tokens_found") or 0),
                error=cp.get("error"),
            )

    async def _discover(self, start: datetime, end: datetime) -> list[dict]:
        try:
            addrs = await self.market_data.discover_tokens()
        except DataUnavailable as e:
            log.warning("global discovery unavailable: %s", e)
            return []
        except Exception:
            log.exception("discover_tokens failed")
            return []

        out: list[dict] = []
        for addr in addrs:
            try:
                m = await self.gateway.get(
                    "arc",
                    addr,
                    lambda a=addr: self.market_data.get_market_state(a),
                    kind="state",
                    ttl_s=30.0,
                )
            except Exception:
                out.append({"chain": "arc", "token_address": str(addr).lower()})
                continue
            if not isinstance(m, MarketState):
                out.append({"chain": "arc", "token_address": str(addr).lower()})
                continue
            if m.token_created_at is not None:
                created = (
                    m.token_created_at
                    if m.token_created_at.tzinfo
                    else m.token_created_at.replace(tzinfo=timezone.utc)
                )
                age_h = (utcnow() - created).total_seconds() / 3600
                if age_h > 72 and (created < start or created > end):
                    continue
            out.append({
                "chain": m.chain or "arc",
                "token_address": m.token_address.lower(),
                "launchpad": m.launchpad,
                "creator_address": m.creator_address,
                "launched_at": m.token_created_at,
                "symbol": m.symbol,
                "meta": {"initial_snapshot": market_state_to_snapshot(m)},
            })
        return out

    async def _screen(self, token: LaunchpadToken) -> LaunchpadToken:
        snap = (token.meta or {}).get("initial_snapshot") or (token.meta or {}).get("last_snapshot")
        if not snap:
            try:
                m = await self.gateway.get(
                    token.chain,
                    token.token_address,
                    lambda: self.market_data.get_market_state(token.token_address),
                )
                if isinstance(m, MarketState):
                    snap = market_state_to_snapshot(m)
                    token.meta = dict(token.meta or {})
                    token.meta["last_snapshot"] = snap
                    if m.launchpad and not token.launchpad:
                        token.launchpad = m.launchpad
                    if m.symbol and not token.symbol:
                        token.symbol = m.symbol
                    if m.creator_address and not token.creator_address:
                        token.creator_address = m.creator_address
                    if m.token_created_at and not token.launched_at:
                        token.launched_at = m.token_created_at
            except Exception:
                token.status = TokenStatus.SCREENING
                await self.store.upsert(token)
                return token

        liq = (snap or {}).get("liquidity") or 0
        min_liq = self.discovery.config.min_liquidity
        if liq and min_liq and liq < min_liq * 0.1:
            token.status = TokenStatus.REJECTED
            token.priority = "DEAD"
            await self.store.upsert(token)
            return token

        score = await self._score_from_snap(token, snap or {})
        token.initial_score = score
        token.current_score = score
        token.last_score_at = utcnow()
        thresh = self.discovery.config.candidate_score_threshold
        if score is not None and score >= thresh:
            token.status = TokenStatus.WATCHING
            token.priority = "HOT" if score >= 70 else "WARM"
        elif score is not None and score >= thresh * 0.6:
            token.status = TokenStatus.SCREENING
            token.priority = "WARM"
        else:
            token.status = TokenStatus.REJECTED
            token.priority = "COLD"
        if snap:
            await self.store.write_market_snapshot(token, snap)
        else:
            await self.store.upsert(token)
        return token

    async def _fetch_state(self, token: LaunchpadToken) -> dict:
        """Refresh market state — keeps price/mcap/liquidity/holders live."""
        async def _fetch():
            return await self.market_data.get_market_state(token.token_address)

        m = await self.gateway.get(
            token.chain, token.token_address, _fetch, kind="state", ttl_s=12.0,
        )
        if isinstance(m, MarketState):
            snap = market_state_to_snapshot(m)
            if m.launchpad and not token.launchpad:
                token.launchpad = m.launchpad
            if m.symbol and not token.symbol:
                token.symbol = m.symbol
            if m.creator_address and not token.creator_address:
                token.creator_address = m.creator_address
            if m.token_created_at and not token.launched_at:
                token.launched_at = m.token_created_at
            await self.store.write_market_snapshot(token, snap)
            return snap
        raise DataUnavailable(f"no market state for {token.token_key}")

    async def _score(self, token: LaunchpadToken, snap: dict) -> float:
        return await self._score_from_snap(token, snap)

    async def _score_from_snap(self, token: LaunchpadToken, snap: dict) -> float:
        try:
            ts = snap.get("timestamp")
            if isinstance(ts, str):
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, datetime):
                timestamp = ts
            else:
                timestamp = utcnow()
            created = snap.get("token_created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            m = MarketState(
                chain=snap.get("chain") or token.chain,
                token_address=snap.get("token_address") or token.token_address,
                timestamp=timestamp,
                launchpad=snap.get("launchpad") or token.launchpad,
                symbol=snap.get("symbol") or token.symbol,
                creator_address=snap.get("creator_address") or token.creator_address,
                token_created_at=created or token.launched_at,
                price=snap.get("price"),
                market_cap=snap.get("market_cap"),
                liquidity=snap.get("liquidity"),
                volume_5m=snap.get("volume_5m"),
                volume_15m=snap.get("volume_15m"),
                buys_5m=snap.get("buys_5m"),
                sells_5m=snap.get("sells_5m"),
                unique_buyers_5m=snap.get("unique_buyers_5m"),
                unique_sellers_5m=snap.get("unique_sellers_5m"),
                holder_count=snap.get("holder_count"),
                holder_growth_pct=snap.get("holder_growth_pct"),
                top10_holder_pct=snap.get("top10_holder_pct"),
                price_change_5m=snap.get("price_change_5m"),
                price_change_15m=snap.get("price_change_15m"),
            )
            sig = self.strategy.score(m, utcnow())
            return float(sig.score) if sig and sig.score is not None else 0.0
        except Exception:
            log.exception("score failed for %s", token.token_key)
            return 0.0