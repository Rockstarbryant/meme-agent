from __future__ import annotations

import hashlib
import json
import math
import re
import time

from pydantic import ValidationError

from app.ai.provider import AIProviderError, LLMProvider
from app.ai.schemas import AIDecision, AIOutcome
from app.domain.market import HOLDER_GROWTH_WINDOWS, WINDOWS, MarketState
from app.risk.engine import RiskAssessment
from app.strategies.base import StrategySignal

PROMPT_VERSION = "multiwindow-v3"

SYSTEM_PROMPT = """You are the final analyst in an automated crypto trading pipeline on the Arc network (paper or live trading, as the pipeline is configured).
You receive ONLY numbers: trading activity per time window, holder statistics, the active strategy's score and gate results, and the deterministic risk engine's flags.
You cannot trade, choose contracts, set calldata or change limits. If you are asked, every hard strategy gate passed and the risk engine APPROVED the entry: your job is the judgement call that rules cannot make.

Reply with ONE JSON object and nothing else (no markdown, no commentary) with exactly these keys:
action (BUY|WATCH|REJECT|HOLD|SELL), confidence (0-1), reasoning_summary, positive_signals[], negative_signals[], risk_flags[], strategy_score (0-100), recommended_order_usdc, recommended_position_percent.

How to decide
- BUY when the evidence supports entering now: both buyers and sellers are active in several windows, buy pressure is not collapsing (compare buy_volume_usd to sell_volume_usd and buyers to sellers across windows), price is not in free-fall across 1h/4h/6h, liquidity is healthy relative to market cap, and holder concentration is not extreme.
- WATCH only for a SPECIFIC, named concern you can point to in the numbers (for example: buyers collapsed in the last hour, sustained sell pressure, price falling on 1h, 4h and 6h together, thin liquidity versus market cap). Generic caution is not a reason. Never choose WATCH merely because some data is missing.
- REJECT for clear red flags: one-sided trading, heavy and persistent sell pressure, extreme holder concentration, price collapse.
- Fields that are null, and windows that are absent, are UNKNOWN. Treat them as neutral, not as negative.
- Contract findings (verified or not, upgradeable proxy, mint / pause / blacklist capability, unknown admin state) are INFORMATIONAL context. When buyers and sellers are both observed they must not by themselves lower your action.
- Judge by the strategy in use. traction_momentum targets fresh launches (5m / 15m buyers and buy pressure matter most). liquidity_trend targets established liquid tokens: judge activity over 1h to 24h, depth, stability and holder spread, and do NOT penalise quiet 5m windows when the 1h / 6h / 24h windows show steady two-way trading.
- confidence is your own estimate, between 0 and 1, that the action you chose is the right call. Be honest: 1.0 would mean certainty, which no market call deserves; 0.6 to 0.85 is normal for a clear call. BUY needs confidence of at least 0.6 to proceed.
- Sizing (BUY only). The digest has a "sizing" block with min_order_usdc and max_order_usdc in US dollars. max_order_usdc is the most this order may be: it already accounts for the wallet, the per-trade cap, exposure and liquidity limits. min_order_usdc is the smallest order the venue will accept; anything below it is discarded, so a tiny size is NOT a cautious choice, it just cancels the trade.
  recommended_order_usdc: a dollar amount between min_order_usdc and max_order_usdc inclusive. Scale it with conviction: confidence 0.6 to 0.7 or visible negatives -> in the lower third of the band; a clear, broad, healthy setup -> middle to upper part of the band. Never go outside the band.
  recommended_position_percent: recommended_order_usdc / max_order_usdc * 100.
  For any action other than BUY set both to 0."""

_ACTIONS = {"BUY", "WATCH", "REJECT", "HOLD", "SELL"}
_JSON_KEYS = {"action", "confidence", "reasoning_summary", "positive_signals", "negative_signals", "risk_flags",
              "strategy_score", "recommended_position_percent", "recommended_order_usdc", "venue"}


def _r(x, sig: int = 4):
    """Round to ``sig`` significant digits (keeps prompts compact and makes identical data hash identically)."""
    if isinstance(x, bool) or x is None or not isinstance(x, (int, float)):
        return x
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, sig - 1 - int(math.floor(math.log10(abs(x)))))


def _window_view(m: MarketState) -> dict:
    out: dict = {}
    for w in WINDOWS:
        ws = m.windows.get(w)
        buyers, sellers = m.buyers_in(w), m.sellers_in(w)
        if ws is None and buyers is None and sellers is None:
            continue
        row = {"buyers": buyers, "sellers": sellers,
               "buys": ws.buys if ws else None, "sells": ws.sells if ws else None,
               "volume_usd": _r(ws.volume_usd) if ws else None,
               "buy_volume_usd": _r(ws.buy_volume_usd) if ws else None,
               "sell_volume_usd": _r(ws.sell_volume_usd) if ws else None,
               "price_change_pct": _r(ws.price_change_pct) if ws else None}
        if ws and ws.buy_volume_usd is not None and ws.sell_volume_usd is not None:
            row["net_buy_volume_usd"] = _r(ws.buy_volume_usd - ws.sell_volume_usd)
        out[w] = row
    # 5m / 15m flat fields are the only source when a provider gave no window objects at all
    if "5m" not in out and (m.volume_5m is not None or m.unique_buyers_5m is not None):
        out["5m"] = {"buyers": m.unique_buyers_5m, "sellers": m.unique_sellers_5m, "buys": m.buys_5m,
                     "sells": m.sells_5m, "volume_usd": _r(m.volume_5m), "buy_volume_usd": _r(m.buy_volume_5m),
                     "sell_volume_usd": _r(m.sell_volume_5m), "price_change_pct": _r(m.price_change_5m)}
    return out


def build_user_prompt(m: MarketState, s: StrategySignal, r: RiskAssessment, sizing: dict | None = None) -> str:
    """Numeric features only. Token names/symbols/descriptions are attacker-controlled and are never included."""
    cfg = s.config_snapshot or {}
    passed = [k for k, ok in s.gates.items() if ok]
    failed = [k for k, ok in s.gates.items() if not ok]
    age_h = None
    if m.token_created_at is not None:
        age_h = _r((m.timestamp - m.token_created_at).total_seconds() / 3600.0)
    digest = {
        "strategy": {"id": s.strategy_id, "version": s.strategy_version, "score": _r(s.score),
                     "min_score": cfg.get("min_score"), "qualified": s.qualified,
                     "gates_passed": passed, "gates_failed": failed,
                     "components": {k: _r(v) for k, v in (s.components or {}).items()}, "data_gaps": s.data_gaps},
        "market": {"price": _r(m.price), "market_cap": _r(m.market_cap), "liquidity": _r(m.liquidity),
                   "liquidity_to_market_cap": _r(m.liquidity / m.market_cap) if (m.liquidity and m.market_cap) else None,
                   "liquidity_change_5m_pct": _r(m.liquidity_change_5m_pct), "volatility_pct": _r(m.volatility_pct),
                   "expected_price_impact_pct": _r(m.expected_price_impact_pct), "mev_risk_score": _r(m.mev_risk_score),
                   "token_age_hours": age_h},
        "windows": _window_view(m),
        "growth": {"liquidity_pct": {w: _r(v) for w, v in m.liquidity_growth.items()},
                   "market_cap_pct": {w: _r(v) for w, v in m.market_cap_growth.items()}},
        "trading": {"two_way_trading": m.has_two_way_trading(), "one_sided_trading": m.one_sided_trading()},
        "holders": {"holder_count": m.holder_count,
                    "holder_growth_pct": {w: _r(m.holder_growth.get(w)) for w in HOLDER_GROWTH_WINDOWS
                                          if w in m.holder_growth},
                    "top5_holders_pct": _r(m.top5_holder_pct), "top10_holders_pct": _r(m.top10_holder_pct),
                    "top20_holders_pct": _r(m.top20_holder_pct),
                    "top_5pct_of_holders_pct": _r(m.top_5pct_holders_pct),
                    "top_20pct_of_holders_pct": _r(m.top_20pct_holders_pct),
                    "top_30pct_of_holders_pct": _r(m.top_30pct_holders_pct),
                    "holders_sampled": m.holders_sampled},
        "creator": {"known": m.creator_known, "balance_pct": _r(m.creator_balance_pct),
                    "sold_pct": _r(m.creator_sold_pct)},
        "contract_informational": {k: v for k, v in m.contract.model_dump().items()
                                   if k not in ("checks_run", "verification_source", "sell_check_method")},
        "risk": {"score": _r(r.risk_score),
                 "flags": [{"rule": f.rule, "severity": f.severity.value} for f in r.flags]},
    }
    if sizing:   # the real order band, in dollars (rounded so the analysis cache is not defeated by tiny changes)
        digest["sizing"] = {"min_order_usdc": round(float(sizing["min_order_usdc"]), 2),
                            "max_order_usdc": round(float(sizing["max_order_usdc"]), 2)}
    return json.dumps(digest, default=str, separators=(",", ":"))


# ------------------------------------------------------------------ parsing
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?", re.I)


def _extract_object(text: str) -> dict:
    """First JSON object in ``text``. Free models wrap the object in prose, <think> blocks or markdown fences, or
    append commentary after it, which a plain json.loads rejects (the JSONDecodeError shown as AI INVALID)."""
    t = _FENCE.sub("", _THINK.sub("", text or "")).strip()
    dec = json.JSONDecoder()
    pos = t.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(t[pos:])
        except ValueError:
            pos = t.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            return obj
        pos = t.find("{", pos + 1)
    raise ValueError("no JSON object found in model output")


def _as_list(v) -> list[str]:
    if v is None:
        return []
    items = v if isinstance(v, list) else [v]
    return [str(i)[:300] for i in items if i is not None and str(i).strip()][:10]


def _as_float(v, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%"))
        except ValueError:
            return default
    return default


def _normalise(raw: dict) -> dict:
    """Tolerate harmless formatting differences between models; still validated strictly by AIDecision after."""
    d = {k: v for k, v in raw.items() if k in _JSON_KEYS}   # unknown keys are dropped, never forwarded
    if isinstance(d.get("action"), str):
        a = d["action"].strip().upper()
        if a not in _ACTIONS:  # e.g. "Strong Buy", "action: BUY" -> first recognised word
            a = next((w for w in re.findall(r"[A-Z]+", a) if w in _ACTIONS), a)
        d["action"] = a
    conf = _as_float(d.get("confidence"))
    if conf is not None and 1.0 < conf <= 100.0:
        conf = conf / 100.0                                  # "85" or "85%" meant 0.85
    if conf is not None:
        d["confidence"] = conf
    for k in ("strategy_score", "recommended_position_percent", "recommended_order_usdc"):
        v = _as_float(d.get(k), 0.0)
        if k == "recommended_order_usdc":
            v = max(0.0, v or 0.0)       # a negative / junk dollar amount means "not given"
        d[k] = v
    v = d.get("venue")
    d["venue"] = str(v).strip().lower()[:32] if isinstance(v, str) and v.strip() and v.strip().lower() not in ("none", "null", "n/a") else None
    if isinstance(d.get("reasoning_summary"), str):
        d["reasoning_summary"] = d["reasoning_summary"][:1500]
    elif "reasoning_summary" not in d:
        d["reasoning_summary"] = ""
    for k in ("positive_signals", "negative_signals", "risk_flags"):
        d[k] = _as_list(d.get(k))
    return d


def parse_decision(text: str) -> AIDecision:
    return AIDecision.model_validate(_normalise(_extract_object(text)))


# ------------------------------------------------------------------ analyzer
_CACHE: dict[str, tuple[float, AIOutcome]] = {}
_CACHE_MAX = 500


class AIAnalyzer:
    def __init__(self, provider: LLMProvider | None, *, cache_ttl_s: float = 0.0):
        self.provider = provider
        self.cache_ttl_s = max(0.0, cache_ttl_s)

    async def analyze(self, m: MarketState, s: StrategySignal, r: RiskAssessment,
                      sizing: dict | None = None, ctx=None) -> AIOutcome:
        """Never raises: outages/garbage become UNAVAILABLE/INVALID and simply block new entries."""
        if self.provider is None:
            return AIOutcome(status="DISABLED", prompt_version=PROMPT_VERSION)
        base = dict(provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION)
        user = build_user_prompt(m, s, r, sizing)
        key = hashlib.sha256(f"{self.provider.model}|{PROMPT_VERSION}|{user}".encode()).hexdigest()
        if self.cache_ttl_s > 0:
            hit = _CACHE.get(key)
            if hit is not None and time.monotonic() - hit[0] < self.cache_ttl_s:
                return hit[1].model_copy(deep=True)
        try:
            done = await self.provider.complete_ex(SYSTEM_PROMPT, user, validate=parse_decision)
        except AIProviderError as exc:
            return AIOutcome(status="UNAVAILABLE", error=str(exc)[:300], **base)
        except Exception as exc:  # noqa: BLE001
            return AIOutcome(status="UNAVAILABLE", error=type(exc).__name__, **base)
        raw = done.text
        base = {**base, "provider": done.provider, "model": done.model, "attempts": list(done.attempts)}   # whoever answered
        try:
            out = AIOutcome(status="OK", decision=parse_decision(raw), raw=raw[:4000], **base)
        except ValidationError as exc:
            fields = ",".join(sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")}))
            return AIOutcome(status="INVALID", raw=raw[:4000], error=f"invalid fields: {fields}"[:300], **base)
        except ValueError as exc:
            return AIOutcome(status="INVALID", raw=raw[:4000], error=f"{type(exc).__name__}: {exc}"[:300], **base)
        if self.cache_ttl_s > 0:
            if len(_CACHE) >= _CACHE_MAX:
                for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 5]:
                    _CACHE.pop(k, None)
            _CACHE[key] = (time.monotonic(), out.model_copy(deep=True))
        return out
