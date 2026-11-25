"""Typed, versioned event types (PRD §6.2).

Market data events reuse the §8 models directly. This module adds the
engine-internal events and a registry so the journal can round-trip any event
by its `kind` name.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from helm.core.enums import (
    ControlAction,
    EngineState,
    FeedState,
    Mode,
    OrderEventKind,
    OrderState,
    Severity,
)
from helm.core.models import (
    Balance,
    Bar,
    BookDelta,
    BookSnapshot,
    Fill,
    Model,
    Order,
    OrderIntent,
    Position,
    Quote,
    RiskDecision,
    Trade,
    WalletEvent,
)

SCHEMA_VERSION = 1


class OrderEvent(Model):
    kind: OrderEventKind
    client_order_id: str
    state: OrderState
    order: Order
    reason: str = ""
    ts: int


class PositionSnapshot(Model):
    account_id: str
    positions: tuple[Position, ...]
    ts: int


class AccountSnapshot(Model):
    account_id: str
    balances: tuple[Balance, ...]
    equity: Decimal
    ts: int


class FeedStatus(Model):
    venue: str
    symbol: str | None
    state: FeedState
    reason: str = ""
    ts: int


class ControlCommand(Model):
    action: ControlAction
    source: str  # "telegram:<user_id>", "ui", "cli", "supervisor"
    target: str | None = None  # bot/strategy id; None = engine-wide
    code: str | None = None  # one-time resume code
    ts: int


class EngineStateChanged(Model):
    previous: EngineState
    current: EngineState
    reason: str
    ts: int


class Alert(Model):
    severity: Severity
    key: str  # dedupe key
    message: str
    mode: Mode
    ts: int

    @property
    def text(self) -> str:
        return f"{self.mode.badge} {self.severity.value}: {self.message}"


class NewsEvent(Model):
    source: str
    headline: str
    url: str | None = None
    ts_event: int


class ConfigChanged(Model):
    """SEC-7: config changes are journaled, including refused ones."""

    section: str
    before: str
    after: str
    source: str
    accepted: bool
    ts: int


class IntentEvent(Model):
    """An intent emitted by a strategy, journaled before risk sees it (I4)."""

    intent: OrderIntent
    ts: int


EVENT_TYPES: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        Bar,
        Quote,
        Trade,
        BookDelta,
        BookSnapshot,
        WalletEvent,
        NewsEvent,
        IntentEvent,
        ConfigChanged,
        OrderIntent,
        RiskDecision,
        OrderEvent,
        Fill,
        PositionSnapshot,
        AccountSnapshot,
        FeedStatus,
        ControlCommand,
        EngineStateChanged,
        Alert,
    )
}


def event_kind(event: BaseModel) -> str:
    name = type(event).__name__
    if name not in EVENT_TYPES:
        raise KeyError(f"unregistered event type {name}")
    return name


def decode_event(kind: str, payload_json: str) -> BaseModel:
    return EVENT_TYPES[kind].model_validate_json(payload_json)
