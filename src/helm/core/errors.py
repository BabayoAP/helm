"""Exception hierarchy."""

from __future__ import annotations


class HelmError(Exception):
    """Base class for all Helm errors."""


class ConfigError(HelmError):
    pass


class QuantizationError(HelmError):
    pass


class IllegalTransitionError(HelmError):
    """An order or engine state machine received an illegal transition (PRD §6.5)."""


class JournalError(HelmError):
    pass


class LiveGateError(HelmError):
    """Raised when a live broker is requested without all EXE-1 gates open."""


class AccountLockError(HelmError):
    """Single-writer lock per broker account is held by another engine (I9, EXE-8)."""


class BrokerError(HelmError):
    pass


class RateLimitedError(BrokerError):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AmbiguousSubmitError(BrokerError):
    """Submit outcome unknown (timeout / ambiguous response). Order goes to UNKNOWN."""


class DisconnectedError(BrokerError):
    pass


class LookaheadError(HelmError):
    pass
