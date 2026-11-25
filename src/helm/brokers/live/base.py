"""Base for live broker adapters. Constructing one is only possible through the
EXE-1 triple gate (helm.app.gates) and never for a paper account (PAP-2)."""

from __future__ import annotations

from helm.core.config import PAPER_ACCOUNT_PREFIX
from helm.core.errors import LiveGateError


class LiveBrokerBase:
    is_live = True

    def __init__(self, *, account_id: str, venue: str, _gate_token: object) -> None:
        from helm.app.gates import GATE_TOKEN  # noqa: PLC0415 - avoid import cycle

        if _gate_token is not GATE_TOKEN:
            raise LiveGateError(
                "live brokers can only be built by helm.app.gates.build_broker (EXE-1)"
            )
        if account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise LiveGateError(
                f"paper account {account_id!r} can never reach a live broker (PAP-2)"
            )
        self.account_id = account_id
        self.venue = venue
