"""Kill-switch state machine (PRD §10, EXE-2, RISK-14).

RUNNING -> PAUSED -> HALTED. HALTED is latched: it is persisted (journal) and
survives restarts; leaving it requires `resume(code)` with the one-time code
issued when the halt happened (or re-issued to an authorized channel later).
Codes are kept only as SHA-256 hashes and are single-use.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from helm.core.enums import EngineState
from helm.core.events import EngineStateChanged

S = EngineState


def _h(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class ResumeDeniedError(Exception):
    pass


class KillSwitch:
    def __init__(
        self,
        code_fn: Callable[[], str],
        clock_fn: Callable[[], int],
        state: EngineState = S.RUNNING,
    ) -> None:
        self._code_fn = code_fn
        self._now = clock_fn
        self.state = state
        self._code_hash: str | None = None

    @classmethod
    def after_restart(
        cls, previous: EngineState | None, code_fn: Callable[[], str], clock_fn: Callable[[], int]
    ) -> KillSwitch:
        """Restart policy (decision 0014): HALTED stays HALTED; anything else starts PAUSED."""
        ks = cls(code_fn, clock_fn, S.HALTED if previous is S.HALTED else S.PAUSED)
        return ks

    def _move(self, new: EngineState, reason: str) -> EngineStateChanged | None:
        if new is self.state:
            return None
        ev = EngineStateChanged(previous=self.state, current=new, reason=reason, ts=self._now())
        self.state = new
        return ev

    @property
    def can_open_risk(self) -> bool:
        return self.state is S.RUNNING

    def pause(self, reason: str) -> EngineStateChanged | None:
        if self.state is S.HALTED:
            return None  # pausing never un-latches a halt
        return self._move(S.PAUSED, reason)

    def halt(self, reason: str) -> tuple[EngineStateChanged | None, str]:
        """Latch HALTED and return (event, fresh one-time resume code)."""
        ev = self._move(S.HALTED, reason)
        return ev, self.issue_code()

    def issue_code(self) -> str:
        """(Re-)issue the one-time resume code; any previous code is invalidated."""
        code = self._code_fn()
        self._code_hash = _h(code)
        return code

    def resume(self, code: str | None = None, reason: str = "resume") -> EngineStateChanged | None:
        if self.state is S.HALTED:
            if (
                code is None
                or self._code_hash is None
                or not hmac.compare_digest(_h(code), self._code_hash)
            ):
                raise ResumeDeniedError("HALTED: resume requires the valid one-time code")
            self._code_hash = None  # single use
        return self._move(S.RUNNING, reason)
