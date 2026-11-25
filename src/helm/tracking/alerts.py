"""Alert routing with severities, dedupe and throttling (OPS-2, TRK-2).

CRITICAL alerts are always delivered, but repeats of the same key inside the
throttle window are coalesced into a single "repeated N times" follow-up so a
storm cannot flood the channel. INFO/WARN repeats inside the window are dropped
(and counted).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

from helm.core.enums import Mode, Severity
from helm.core.events import Alert

Sink = Callable[[Alert], None]
NS_PER_S = 1_000_000_000


@dataclass
class _KeyState:
    last_sent_ns: int
    suppressed: int = 0


@dataclass
class AlertManager:
    mode: Mode
    clock_fn: Callable[[], int]
    sinks: list[Sink] = field(default_factory=lambda: [])
    window_s: dict[Severity, float] = field(
        default_factory=lambda: {Severity.INFO: 300.0, Severity.WARN: 60.0, Severity.CRITICAL: 30.0}
    )
    sent: list[Alert] = field(default_factory=lambda: [])
    suppressed_total: int = 0

    def __post_init__(self) -> None:
        self._keys: dict[str, _KeyState] = {}

    def _deliver(self, alert: Alert) -> None:
        self.sent.append(alert)
        for sink in self.sinks:
            with contextlib.suppress(Exception):  # a broken sink must never break trading
                sink(alert)

    def emit(self, severity: Severity, key: str, message: str) -> bool:
        """Returns True if delivered now."""
        now = self.clock_fn()
        st = self._keys.get(key)
        window_ns = int(self.window_s[severity] * NS_PER_S)
        if st is not None and now - st.last_sent_ns < window_ns:
            st.suppressed += 1
            self.suppressed_total += 1
            return False
        text = message
        if st is not None and st.suppressed:
            text = f"{message} (repeated {st.suppressed}x since last alert)"
        self._keys[key] = _KeyState(last_sent_ns=now)
        self._deliver(Alert(severity=severity, key=key, message=text, mode=self.mode, ts=now))
        return True

    def info(self, key: str, message: str) -> bool:
        return self.emit(Severity.INFO, key, message)

    def warn(self, key: str, message: str) -> bool:
        return self.emit(Severity.WARN, key, message)

    def critical(self, key: str, message: str) -> bool:
        return self.emit(Severity.CRITICAL, key, message)

    def flush_suppressed(self) -> None:
        """Deliver a summary for keys with suppressed CRITICAL repeats (called by a timer)."""
        now = self.clock_fn()
        for key, st in self._keys.items():
            if st.suppressed:
                self._deliver(
                    Alert(
                        severity=Severity.CRITICAL,
                        key=key,
                        mode=self.mode,
                        ts=now,
                        message=f"{key}: {st.suppressed} more occurrence(s) suppressed",
                    )
                )
                st.suppressed = 0
                st.last_sent_ns = now
