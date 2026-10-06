"""Per-token liquidity / market-cap history, kept in the registry token's ``meta["hist"]``.

Providers only report the CURRENT liquidity, so "is liquidity growing?" can only come from our own samples. Entries are
``[unix_ts, liquidity, market_cap, price]``, at most one per ``MIN_SPACING_S`` and kept for ``MAX_AGE_S`` (it is saved
with the token, so it survives worker restarts).
"""
from __future__ import annotations

from typing import Any

WINDOW_SECONDS = {"1h": 3600, "6h": 21600, "24h": 86400}
MIN_SPACING_S = 600
MAX_AGE_S = 26 * 3600
_FIELD = {"liquidity": 1, "market_cap": 2, "price": 3}


def _ok(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0


def update_history(hist: list, ts: float, liquidity: float | None, market_cap: float | None, price: float | None) -> list:
    """Append a sample when enough time has passed, drop old ones. Returns the (same) list."""
    if not (_ok(liquidity) or _ok(market_cap) or _ok(price)):
        return hist
    if not hist or ts - float(hist[-1][0]) >= MIN_SPACING_S:
        hist.append([ts, liquidity if _ok(liquidity) else None, market_cap if _ok(market_cap) else None,
                     price if _ok(price) else None])
    cutoff = ts - MAX_AGE_S
    while hist and float(hist[0][0]) < cutoff:
        hist.pop(0)
    return hist


def growth(hist: list, ts: float, field: str, current: float | None) -> dict[str, float]:
    """Percent change of ``field`` from about 1h / 6h / 24h ago to ``current``; a window is reported only when a
    sample close to that age exists (0.85x to 1.6x of the window)."""
    if not _ok(current):
        return {}
    idx = _FIELD[field]
    out: dict[str, float] = {}
    for w, secs in WINDOW_SECONDS.items():
        best = None
        for row in hist:
            age = ts - float(row[0])
            val = row[idx] if len(row) > idx else None
            if _ok(val) and secs * 0.85 <= age <= secs * 1.6:
                gap = abs(age - secs)
                if best is None or gap < best[0]:
                    best = (gap, float(val))
        if best is not None:
            out[w] = round((float(current) / best[1] - 1.0) * 100.0, 2)
    return out
