"""Trading calendars (EXE-9, RISK-2). Pure functions of (calendar, ts)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_RTH = (time(9, 30), time(16, 0))


def is_open(calendar: str, ts_ns: int) -> bool:
    """True if the market described by `calendar` is open at ts_ns (UTC ns).

    Supported: "24x7" (crypto), "us_equity_rth" (Mon-Fri 09:30-16:00 New York;
    exchange holidays are not modelled yet and must come from venue metadata).
    Unknown calendars are closed (fail-closed).
    """
    if calendar == "24x7":
        return True
    if calendar == "us_equity_rth":
        local = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(_NY)
        return local.weekday() < 5 and _RTH[0] <= local.time() < _RTH[1]
    return False
