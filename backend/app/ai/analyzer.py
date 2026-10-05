from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any, Iterable, Mapping

from pydantic import ValidationError

from app.ai.provider import AIProviderError, LLMProvider
from app.ai.schemas import AIDecision, AIOutcome
from app.domain.market import HOLDER_GROWTH_WINDOWS, WINDOWS, MarketState
from app.risk.engine import RiskAssessment
from app.strategies.base import StrategySignal

PROMPT_VERSION = "multiwindow-v3"

# Shared with any downstream bias-to-action override so both layers agree on
# what "confident enough to buy" means. Change in one place.
BUY_MIN_CONFIDENCE = 0.6

SYSTEM_PROMPT = """You are the final analyst in an automated crypto trading pipeline on the Arc network (currently paper trading).
You receive ONLY numbers: trading activity per time window, holder statistics, the active strategy's score and gate results, the deterministic risk engine's flags, the position budget the pipeline will enforce, and (when available) the realised P&L outcomes of this pipeline's recent decisions.
You cannot trade, choose contracts, set calldata or change limits. Every hard strategy gate has already passed and the risk engine APPROVED the entry: your job is the judgement call that rules cannot make.

Reply with ONE JSON object and nothing else (no markdown, no commentary) with exactly these keys:
action (BUY|WATCH|REJECT|HOLD|SELL), confidence (0-1), reasoning_summary, positive_signals[], negative_signals[], risk_flags[], strategy_score (0-100), recommended_position_percent (0-100, share of portfolio value).

How to decide
- BUY is the default when the deterministic layer passed and you cannot name a SPECIFIC, data-backed concern. Both buyers and sellers should be active in several windows, buy pressure must not be collapsing (compare buy_volume_usd to sell_volume_usd and buyers to sellers across windows), price must not be in free-fall across 1h/4h/6h, liquidity must be healthy relative to market cap, and holder concentration must not be extreme. If you have to search the numbers for a reason NOT to buy, that is itself a signal to buy.
- WATCH is reserved for a SPECIFIC, named concern you can point to in the numbers (for example: buyers collapsed in the last hour, sustained sell pressure, price falling on 1h, 4h and 6h together, thin liquidity versus market cap, extreme holder concentration). Generic caution is not a reason. Missing data is not a reason. If you cannot state the concern in one sentence, choose BUY instead.
- REJECT only for clear red flags: one-sided trading with no counter-flow, heavy and persistent sell pressure, extreme holder concentration, active price collapse.
- HOLD applies to an existing position you would not add to but would not exit.
- SELL applies to an existing position you would exit now.

Data interpretation
- Fields that are null are UNKNOWN. Treat them as neutral, not as negative.
- Windows listed in "windows_absent" were not reported by any provider this cycle. An absent 1h window does NOT mean zero 1h activity; it means you were not given the data. Never penalise a token for a window you were not given.
- Contract findings (verified or not, upgradeable proxy, mint / pause / blacklist capability, unknown admin state) are INFORMATIONAL context. When buyers and sellers are both observed they must not by themselves lower your action.
- "prior_outcomes" (when present) is this pipeline's recent track record: each entry has the action taken, the confidence, the strategy score, and the realised pnl_pct (or null if the position is still open). Use it to calibrate: if your recent BUYs at confidence below 0.7 have been losing, raise your bar; if the pipeline has been correctly rejecting and the market is healthy, do not overcorrect toward caution.

Judge by the strategy in use
- traction_momentum targets fresh launches: 5m / 15m buyers and buy pressure matter most.
- liquidity_trend targets established liquid tokens: judge activity over 1h to 24h, depth, stability and holder spread, and do NOT penalise quiet 5m windows when the 1h / 6h / 24h windows show steady two-way trading.

Position sizing
- "position" (when present) is the pipeline's own hard budget: cash_usdc, equity_usdc, max_position_usdc, max_position_percent, current_position_usdc, open_positions, max_open_positions. recommended_position_percent is 0 unless action is BUY; otherwise a small share of portfolio value, typically 1 to 5. The pipeline's cap is applied after your answer; your number is a request, not a guarantee.

Confidence
- confidence is your own estimate, between 0 and 1, that the action you chose is the right call. Be honest: 1.0 would mean certainty, which no market call deserves; 0.6 to 0.85 is normal for a clear call. BUY needs confidence of at least 0.6 to proceed."""

_ACTIONS = {"BUY", "WATCH", "REJECT", "HOLD", "SELL"}
_JSON_KEYS = {"action", "confidence", "reasoning_summary", "positive_signals", "negative_signals", "risk_flags",
              "strategy_score", "recommended_position_percent"}


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


def _position_view(ctx: Mapping[str, Any] | None) -> dict:
    """Compact, numeric-only position context. Everything is optional; missing keys are simply omitted."""
    if not ctx:
        return {}
    out: dict = {}
    for k in ("cash_usdc", "equity_usdc", "max_position_usdc", "max_position_percent",
              "current_position_usdc", "open_positions", "max_open_positions"):
        v = ctx.get(k)
        if v is None:
            continue
        out[k] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    return out


def _outcomes_view(recent: Iterable[Mapping[str, Any]] | None) -> list[dict]:
    """Compact digest of the pipeline's recent decisions, for calibration. Numeric-only, capped at 20."""
    if not recent:
        return []
    out: list[dict] = []
    for o in recent:
        if not isinstance(o, Mapping):
            continue
        row: dict[str, Any] = {}
        act = o.get("action")
        if isinstance(act, str) and act.strip():
            row["action"] = act.strip().upper()[:16]
        for k in ("confidence", "strategy_score", "pnl_pct"):
            v = _as_float(o.get(k))
            if v is not None:
                row[k] = _r(v)
        st = o.get("status")
        if isinstance(st, str) and st.strip():
            row["status"] = st.strip()[:24]
        if row:
            out.append(row)
    return out[-20:]


def build_user_prompt(
    m: MarketState,
    s: StrategySignal,
    r: RiskAssessment,
    *,
    position_context: Mapping[str, Any] | None = None,
    recent_outcomes: Iterable[Mapping[str, Any]] | None = None,
) -> str:
    """Numeric features only. Token names/symbols/descriptions are attacker-controlled and are never included.

    ``position_context`` and ``recent_outcomes`` are optional. Omitting either yields the same prompt as a
    caller that never had them, so the cache key stays stable for callers that don't supply extra context.
    """
    cfg = s.config_snapshot or {}
    passed = [k for k, ok in s.gates.items() if ok]
    failed = [k for k, ok in s.gates.items() if not ok]
    age_h = None
    if m.token_created_at is not None:
        age_h = _r((m.timestamp - m.token_created_at).total_seconds() / 3600.0)
    windows = _window_view(m)
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
        "windows": windows,
        "windows_absent": [w for w in WINDOWS if w not in windows],
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
    pos = _position_view(position_context)
    if pos:
        digest["position"] = pos
    outcomes = _outcomes_view(recent_outcomes)
    if outcomes:
        digest["prior_outcomes"] = outcomes
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
    for k in ("strategy_score", "recommended_position_percent"):
        d[k] = _as_float(d.get(k), 0.0)
    if isinstance(d.get("reasoning_summary"), str):
        d["reasoning_summary"] = d["reasoning_summary"][:1500]
    elif "reasoning_summary" not in d:
        d["reasoning_summary"] = ""
    for k in ("positive_signals", "negative_signals", "risk_flags"):
        d[k] = _as_list(d.get(k))
    return d


def parse_decision(text: str) -> AIDecision:
    return AIDecision.model_validate(_normalise(_extract_object(text)))


def is_actionable(decision: AIDecision) -> bool:
    """True when the model chose BUY at or above the shared confidence floor.

    DecisionPipeline should gate new entries on this instead of re-implementing the threshold. Any downstream
    bias-to-action override must use the same value so the two layers cannot disagree.
    """
    action = getattr(decision.action, "value", decision.action)
    return str(action).upper() == "BUY" and decision.confidence >= BUY_MIN_CONFIDENCE


# ------------------------------------------------------------------ analyzer
_CACHE: dict[str, tuple[float, AIOutcome]] = {}
_CACHE_MAX = 500


class AIAnalyzer:
    def __init__(self, provider: LLMProvider | None, *, cache_ttl_s: float = 0.0):
        self.provider = provider
        self.cache_ttl_s = max(0.0, cache_ttl_s)

    async def analyze(
        self,
        m: MarketState,
        s: StrategySignal,
        r: RiskAssessment,
        *,
        position_context: Mapping[str, Any] | None = None,
        recent_outcomes: Iterable[Mapping[str, Any]] | None = None,
    ) -> AIOutcome:
        """Never raises: outages/garbage become UNAVAILABLE/INVALID and simply block new entries.

        ``position_context`` (pipeline's own budget) and ``recent_outcomes`` (this pipeline's recent track
        record) are optional, numeric-only hints. Passing either changes the prompt and therefore the cache
        key; omitting both is byte-for-byte identical to the previous behaviour for callers that don't have
        that data on hand.
        """
        if self.provider is None:
            return AIOutcome(status="DISABLED", prompt_version=PROMPT_VERSION)
        base = dict(provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION)
        user = build_user_prompt(
            m, s, r,
            position_context=position_context,
            recent_outcomes=recent_outcomes,
        )
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
        base = {**base, "provider": done.provider, "model": done.model}   # whoever in the chain actually answered
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