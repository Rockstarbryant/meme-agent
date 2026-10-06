"""Portfolio history downsampling and trading performance statistics (pure functions, no database access)."""
from __future__ import annotations

from typing import Any, Iterable, Sequence


def downsample(points: Sequence[tuple[float, float]], max_points: int = 240) -> list[tuple[float, float]]:
    """Reduce a time series to at most ``max_points`` while KEEPING its shape: the first and last points and each
    bucket's minimum and maximum survive, so a short dip or spike is never averaged away (a plain stride would
    erase exactly the moves a user wants to see)."""
    pts = list(points)
    if max_points < 4 or len(pts) <= max_points:
        return pts
    per_bucket = 2  # min + max of each bucket
    buckets = max(1, (max_points - 2) // per_bucket)
    inner = pts[1:-1]
    size = -(-len(inner) // buckets)
    out = [pts[0]]
    for i in range(0, len(inner), size):
        chunk = inner[i:i + size]
        lo, hi = min(chunk, key=lambda p: p[1]), max(chunk, key=lambda p: p[1])
        out.extend(sorted({lo, hi}, key=lambda p: p[0]))
    out.append(pts[-1])
    return out


def performance(positions: Iterable[Any]) -> dict:
    """Win rate, profit factor, average win / loss, best / worst, hold time, and breakdowns by strategy and exit
    reason, from CLOSED positions (objects with realized_pnl_usdc, total_invested_usdc, exit_reason, strategy_id,
    opened_at, closed_at)."""
    closed = [p for p in positions if getattr(p, "status", "CLOSED") == "CLOSED"]
    n = len(closed)
    base = {"closed": n, "wins": 0, "losses": 0, "win_rate_pct": None, "profit_factor": None, "avg_win_usdc": None,
            "avg_loss_usdc": None, "best_usdc": None, "worst_usdc": None, "avg_hold_seconds": None,
            "total_realized_usdc": 0.0, "by_strategy": [], "by_exit_reason": []}
    if not n:
        return base
    pnl = [float(p.realized_pnl_usdc or 0.0) for p in closed]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    holds = []
    for p in closed:
        if p.opened_at and p.closed_at:
            holds.append((p.closed_at - p.opened_at).total_seconds())

    def group(key) -> list[dict]:
        acc: dict[str, dict] = {}
        for p in closed:
            k = key(p) or "UNKNOWN"
            g = acc.setdefault(k, {"key": k, "count": 0, "wins": 0, "pnl_usdc": 0.0})
            g["count"] += 1
            g["pnl_usdc"] += float(p.realized_pnl_usdc or 0.0)
            g["wins"] += 1 if (p.realized_pnl_usdc or 0.0) > 0 else 0
        return sorted(({**g, "pnl_usdc": round(g["pnl_usdc"], 6), "win_rate_pct": round(g["wins"] / g["count"] * 100, 1)} for g in acc.values()),
                      key=lambda g: -g["count"])

    base.update({
        "wins": len(wins), "losses": len(losses), "win_rate_pct": round(len(wins) / n * 100, 1),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_win_usdc": round(gross_win / len(wins), 6) if wins else None,
        "avg_loss_usdc": round(-gross_loss / len(losses), 6) if losses else None,
        "best_usdc": round(max(pnl), 6), "worst_usdc": round(min(pnl), 6),
        "avg_hold_seconds": round(sum(holds) / len(holds)) if holds else None,
        "total_realized_usdc": round(sum(pnl), 6),
        "by_strategy": group(lambda p: p.strategy_id),
        "by_exit_reason": group(lambda p: getattr(p.exit_reason, "value", p.exit_reason)),
    })
    return base


def derive_starting_value(stored: float | None, total_value: float, realized: float, unrealized: float) -> tuple[float, bool]:
    """(starting value, derived?). A paper portfolio whose stored starting cash is missing or zero shows "started
    $0.00" and a meaningless return; the true start is what it is worth now minus what it has earned since."""
    if stored is not None and stored > 0:
        return float(stored), False
    start = total_value - realized - unrealized
    return (max(start, 0.0), True)
