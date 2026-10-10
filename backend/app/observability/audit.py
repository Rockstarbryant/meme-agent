"""Operational audit trail: WHAT happened, WHERE, WHEN and WHICH provider/model was involved.

Separate from ``audit_logs`` (user/operator *actions*) and from the ``events`` table (trading events). This is the
trail an operator reads when a token was rejected, a provider went dark, or an AI answer looks odd:

  * which market-data provider failed / was skipped (circuit open) / answered, for which token, how long it took;
  * which enrichment source produced holder / verification data;
  * which AI provider+model answered a decision (and which ones failed before it);
  * each agent tool call and each venue quote;
  * why a live order was rejected (the exact safety-check codes).

Design rules
  * Recording is synchronous, in-memory and NEVER raises or blocks the trading path. Sinks (DB / event outbox) are
    drained asynchronously by whoever owns a connection (control plane, cloud worker, local runner).
  * Failures are always recorded. Successes are sampled (one per provider per ``ok_sample_s``) plus every
    recovery transition, so a healthy system does not flood the table, while AI answers (decision provenance) are
    always recorded.
  * Identical consecutive failures are collapsed with a counter (``repeat``) instead of N rows.
  * Context (user, token, decision) is carried in a ContextVar so deep provider code does not need extra arguments.
  * Secrets never go in: ``detail`` is scrubbed of anything that looks like a key/token/authorization value.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Iterator

from pydantic import BaseModel, Field

log = logging.getLogger("audit")


class AuditKind(str, Enum):
    MARKET_PROVIDER = "MARKET_PROVIDER"
    ENRICHMENT_PROVIDER = "ENRICHMENT_PROVIDER"
    AI_PROVIDER = "AI_PROVIDER"
    AI_AGENT = "AI_AGENT"            # one tool call / final answer of the agentic analyst
    VENUE_QUOTE = "VENUE_QUOTE"
    EXECUTION = "EXECUTION"          # live pre-flight / submit / fill problems
    CHART_PROVIDER = "CHART_PROVIDER"
    SYSTEM = "SYSTEM"


class AuditStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"              # circuit open / not configured / not needed
    DEGRADED = "DEGRADED"            # answered, but partially or via a fallback
    RECOVERED = "RECOVERED"          # first success after failures


class AuditSeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class AuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: AuditKind
    component: str = ""              # e.g. "market_registry", "enrichment.holders", "ai.fallback", "execution.live"
    provider: str = ""               # codex | goldrush | goldsky | geckoterminal | cerebras | uniswap | kyberswap ...
    model: str = ""                  # AI model id when relevant
    operation: str = ""              # get_market_state | discover_tokens | holders | complete | quote ...
    status: AuditStatus
    severity: AuditSeverity = AuditSeverity.INFO
    latency_ms: float | None = None
    error: str = ""
    token_key: str = ""
    user_id: str = ""
    decision_id: str = ""
    repeat: int = 1                  # 1 + identical failures suppressed since the previous row
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------------------------- context
_CTX: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("audit_ctx", default={})


@contextlib.contextmanager
def audit_scope(**fields: str | None) -> Iterator[None]:
    """``with audit_scope(user_id=..., token_key=..., decision_id=...):`` -> every record made inside carries them."""
    cur = dict(_CTX.get())
    cur.update({k: str(v) for k, v in fields.items() if v})
    tok = _CTX.set(cur)
    try:
        yield
    finally:
        _CTX.reset(tok)


def current_scope() -> dict[str, str]:
    return dict(_CTX.get())


# ---------------------------------------------------------------------------------------------- scrubbing
_SECRET_KEY = re.compile(r"(key|token|secret|authorization|password|signature|bearer)", re.I)
_SECRET_VAL = re.compile(r"(?i)(bearer\s+[a-z0-9._\-]{8,}|[a-z0-9_\-]{24,}\.[a-z0-9_\-]{6,}\.[a-z0-9_\-]{6,}|gs_edge_[a-z0-9]+|"
                         r"(api[_-]?key|key)=[^&\s]+)")


def scrub_text(s: str, limit: int = 400) -> str:
    return _SECRET_VAL.sub("[redacted]", str(s))[:limit]


def scrub(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:40]:
            out[str(k)[:60]] = "[redacted]" if _SECRET_KEY.search(str(k)) else scrub(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v, depth + 1) for v in list(obj)[:40]]
    if isinstance(obj, str):
        return scrub_text(obj, 600)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return scrub_text(repr(obj), 200)


# ---------------------------------------------------------------------------------------------- recorder
Sink = Callable[[list[AuditRecord]], Awaitable[None]]


class AuditRecorder:
    def __init__(self, max_buffer: int = 5000, ok_sample_s: float = 60.0, repeat_window_s: float = 30.0):
        self._buf: deque[AuditRecord] = deque(maxlen=max_buffer)
        self.dropped = 0
        self.ok_sample_s = ok_sample_s
        self.repeat_window_s = repeat_window_s
        self._last_ok: dict[tuple[str, str, str], float] = {}
        self._last_fail: dict[tuple[str, str, str], dict] = {}
        self._failing: set[tuple[str, str, str]] = set()
        self._sinks: list[Sink] = []
        self.enabled = True

    # -------------------------------------------------------------------- sinks
    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def clear_sinks(self) -> None:
        self._sinks.clear()

    def pending(self) -> int:
        return len(self._buf)

    def drain(self, n: int = 500) -> list[AuditRecord]:
        out: list[AuditRecord] = []
        while self._buf and len(out) < n:
            out.append(self._buf.popleft())
        return out

    def reset(self) -> None:
        """Forget buffered records and failure/sampling state (tests)."""
        self._buf.clear()
        self._last_ok.clear()
        self._last_fail.clear()
        self._failing.clear()
        self.dropped = 0

    def has_sinks(self) -> bool:
        return bool(self._sinks)

    def drain_where(self, pred: Callable[[AuditRecord], bool], n: int = 500) -> list[AuditRecord]:
        """Remove and return up to ``n`` buffered records matching ``pred`` (others stay for their own owner)."""
        keep: deque[AuditRecord] = deque(maxlen=self._buf.maxlen)
        out: list[AuditRecord] = []
        for r in self._buf:
            (out if len(out) < n and pred(r) else keep).append(r)
        self._buf = keep
        return out

    async def flush(self, n: int = 500) -> int:
        """Deliver buffered records to every sink. A failing sink puts the records back (bounded) and never raises."""
        if not self._sinks:
            return 0
        batch = self.drain(n)
        if not batch:
            return 0
        ok_any = False
        for sink in list(self._sinks):
            try:
                await sink(batch)
                ok_any = True
            except Exception:  # noqa: BLE001
                log.warning("audit sink failed", exc_info=True)
        if not ok_any:
            for r in reversed(batch):          # keep them for the next flush
                if len(self._buf) < (self._buf.maxlen or 0):
                    self._buf.appendleft(r)
                else:
                    self.dropped += 1
            return 0
        return len(batch)

    # -------------------------------------------------------------------- recording
    def _push(self, rec: AuditRecord) -> None:
        if len(self._buf) == self._buf.maxlen:
            self.dropped += 1
        self._buf.append(rec)

    def record(self, rec: AuditRecord) -> AuditRecord | None:
        """Never raises. Returns the stored record (or None when it was sampled out / collapsed)."""
        if not self.enabled:
            return None
        try:
            now = time.monotonic()
            for k, v in current_scope().items():
                if k in ("user_id", "token_key", "decision_id") and not getattr(rec, k):
                    setattr(rec, k, v)
            key = (rec.kind.value, rec.provider, rec.operation)
            if rec.status in (AuditStatus.OK, AuditStatus.RECOVERED):
                recovered = key in self._failing
                self._failing.discard(key)
                gone = self._last_fail.pop(key, None)
                if recovered:
                    rec.status = AuditStatus.RECOVERED
                    rec.severity = AuditSeverity.INFO
                    if gone and gone["suppressed"]:
                        rec.detail = {**rec.detail, "suppressed_identical_failures": gone["suppressed"]}
                elif rec.kind not in (AuditKind.AI_PROVIDER, AuditKind.AI_AGENT, AuditKind.VENUE_QUOTE, AuditKind.EXECUTION):
                    last = self._last_ok.get(key, 0.0)
                    if now - last < self.ok_sample_s:
                        return None
                self._last_ok[key] = now
                self._push(rec)
                return rec
            # failures / skips / degraded
            self._failing.add(key)
            prev = self._last_fail.get(key)
            same = (prev is not None and now - prev["t"] < self.repeat_window_s and prev["err"] == rec.error[:60]
                    and prev["token"] == rec.token_key and prev["status"] == rec.status)
            if same:
                prev["suppressed"] += 1      # counted, and reported on the next row that is let through
                return None
            suppressed = prev["suppressed"] if prev is not None else 0
            if suppressed:
                rec.repeat = 1 + suppressed
                rec.detail = {**rec.detail, "suppressed_identical_failures": suppressed}
            if rec.severity == AuditSeverity.INFO:
                rec.severity = AuditSeverity.ERROR if rec.status == AuditStatus.FAILED else AuditSeverity.WARN
            self._last_fail[key] = {"t": now, "err": rec.error[:60], "token": rec.token_key, "status": rec.status,
                                    "suppressed": 0}
            self._push(rec)
            return rec
        except Exception:  # noqa: BLE001
            log.debug("audit record failed", exc_info=True)
            return None


RECORDER = AuditRecorder()


def record_event(kind: AuditKind, status: AuditStatus, *, provider: str = "", operation: str = "", component: str = "",
                 model: str = "", latency_ms: float | None = None, error: str = "", token_key: str = "",
                 decision_id: str = "", user_id: str = "", severity: AuditSeverity | None = None,
                 detail: dict | None = None, recorder: AuditRecorder | None = None) -> AuditRecord | None:
    rec = AuditRecord(kind=kind, status=status, provider=provider, operation=operation, component=component,
                      model=model, latency_ms=None if latency_ms is None else round(latency_ms, 1),
                      error=scrub_text(error), token_key=token_key, decision_id=decision_id, user_id=user_id,
                      severity=severity or AuditSeverity.INFO, detail=scrub(detail or {}))
    return (recorder or RECORDER).record(rec)
