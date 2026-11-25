"""Enumerations shared across the platform (PRD §6.2, §6.5, §8, §10, §13)."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"

    @property
    def badge(self) -> str:
        """Mode badge for UI/logs/Telegram (I8)."""
        return f"[{self.value.upper()}]"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"
    ONCHAIN = "onchain"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LMT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    DAY = "DAY"
    POST_ONLY = "POST_ONLY"


class OrderState(StrEnum):
    NEW = "NEW"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_open(self) -> bool:
        return self in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED)


_TERMINAL = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)


class OrderEventKind(StrEnum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class EngineState(StrEnum):
    """Kill-switch states (PRD §10)."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"


class FeedState(StrEnum):
    LIVE = "LIVE"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class LifecycleStage(StrEnum):
    """Strategy promotion lifecycle (PRD §13). Order matters."""

    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    LIVE_SMALL = "LIVE_SMALL"
    LIVE = "LIVE"

    @property
    def rank(self) -> int:
        return _STAGE_ORDER.index(self)

    def at_least(self, other: LifecycleStage) -> bool:
        return self.rank >= other.rank


_STAGE_ORDER: list[LifecycleStage] = [
    LifecycleStage.DRAFT,
    LifecycleStage.BACKTESTED,
    LifecycleStage.VALIDATED,
    LifecycleStage.PAPER,
    LifecycleStage.LIVE_SMALL,
    LifecycleStage.LIVE,
]


class ControlAction(StrEnum):
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    FLATTEN = "flatten"
    KILL = "kill"
