"""Wall clock for paper/live engines. Strategies never see this; they use ctx.now()."""

from __future__ import annotations

import time


class LiveClock:
    def now(self) -> int:
        return time.time_ns()
