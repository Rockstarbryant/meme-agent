class AgentError(Exception):
    """Base class for all application errors."""


class IntegrationNotVerified(AgentError):
    """An external integration has not been verified against official docs, so it is disabled."""

    def __init__(self, what: str, requirement: str = ""):
        self.what = what
        self.requirement = requirement
        super().__init__(f"{what} is not verified/enabled. {requirement}".strip())


class DataUnavailable(AgentError):
    """A data source is down or unsupported. Never substitute fabricated data."""


class LiveSafetyError(AgentError):
    def __init__(self, failed_checks: list[str]):
        self.failed_checks = failed_checks
        super().__init__("Live safety checks failed: " + "; ".join(failed_checks))


class ModeMismatchError(AgentError):
    """Executor does not match the trading mode (e.g. paper executor in LIVE)."""


class ApprovalError(AgentError):
    """Trade was not approved by the deterministic risk layer, or approval is forged/invalid."""


class PolicyViolationError(AgentError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("Wallet policy violation: " + "; ".join(violations))


class PaperWalletCannotSubmit(AgentError):
    """Paper wallets never submit blockchain transactions."""
