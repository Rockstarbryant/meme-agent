"""Builds per-window trading stats (see MarketState.windows) from provider payloads.

Two kinds of source, merged by ``merge_window_sources``:

  * POOL STATS  rolling windows the provider already computed (GeckoTerminal: 5m / 15m / 1h / 6h / 24h). Exact over
                the whole window, but only for the window sizes the provider offers and without a USD buy/sell split.
  * TRADES      the pool's recent trade list (GeckoTerminal returns at most the last 300 trades inside 24h). From it
                we can compute ANY window (2h, 4h, 12h ...) plus real USD buy/sell volume, but ONLY for windows the
                list fully covers. A window the list does not fully cover is left out rather than reported as a
                partial (under-counted) number.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.domain.market import WINDOW_SECONDS, WINDOWS, WindowStats

TRADES_PAGE_LIMIT = 300          # GeckoTerminal's trades endpoint cap per page
_GECKO_KEYS = {"5m": "m5", "15m": "m15", "1h": "h1", "6h": "h6", "24h": "h24"}
_PAPRIKA_KEYS = ("5m", "15m", "1h", "6h", "24h")


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _has_any(w: WindowStats) -> bool:
    return any(getattr(w, f) is not None for f in WindowStats.model_fields if f != "basis")


# ------------------------------------------------------------------ pool stats
def windows_from_gecko_pool(attrs: dict) -> dict[str, WindowStats]:
    txs = attrs.get("transactions") or {}
    vols = attrs.get("volume_usd") or {}
    chg = attrs.get("price_change_percentage") or {}
    out: dict[str, WindowStats] = {}
    for w, k in _GECKO_KEYS.items():
        t = txs.get(k) or {}
        ws = WindowStats(
            buyers=_int(t.get("buyers")), sellers=_int(t.get("sellers")),
            buys=_int(t.get("buys")), sells=_int(t.get("sells")),
            volume_usd=_num(vols.get(k)), price_change_pct=_num(chg.get(k)), basis="pool_stats",
        )
        if _has_any(ws):
            out[w] = ws
    return out


def windows_from_dexpaprika(summary: dict) -> dict[str, WindowStats]:
    """Best effort: DexPaprika reports counts / volume per window but no unique traders."""
    out: dict[str, WindowStats] = {}
    for w in _PAPRIKA_KEYS:
        buys, sells = _int(summary.get(f"buys_{w}")), _int(summary.get(f"sells_{w}"))
        ws = WindowStats(
            buys=buys, sells=sells, volume_usd=_num(summary.get(f"volume_usd_{w}")),
            buy_volume_usd=_num(summary.get(f"buy_usd_{w}")), sell_volume_usd=_num(summary.get(f"sell_usd_{w}")),
            price_change_pct=_num(summary.get(f"price_change_pct_{w}")), basis="pool_stats",
        )
        if _has_any(ws):
            out[w] = ws
    return out


# ------------------------------------------------------------------ trades
def _parse_trades(rows: Iterable[dict], *, target_is_base: bool) -> list[tuple[datetime, str, float, str, float | None]]:
    """-> [(ts, side relative to the TARGET token, usd, trader, price_usd_of_target)] newest first as received."""
    out = []
    for row in rows:
        a = row.get("attributes") if isinstance(row, dict) and isinstance(row.get("attributes"), dict) else row
        if not isinstance(a, dict):
            continue
        ts = _ts(a.get("block_timestamp"))
        if ts is None:
            continue
        kind = str(a.get("kind") or "").lower()
        if kind not in ("buy", "sell"):
            continue
        side = kind if target_is_base else ("sell" if kind == "buy" else "buy")
        usd = _num(a.get("volume_in_usd")) or _num(a.get("volume_usd")) or 0.0  # real field is volume_in_usd
        trader = str(a.get("tx_from_address") or a.get("from_address") or "").lower()
        p_from, p_to = _num(a.get("price_from_in_usd")), _num(a.get("price_to_in_usd"))
        # For a BASE-token buy the pool receives quote and pays base, so price_to is the base price; mirror for sells.
        if target_is_base:
            price = (p_to if kind == "buy" else p_from) or p_to or p_from
        else:
            price = (p_from if kind == "buy" else p_to) or p_from or p_to
        out.append((ts, side, usd, trader, price))
    return out


def windows_from_trades(rows: list[dict], now: datetime, *, target_is_base: bool = True,
                        page_limit: int = TRADES_PAGE_LIMIT) -> dict[str, WindowStats]:
    """Exact stats for every window the trade list FULLY covers (see module docstring)."""
    events = _parse_trades(rows, target_is_base=target_is_base)
    if not rows:
        return {}
    # A page below the cap holds every trade of the last 24h; a full page only reaches back to its oldest trade.
    if len(rows) >= page_limit and events:
        coverage_start = min(e[0] for e in events)
    else:
        coverage_start = now - timedelta(seconds=WINDOW_SECONDS["24h"])
    events.sort(key=lambda e: e[0])
    latest_price = next((e[4] for e in reversed(events) if e[4]), None)
    out: dict[str, WindowStats] = {}
    for w in WINDOWS:
        start = now - timedelta(seconds=WINDOW_SECONDS[w])
        if start + timedelta(seconds=1) < coverage_start:
            continue  # the trade list does not reach back far enough for this window
        inside = [e for e in events if e[0] >= start]
        buys = [e for e in inside if e[1] == "buy"]
        sells = [e for e in inside if e[1] == "sell"]
        before = [e for e in events if e[0] < start and e[4]]
        ref = (before[-1][4] if before else next((e[4] for e in inside if e[4]), None))
        change = None
        if ref and latest_price and inside:
            change = round((latest_price / ref - 1.0) * 100.0, 4)
        buy_vol, sell_vol = sum(e[2] for e in buys), sum(e[2] for e in sells)
        out[w] = WindowStats(
            buyers=len({e[3] for e in buys if e[3]}), sellers=len({e[3] for e in sells if e[3]}),
            buys=len(buys), sells=len(sells), volume_usd=round(buy_vol + sell_vol, 4),
            buy_volume_usd=round(buy_vol, 4), sell_volume_usd=round(sell_vol, 4),
            price_change_pct=change, basis="trades",
        )
    return out


def merge_window_sources(pool: dict[str, WindowStats], trades: dict[str, WindowStats]) -> dict[str, WindowStats]:
    """Pool stats win for counts / uniques / volume / price change (provider-computed over the whole window);
    trade history fills the windows the provider does not offer and supplies the USD buy/sell split."""
    out: dict[str, WindowStats] = {}
    for w in WINDOWS:
        p, t = pool.get(w), trades.get(w)
        if p is None and t is None:
            continue
        if p is None:
            out[w] = t  # type: ignore[assignment]
            continue
        merged = p.model_copy()
        if t is not None:
            for f in ("buyers", "sellers", "buys", "sells", "volume_usd", "price_change_pct"):
                if getattr(merged, f) is None:
                    setattr(merged, f, getattr(t, f))
            merged.buy_volume_usd, merged.sell_volume_usd = t.buy_volume_usd, t.sell_volume_usd
            merged.basis = "pool_stats+trades"
        out[w] = merged
    return out
