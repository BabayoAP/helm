"""Append-only journal (I4, SEC-7, PRD §4 durable-state decision).

SQLite in WAL mode is the source of truth; a JSONL file mirrors every entry for
human inspection and offline recovery. UPDATE/DELETE on the journal table are
blocked by triggers. Entries are written (and fsynced, `synchronous=FULL`)
*before* the side effect they describe.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from helm.core.errors import JournalError
from helm.core.events import decode_event, event_kind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    run_id     TEXT    NOT NULL,
    account_id TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    key        TEXT    NOT NULL DEFAULT '',
    payload    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_kind ON journal(kind);
CREATE INDEX IF NOT EXISTS journal_key ON journal(key);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    ts: int
    run_id: str
    account_id: str
    kind: str
    key: str
    payload: str

    def decode(self) -> BaseModel:
        return decode_event(self.kind, self.payload)


class Journal:
    def __init__(self, path: Path | str, *, mirror: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._mirror: IO[str] | None = None
        if mirror:
            self._mirror = self.path.with_suffix(".jsonl").open("a", encoding="utf-8")

    def append(
        self,
        event: BaseModel,
        *,
        ts: int,
        run_id: str,
        account_id: str,
        key: str = "",
    ) -> int:
        kind = event_kind(event)
        payload = event.model_dump_json()
        try:
            cur = self._db.execute(
                "INSERT INTO journal (ts, run_id, account_id, kind, key, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, run_id, account_id, kind, key, payload),
            )
        except sqlite3.Error as exc:  # pragma: no cover - disk errors
            raise JournalError(str(exc)) from exc
        seq = cur.lastrowid
        if seq is None:  # pragma: no cover
            raise JournalError("insert returned no rowid")
        if self._mirror is not None:
            line = {
                "seq": seq,
                "ts": ts,
                "run_id": run_id,
                "account_id": account_id,
                "kind": kind,
                "key": key,
                "payload": json.loads(payload),
            }
            self._mirror.write(json.dumps(line, separators=(",", ":")) + "\n")
            self._mirror.flush()
            os.fsync(self._mirror.fileno())
        return seq

    def entries(
        self,
        *,
        kinds: tuple[str, ...] | None = None,
        account_id: str | None = None,
        after_seq: int = 0,
        key: str | None = None,
    ) -> Iterator[JournalEntry]:
        sql = "SELECT seq, ts, run_id, account_id, kind, key, payload FROM journal WHERE seq > ?"
        args: list[object] = [after_seq]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if account_id is not None:
            sql += " AND account_id = ?"
            args.append(account_id)
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        sql += " ORDER BY seq"
        for row in self._db.execute(sql, args):
            yield JournalEntry(*row)

    def replay(self, **kwargs: object) -> Iterator[BaseModel]:
        for entry in self.entries(**kwargs):  # type: ignore[arg-type]
            yield entry.decode()

    def last_seq(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()
        return int(row[0])

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    def close(self) -> None:
        if self._mirror is not None:
            self._mirror.close()
            self._mirror = None
        self._db.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
