"""Order state machine (PRD §6.5).

NEW -> PENDING_SUBMIT -> ACCEPTED -> PARTIALLY_FILLED -> FILLED, with terminal
alternates CANCELED / REJECTED / EXPIRED and the special UNKNOWN state for
ambiguous submits (resolved by REST lookup, never by blind retry).

Two kinds of non-progress events are distinguished:
- *stale*: a duplicate or reordered event describing an earlier stage than the
  one we already know (e.g. ACCEPTED after FILLED). Counted, logged, not applied.
- *illegal*: a contradiction (e.g. FILLED -> CANCELED). Raises; callers alert.
"""

from __future__ import annotations

from helm.core.enums import OrderState
from helm.core.errors import IllegalTransitionError

S = OrderState

_TERMINAL = frozenset({S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED})
_AFTER_SUBMIT = frozenset({S.ACCEPTED, S.PARTIALLY_FILLED}) | _TERMINAL

LEGAL: dict[OrderState, frozenset[OrderState]] = {
    S.NEW: frozenset({S.PENDING_SUBMIT, S.REJECTED}),
    S.PENDING_SUBMIT: _AFTER_SUBMIT | {S.UNKNOWN},
    S.UNKNOWN: _AFTER_SUBMIT | {S.PENDING_SUBMIT},
    S.ACCEPTED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED}),
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
}

_RANK: dict[OrderState, int] = {
    S.NEW: 0,
    S.PENDING_SUBMIT: 1,
    S.UNKNOWN: 1,
    S.ACCEPTED: 2,
    S.PARTIALLY_FILLED: 3,
    S.FILLED: 4,
    S.CANCELED: 4,
    S.REJECTED: 4,
    S.EXPIRED: 4,
}


def is_stale(current: OrderState, incoming: OrderState) -> bool:
    """Incoming event describes a stage at or before the current one (dup / reorder)."""
    if incoming is S.PARTIALLY_FILLED and current is S.PARTIALLY_FILLED:
        return False
    if current in _TERMINAL and incoming in _TERMINAL:
        return incoming is current
    if {current, incoming} == {S.UNKNOWN, S.PENDING_SUBMIT}:
        return False
    return _RANK[incoming] <= _RANK[current]


def transition(current: OrderState, new: OrderState) -> OrderState:
    if new not in LEGAL[current]:
        raise IllegalTransitionError(f"illegal order transition {current} -> {new}")
    return new
