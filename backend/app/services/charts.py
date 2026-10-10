"""OHLCV candles for the token page, with provider fallback and per-call audit.

Order: Codex ``getBars`` (when a key is set; follows the token across launchpad migrations) -> GeckoTerminal pool OHLCV (keyless)
-> candles built from our own market snapshots (always available, coarse). The response names the provider that produced the
candles and lists the providers that failed on the way, so the chart can say where its data came from.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.errors import DataUnavailable
from app.observability.audit import AuditKind, AuditStatus, record_event

TIMEFRAMES: dict[str, dict[str, Any]] = {
    # tf: (seconds per candle, Codex resolution, GeckoTerminal timeframe + aggregate)
    "1m": {"s": 60, "codex": "1", "gt": ("minute", 1)},
    "5m": {"s": 300, "codex": "5", "gt": ("minute", 5)},
    "15m": {"s": 900, "codex": "15", "gt": ("minute", 15)},
    "1h": {"s": 3600, "codex": "60", "gt": ("hour", 1)},
    "4h": {"s": 14400, "codex": "240", "gt": ("hour", 4)},
    "1d": {"s": 86400, "codex": "1D", "gt": ("day", 1)},
}


def _candle(t, o, h, l, c, v) -> dict | None:
    try:
        t, o, h, l, c = int(t), float(o), float(h), float(l), float(c)
        v = float(v or 0.0)
    except (TypeError, ValueError):
        return None
    if min(o, h, l, c) <= 0 or h < l or t <= 0:
        return None
    return {"t": t, "o": o, "h": max(h, o, c), "l": min(l, o, c), "c": c, "v": max(v, 0.0)}


async def codex_candles(client, token: str, tf: str, limit: int) -> list[dict]:
    spec = TIMEFRAMES[tf]
    end = int(time.time())
    data = await client.bars(token, start=end - spec["s"] * limit, end=end, resolution=spec["codex"], countback=limit)
    ts, o, h, l, c, v = (data.get(k) or [] for k in ("t", "o", "h", "l", "c", "volume"))
    out = [x for x in (_candle(*row) for row in zip(ts, o, h, l, c, v or [0] * len(ts))) if x]
    if not out:
        raise DataUnavailable("Codex returned no bars")
    return sorted(out, key=lambda x: x["t"])


async def gecko_candles(http: httpx.AsyncClient, base: str, network: str, pool: str, tf: str, limit: int) -> list[dict]:
    unit, agg = TIMEFRAMES[tf]["gt"]
    try:
        r = await http.get(f"{base.rstrip('/')}/networks/{network}/pools/{pool}/ohlcv/{unit}",
                           params={"aggregate": agg, "limit": min(limit, 1000), "currency": "usd"},
                           headers={"accept": "application/json;version=20230302"})
    except httpx.HTTPError as exc:
        raise DataUnavailable(f"GeckoTerminal network error: {type(exc).__name__}") from exc
    if r.status_code == 429:
        raise DataUnavailable("GeckoTerminal rate limited (HTTP 429)")
    if r.status_code >= 400:
        raise DataUnavailable(f"GeckoTerminal HTTP {r.status_code}")
    rows = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    out = [x for x in (_candle(*row[:6]) for row in rows if isinstance(row, list) and len(row) >= 6) if x]
    if not out:
        raise DataUnavailable("GeckoTerminal returned no candles for this pool")
    return sorted(out, key=lambda x: x["t"])


def snapshot_candles(history: list[dict], tf: str) -> list[dict]:
    """Candles from our own snapshots (price at snapshot time). Coarse: used only when no external provider answered."""
    step = TIMEFRAMES[tf]["s"]
    buckets: dict[int, list[float]] = {}
    for h in history:
        try:
            from datetime import datetime
            ts = int(datetime.fromisoformat(str(h["at"]).replace("Z", "+00:00")).timestamp())
            price = float((h.get("data") or {}).get("price"))
        except (TypeError, ValueError, KeyError):
            continue
        if price > 0:
            buckets.setdefault(ts // step * step, []).append(price)
    return [{"t": t, "o": p[0], "h": max(p), "l": min(p), "c": p[-1], "v": 0.0} for t, p in sorted(buckets.items())]


class ChartService:
    def __init__(self, settings, *, codex_client=None, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.codex = codex_client
        self.http = http or httpx.AsyncClient(timeout=8.0)

    async def candles(self, token: str, pool: str | None, tf: str, limit: int, history: list[dict]) -> dict:
        if tf not in TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        limit = max(20, min(int(limit), 500))
        failures: list[dict] = []
        tk = f"arc:{token.lower()}"
        attempts: list[tuple[str, Any]] = []
        if self.codex is not None:
            attempts.append(("codex", lambda: codex_candles(self.codex, token, tf, limit)))
        if pool:
            attempts.append(("geckoterminal", lambda: gecko_candles(self.http, self.s.geckoterminal_base_url,
                                                                    self.s.geckoterminal_network, pool, tf, limit)))
        for name, fn in attempts:
            t0 = time.monotonic()
            try:
                out = await fn()
            except DataUnavailable as exc:
                failures.append({"provider": name, "error": str(exc)[:160]})
                record_event(AuditKind.CHART_PROVIDER, AuditStatus.FAILED, provider=name, operation=f"candles:{tf}",
                             component="charts", latency_ms=(time.monotonic() - t0) * 1000, error=str(exc), token_key=tk)
                continue
            record_event(AuditKind.CHART_PROVIDER, AuditStatus.OK if not failures else AuditStatus.DEGRADED, provider=name,
                         operation=f"candles:{tf}", component="charts", latency_ms=(time.monotonic() - t0) * 1000, token_key=tk,
                         detail={"candles": len(out), "failed_before": [f["provider"] for f in failures]})
            return {"tf": tf, "provider": name, "candles": out[-limit:], "failed": failures, "has_volume": any(c["v"] > 0 for c in out)}
        snap = snapshot_candles(history, tf)
        record_event(AuditKind.CHART_PROVIDER, AuditStatus.DEGRADED, provider="snapshots", operation=f"candles:{tf}",
                     component="charts", token_key=tk, error="no external chart provider answered; using own snapshots",
                     detail={"candles": len(snap), "failed": [f["provider"] for f in failures]})
        return {"tf": tf, "provider": "snapshots", "candles": snap[-limit:], "failed": failures, "has_volume": False}

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.codex is not None:
            await self.codex.aclose()
