"""Single-writer lock per broker account (I9, EXE-8). OS-level advisory file lock."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO

from helm.core.errors import AccountLockError


class AccountLock:
    def __init__(self, lock_dir: Path | str, account_id: str) -> None:
        self.path = Path(lock_dir) / f"{account_id}.lock"
        self.account_id = account_id
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise AccountLockError(
                f"account {self.account_id!r} is already traded by another engine (pid {holder}); "
                "only one engine may trade an account (I9)"
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> AccountLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
