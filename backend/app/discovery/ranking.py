"""Candidate ranking for the shared cloud worker.

Goals (see cloud_worker._global_candidate_addresses):
  * Prefer tokens that are trending AND balanced: sensible market cap, liquidity and holder
    count, rather than "whatever has the most liquidity".
  * Rotate: every eligible token is re-evaluated eventually (stale / never-evaluated tokens
    get a bonus), so the same top-N tokens do not starve the rest.
  * Pure functions only (no I/O) so the behaviour is unit-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class RankingConfig:
    min_liquidity_usdc: float = 5_000.0
    min_market_cap_usdc: float = 5_000.0
    max_age_s: float = 24 * 3600.0       # tokens not refreshed/seen for this long are dropped
    eval_min_interval_s: float = 300.0   # do not re-evaluate a token more often than this
    max_per_cycle: int = 40


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _lin(x: float | None, lo: float, hi: float) -> float | None:
    if x is None:
        return None
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * 100.0


def _log_lin(x: float | None, lo: float, hi: float) -> float | None:
    if x is None or x <= 0:
        return None if x is None else 0.0
    return _lin(math.log10(x), math.log10(lo), math.log10(hi))


def _tent(x: float | None, lo: float, ideal_lo: float, ideal_hi: float, hi: float) -> float | None:
    """100 inside [ideal_lo, ideal_hi], falling linearly to 0 at lo / hi."""
    if x is None:
        return None
    if x <= lo or x >= hi:
        return 0.0
    if x < ideal_lo:
        return (x - lo) / (ideal_lo - lo) * 100.0
    if x > ideal_hi:
        return (hi - x) / (hi - ideal_hi) * 100.0
    return 100.0


def _mean(vals: Iterable[float | None]) -> float | None:
    got = [v for v in vals if v is not None]
    return sum(got) / len(got) if got else None


def balance_score(snap: dict) -> float:
    """0-100. High when liquidity, market cap and holders are all reasonable AND the token is active.

    Unknown values are skipped (not scored as zero) except that fewer known components cap the
    result: an unknown holder count is common on Arc and must not exclude a token on its own.
    """
    liq = _num(snap.get("liquidity"))
    mcap = _num(snap.get("market_cap"))
    holders = _num(snap.get("holder_count"))
    vol5 = _num(snap.get("volume_5m"))
    buys = _num(snap.get("buys_5m")) or 0.0
    sells = _num(snap.get("sells_5m")) or 0.0
    ch5 = _num(snap.get("price_change_5m"))
    ch15 = _num(snap.get("price_change_15m"))

    liq_s = _log_lin(liq, 5_000, 250_000)
    mcap_s = _log_lin(mcap, 10_000, 1_000_000)
    ratio = (liq / mcap) if (liq and mcap) else None
    ratio_s = _tent(ratio, 0.01, 0.05, 0.40, 1.5)       # thin pool vs cap is a rug/pump smell; huge ratio = dead cap
    holders_s = _log_lin(holders, 50, 2_000)

    parts = [liq_s, mcap_s, ratio_s, holders_s]
    known = [p for p in parts if p is not None]
    core = 0.0
    if known:
        # Penalise imbalance: blend the average with the weakest component.
        core = 0.5 * (sum(known) / len(known)) + 0.5 * min(known)

    turnover = (vol5 / liq) if (vol5 is not None and liq) else None
    trend = _mean([
        _lin(turnover, 0.0, 0.25),
        _lin(buys + sells, 0, 60),
        _lin(ch5, -5, 10),
        _lin(ch15, -10, 20),
    ])
    trend = trend if trend is not None else 0.0
    return round(0.6 * core + 0.4 * trend, 2)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def token_last_seen(token: Any) -> datetime | None:
    """Most recent evidence that we have live data for this token."""
    cands = [_as_utc(getattr(token, "last_monitored_at", None)),
             _as_utc(getattr(token, "last_score_at", None)),
             _as_utc(getattr(token, "discovered_at", None))]
    cands = [c for c in cands if c is not None]
    return max(cands) if cands else None


def is_expired(token: Any, now: datetime, max_age_s: float) -> bool:
    seen = token_last_seen(token)
    return seen is None or (now - seen).total_seconds() > max_age_s


def rank_candidates(
    tokens: Iterable[Any],
    eval_at: dict[str, datetime],
    now: datetime,
    cfg: RankingConfig,
) -> list[str]:
    """Return token addresses to evaluate this cycle, best first, at most ``cfg.max_per_cycle``."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    ranked: list[tuple[float, str]] = []
    for t in tokens:
        if (getattr(t, "priority", None) or "WARM") == "DEAD":
            continue
        if is_expired(t, now, cfg.max_age_s):
            continue
        addr = str(t.token_address).lower()
        snap = (getattr(t, "meta", None) or {}).get("last_snapshot") or {}
        if not isinstance(snap, dict):
            snap = {}
        liq = _num(snap.get("liquidity"))
        mcap = _num(snap.get("market_cap"))
        if liq is not None and liq < cfg.min_liquidity_usdc:
            continue
        if mcap is not None and mcap < cfg.min_market_cap_usdc:
            continue
        last = _as_utc(eval_at.get(addr))
        since = None if last is None else (now - last).total_seconds()
        if since is not None and since < cfg.eval_min_interval_s:
            continue
        bonus = 30.0 if since is None else min(30.0, since / cfg.eval_min_interval_s * 5.0)
        # A token we have no snapshot for yet still gets a turn (the pipeline fetches it within budget).
        base = balance_score(snap) if snap else 20.0
        ranked.append((base + bonus, addr))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [a for _, a in ranked[: max(1, cfg.max_per_cycle)]]
