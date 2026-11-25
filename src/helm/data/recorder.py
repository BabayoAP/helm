"""Live recorder (DATA-4).

Spools live events (bars, quotes, trades, optional book deltas, feed status) to
Parquet as (seq, ts_recv, kind, symbol, payload-JSON). Storing the canonical
JSON keeps Decimal values exact, so `replay()` yields events that are equal,
field for field, to what was recorded and can be fed into any mode (TEST-4).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import polars as pl
from pydantic import BaseModel

from helm.core.events import decode_event, event_kind

RECORD_SCHEMA: dict[str, pl.DataType] = {
    "seq": pl.Int64(),
    "ts_recv": pl.Int64(),
    "kind": pl.String(),
    "symbol": pl.String(),
    "payload": pl.String(),
}


def _ts_recv(ev: BaseModel) -> int:
    for attr in ("ts_recv", "ts_close", "ts", "ts_event"):
        v = getattr(ev, attr, None)
        if isinstance(v, int):
            return v
    return 0


class Recorder:
    def __init__(self, root: Path | str, session: str, *, flush_every: int = 10_000) -> None:
        self.dir = Path(root) / "recorded" / session
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self._buf: list[tuple[int, int, str, str, str]] = []
        existing = sorted(self.dir.glob("part-*.parquet"))
        self._part = len(existing)
        self._seq = 0
        if existing:
            self._seq = int(pl.read_parquet(existing[-1], columns=["seq"])["seq"].max() or 0) + 1  # type: ignore[arg-type]

    def record(self, ev: BaseModel) -> None:
        sym = getattr(ev, "symbol", None)
        self._buf.append((self._seq, _ts_recv(ev), event_kind(ev), sym or "", ev.model_dump_json()))
        self._seq += 1
        if len(self._buf) >= self.flush_every:
            self.flush()

    async def on_event(self, ev: BaseModel) -> None:
        self.record(ev)

    def flush(self) -> None:
        if not self._buf:
            return
        df = pl.DataFrame(self._buf, schema=RECORD_SCHEMA, orient="row")
        path = self.dir / f"part-{self._part:06d}.parquet"
        tmp = path.with_suffix(".tmp")
        df.write_parquet(tmp, compression="zstd")
        os.replace(tmp, path)
        self._part += 1
        self._buf.clear()

    def close(self) -> None:
        self.flush()


def replay(session_dir: Path | str, kinds: tuple[str, ...] | None = None) -> Iterator[BaseModel]:
    parts = sorted(Path(session_dir).glob("part-*.parquet"))
    if not parts:
        return
    lf = pl.scan_parquet(parts).sort("seq")
    if kinds:
        lf = lf.filter(pl.col("kind").is_in(list(kinds)))
    for kind, payload in lf.select("kind", "payload").collect().iter_rows():
        yield decode_event(kind, payload)
