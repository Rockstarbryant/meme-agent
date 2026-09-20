"""Proof-carrying approvals: executors only act on trades the risk layer signed."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from app.core.errors import ApprovalError
from app.core.types import RiskDecision
from app.domain.trade import ApprovedTrade, TradeRequest


class TradeApprover:
    def __init__(self, secret: str):
        if len(secret) < 16:
            raise ValueError("approval secret too short")
        self._key = secret.encode()

    def _sig(self, req: TradeRequest, assessment_id: str) -> str:
        msg = (req.model_dump_json() + "|" + assessment_id).encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def approve(self, req: TradeRequest, assessment) -> ApprovedTrade:
        if assessment.decision != RiskDecision.APPROVE:
            raise ApprovalError(f"risk assessment rejected the trade: {assessment.summary()}")
        return ApprovedTrade(request=req, assessment_id=assessment.id, decision=RiskDecision.APPROVE,
                             signature=self._sig(req, assessment.id), approved_at=datetime.now(timezone.utc))

    def approve_exit(self, req: TradeRequest) -> ApprovedTrade:
        """Deterministic exits (stop-loss, take-profit...) are approved by code, never by the LLM."""
        return ApprovedTrade(request=req, assessment_id="EXIT", decision=RiskDecision.APPROVE,
                             signature=self._sig(req, "EXIT"), approved_at=datetime.now(timezone.utc))

    def verify(self, approved: ApprovedTrade) -> bool:
        if approved.decision != RiskDecision.APPROVE:
            return False
        return hmac.compare_digest(approved.signature, self._sig(approved.request, approved.assessment_id))
