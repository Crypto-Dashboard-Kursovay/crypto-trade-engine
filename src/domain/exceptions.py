class DomainError(Exception):
    """Base for all domain-level errors."""


class InvalidSignalError(DomainError):
    """Signal violates structural invariants and cannot be processed."""


class RiskRejectedError(DomainError):
    """RiskManager refused to approve a signal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InsufficientBalanceError(RiskRejectedError):
    """Available quote balance is below the required notional."""


class LotSizeError(RiskRejectedError):
    """Order size violates exchange lot/min-notional constraints."""


class OrderExecutionError(DomainError):
    """Exchange adapter failed to place or confirm an order."""
