from enum import Enum


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Action(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    REJECT = "REJECT"
    HOLD = "HOLD"
    SELL = "SELL"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RiskCategory(str, Enum):
    CONTRACT_RISK = "CONTRACT_RISK"
    LIQUIDITY_RISK = "LIQUIDITY_RISK"
    HOLDER_RISK = "HOLDER_RISK"
    CREATOR_RISK = "CREATOR_RISK"
    MARKET_RISK = "MARKET_RISK"
    EXECUTION_RISK = "EXECUTION_RISK"
    MEV_RISK = "MEV_RISK"
    PORTFOLIO_RISK = "PORTFOLIO_RISK"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    VETO = "VETO"


class RiskDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class OrderStatus(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FAILED = "FAILED"            # definitively did not execute; safe to retry
    REJECTED = "REJECTED"        # blocked before submission by a safety check
    PENDING_SIGNATURE = "PENDING_SIGNATURE"  # waiting for the user's wallet
    SUBMITTED = "SUBMITTED"
    TIMEOUT = "TIMEOUT"          # outcome unknown; must be reconciled, never blindly retried


class ExitReason(str, Enum):
    HARD_STOP = "HARD_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    STAGNATION = "STAGNATION"
    MOMENTUM_DETERIORATION = "MOMENTUM_DETERIORATION"
    LIQUIDITY_DETERIORATION = "LIQUIDITY_DETERIORATION"
    RISK_ESCALATION = "RISK_ESCALATION"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"


class WalletCapability(str, Enum):
    AUTONOMOUS_DELEGATED = "AUTONOMOUS_DELEGATED"
    PER_TRADE_SIGNING = "PER_TRADE_SIGNING"
    PAPER_ONLY = "PAPER_ONLY"


class AIMode(str, Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
