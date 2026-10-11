"""OKX Market API provider for Arc: TRENDING discovery + price/market stats.

Why: launchpad-event discovery only finds tokens that were just launched. OKX's token ranking (``/market/token/toplist``) ranks the
chain's tokens by 1h/4h/24h volume and price change, so tokens that are already trending are found too.

  * discover_tokens(): union of several rankings (volume 1h, change 1h, change 4h, volume 24h), filtered by liquidity / holders / txs and
    capped, ordered by how many rankings a token appears in (then liquidity).
  * get_market_state(): from the cached ranking row when fresh, else ``price-info``.
  * Chain support is CHECKED, not assumed: OKX's Trade API added Arc on Sept 10 2026, but its Market API chain list must say so too.
    If ``supported/chain`` is reachable and does not list the configured chainIndex, the provider fails with a precise message
    (visible in the admin audit log) instead of quietly returning nothing.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from app.chains.base import MarketDataProvider
from app.core.errors import DataUnavailable
from app.domain.market import ContractInfo, MarketState, WindowStats
from app.integrations.okx import OkxClient

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TF = {1: "5m", 2: "1h", 3: "4h", 4: "24h"}
DEFAULT_RANKINGS: tuple[tuple[int, int], ...] = ((5, 2), (2, 2), (2, 3), (5, 4))   # (sortBy, timeFrame)


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _int(v: Any) -> int | None:
    x = _num(v)
    return None if x is None else int(x)


class OkxArcMarketData(MarketDataProvider):
    name = "okx"

    def __init__(self, client: OkxClient, *, chain_index: str = "5042", max_tokens: int = 40, min_liquidity: float = 5_000.0,
                 min_holders: int = 25, min_txs: int = 10, cache_s: float = 120.0, rankings: tuple[tuple[int, int], ...] = DEFAULT_RANKINGS,
                 hot_tokens: bool = False):
        self.client, self.chain_index = client, str(chain_index)
        self.max_tokens, self.min_liquidity, self.min_holders, self.min_txs = max_tokens, min_liquidity, min_holders, min_txs
        self.cache_s, self.rankings, self.hot_tokens = max(10.0, cache_s), rankings, hot_tokens
        self._rows: dict[str, tuple[float, dict[str, dict]]] = {}     # token -> (fetched_at, {timeframe label: row})
        self._chain_ok: tuple[float, bool] | None = None

    async def _ensure_chain(self) -> None:
        if self._chain_ok and time.monotonic() - self._chain_ok[0] < 6 * 3600:
            if not self._chain_ok[1]:
                raise DataUnavailable(f"OKX Market API does not list chainIndex {self.chain_index} (Arc) yet; Trade API-only support")
            return
        try:
            chains = await self.client.supported_chains()
        except DataUnavailable:
            return                       # cannot verify right now: let the real call below succeed or fail on its own merits
        ok = any(str(c.get("chainIndex")) == self.chain_index for c in chains if isinstance(c, dict))
        self._chain_ok = (time.monotonic(), ok)
        if not ok:
            raise DataUnavailable(f"OKX Market API does not list chainIndex {self.chain_index} (Arc) yet; Trade API-only support")

    def _remember(self, row: dict, label: str) -> None:
        addr = str(row.get("tokenContractAddress") or "").lower()
        if not _ADDR.match(addr):
            return
        ts, by = self._rows.get(addr, (0.0, {}))
        by = dict(by)
        by[label] = row
        self._rows[addr] = (time.monotonic(), by)

    def _passes(self, row: dict) -> bool:
        liq, holders, txs = _num(row.get("liquidity")), _int(row.get("holders")), _int(row.get("txs"))
        if liq is None or liq < self.min_liquidity or (_num(row.get("price")) or 0) <= 0:
            return False
        if holders is not None and holders < self.min_holders:
            return False
        return txs is None or txs >= self.min_txs

    async def discover_tokens(self) -> list[str]:
        await self._ensure_chain()
        score: dict[str, list[float]] = {}
        errors: list[str] = []
        for sort_by, tf in self.rankings:
            try:
                rows = await self.client.toplist(self.chain_index, sort_by=sort_by, time_frame=tf)
            except DataUnavailable as exc:
                errors.append(str(exc))
                if "rate limited" in str(exc) or "credentials" in str(exc):
                    break
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                self._remember(row, _TF.get(tf, str(tf)))
                addr = str(row.get("tokenContractAddress") or "").lower()
                if _ADDR.match(addr) and self._passes(row):
                    e = score.setdefault(addr, [0.0, 0.0])
                    e[0] += 1
                    e[1] = max(e[1], _num(row.get("liquidity")) or 0.0)
        if self.hot_tokens:
            try:
                for row in await self.client.hot_tokens(self.chain_index):
                    addr = str((row or {}).get("tokenContractAddress") or "").lower()
                    if _ADDR.match(addr) and self._passes({**row, "txs": None}):
                        e = score.setdefault(addr, [0.0, 0.0])
                        e[0] += 1
                        e[1] = max(e[1], _num(row.get("liquidity")) or 0.0)
            except DataUnavailable as exc:
                errors.append(f"hot-token: {exc}")
        if not score and errors:
            raise DataUnavailable("OKX trending discovery failed: " + " | ".join(errors)[:300])
        ordered = sorted(score.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
        return [a for a, _ in ordered[: self.max_tokens]]

    def _state(self, token: str, by: dict[str, dict], extra: dict | None = None) -> MarketState:
        base = {**(next(iter(by.values()), None) or {}), **(extra or {})}
        windows: dict[str, WindowStats] = {}
        for label, row in by.items():
            windows[label] = WindowStats(volume_usd=_num(row.get("volume")), price_change_pct=_num(row.get("change")),
                                         buys=_int(row.get("txsBuy")), sells=_int(row.get("txsSell")), basis="pool_stats")
        first = _int(base.get("firstTradeTime"))
        w5 = windows.get("5m")
        return MarketState(
            chain="arc", token_address=token, timestamp=datetime.now(timezone.utc),
            symbol=str(base.get("tokenSymbol")) if base.get("tokenSymbol") else None,
            price=_num(base.get("price")), liquidity=_num(base.get("liquidity")), market_cap=_num(base.get("marketCap")),
            holder_count=_int(base.get("holders")),
            token_created_at=datetime.fromtimestamp(first / 1000, timezone.utc) if first else None,
            volume_5m=w5.volume_usd if w5 else None, price_change_5m=w5.price_change_pct if w5 else None,
            buys_5m=w5.buys if w5 else None, sells_5m=w5.sells if w5 else None,
            windows=windows, contract=ContractInfo(), data_sources=["okx:toplist" if by else "okx:price-info"], is_demo=False)

    async def get_market_state(self, token_address: str) -> MarketState:
        token = token_address.lower()
        hit = self._rows.get(token)
        if hit and time.monotonic() - hit[0] < self.cache_s:
            st = self._state(token, hit[1])
            if st.price is not None:
                return st
        await self._ensure_chain()
        info = await self.client.price_info(self.chain_index, token)
        if not info or _num(info.get("price")) is None:
            raise DataUnavailable(f"OKX has no price data for {token} on chainIndex {self.chain_index}")
        by: dict[str, dict] = {}
        for sfx, label in (("5M", "5m"), ("1H", "1h"), ("4H", "4h"), ("24H", "24h")):
            vol, chg = info.get(f"volume{sfx}"), info.get(f"priceChange{sfx}")
            if vol is not None or chg is not None:
                by[label] = {"volume": vol, "change": chg, "txsBuy": None, "txsSell": None}
        st = self._state(token, by, extra=info)
        st.data_sources = ["okx:price-info"]
        return st

    async def aclose(self) -> None:
        await self.client.aclose()
