"""Experiment tracking (RES-2) and the locked-holdout access log (RES-4).

Every run records: git hash, strategy code hash, data hash, spec hash, params,
seed, trial count, metrics and the result hash. Rerunning a record on the same
data must reproduce the result hash (I7). Rows are append-only (triggers), like
the journal: research history is evidence for promotion (§13).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helm.core.errors import HelmError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,           -- backtest | validation | holdout | optimize
    strategy    TEXT NOT NULL,           -- module:Class
    code_hash   TEXT NOT NULL,
    params      TEXT NOT NULL,           -- canonical JSON
    params_hash TEXT NOT NULL,
    git_hash    TEXT NOT NULL,
    data_hash   TEXT NOT NULL,
    data_desc   TEXT NOT NULL,
    spec_hash   TEXT NOT NULL,
    seed        INTEGER NOT NULL,
    n_trials    INTEGER NOT NULL,
    metrics     TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    passed      INTEGER,                 -- validation verdict (NULL for plain runs)
    report      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS runs_code ON runs(strategy, code_hash, params_hash);
CREATE TABLE IF NOT EXISTS holdout_access (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    strategy   TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    data_hash  TEXT NOT NULL,
    who        TEXT NOT NULL,
    reason     TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'experiment log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'experiment log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS holdout_no_update BEFORE UPDATE ON holdout_access
BEGIN SELECT RAISE(ABORT, 'holdout log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS holdout_no_delete BEFORE DELETE ON holdout_access
BEGIN SELECT RAISE(ABORT, 'holdout log is append-only'); END;
"""


class HoldoutLockedError(HelmError):
    """The locked holdout was already used for this strategy version (RES-4)."""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def params_hash(params: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(params)).encode()).hexdigest()[:16]


def strategy_path(strategy: object) -> str:
    cls = type(strategy)
    return f"{cls.__module__}:{cls.__name__}"


def code_hash(strategy: object) -> str:
    """SHA-256 over every .py file in the strategy's package directory (conservative:
    editing a shared helper next to the strategy also counts as a code change, §13)."""
    src = inspect.getsourcefile(type(strategy))
    if src is None:
        raise HelmError(f"cannot locate source for {strategy_path(strategy)}")
    h = hashlib.sha256()
    for f in sorted(Path(src).parent.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def git_hash(cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out + ("-dirty" if dirty else "")


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    ts: int
    kind: str
    strategy: str
    code_hash: str
    params: dict[str, Any]
    params_hash: str
    git_hash: str
    data_hash: str
    data_desc: str
    spec_hash: str
    seed: int
    n_trials: int
    metrics: dict[str, Any]
    result_hash: str
    passed: bool | None
    report: dict[str, Any]


class ExperimentStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        *,
        run_id: str,
        kind: str,
        strategy: object,
        params: Mapping[str, Any],
        data_hash: str,
        data_desc: str,
        spec_hash: str,
        seed: int,
        n_trials: int,
        metrics: Mapping[str, Any],
        result_hash: str,
        passed: bool | None = None,
        report: Mapping[str, Any] | None = None,
        git: str | None = None,
    ) -> RunRecord:
        rec = RunRecord(
            run_id=run_id,
            ts=time.time_ns(),
            kind=kind,
            strategy=strategy_path(strategy),
            code_hash=code_hash(strategy),
            params=dict(params),
            params_hash=params_hash(params),
            git_hash=git if git is not None else git_hash(),
            data_hash=data_hash,
            data_desc=data_desc,
            spec_hash=spec_hash,
            seed=seed,
            n_trials=n_trials,
            metrics=dict(metrics),
            result_hash=result_hash,
            passed=passed,
            report=dict(report or {}),
        )
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.run_id,
                    rec.ts,
                    rec.kind,
                    rec.strategy,
                    rec.code_hash,
                    canonical(rec.params),
                    rec.params_hash,
                    rec.git_hash,
                    rec.data_hash,
                    rec.data_desc,
                    rec.spec_hash,
                    rec.seed,
                    rec.n_trials,
                    canonical(rec.metrics),
                    rec.result_hash,
                    None if passed is None else int(passed),
                    canonical(rec.report),
                ),
            )
        return rec

    @staticmethod
    def _row(r: tuple[Any, ...]) -> RunRecord:
        return RunRecord(
            run_id=r[0],
            ts=r[1],
            kind=r[2],
            strategy=r[3],
            code_hash=r[4],
            params=json.loads(r[5]),
            params_hash=r[6],
            git_hash=r[7],
            data_hash=r[8],
            data_desc=r[9],
            spec_hash=r[10],
            seed=r[11],
            n_trials=r[12],
            metrics=json.loads(r[13]),
            result_hash=r[14],
            passed=None if r[15] is None else bool(r[15]),
            report=json.loads(r[16]),
        )

    def get(self, run_id: str) -> RunRecord | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else self._row(row)

    def runs(
        self,
        *,
        strategy: str | None = None,
        code: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[RunRecord]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        for col, val in (("strategy", strategy), ("code_hash", code), ("kind", kind)):
            if val is not None:
                q += f" AND {col} = ?"
                args.append(val)
        q += " ORDER BY ts DESC LIMIT ?"
        return [self._row(r) for r in self._db.execute(q, (*args, limit)).fetchall()]

    def trial_count(self, strategy: str, code: str) -> int:
        """All trials ever run for this strategy version: feeds the deflated Sharpe."""
        row = self._db.execute(
            "SELECT COALESCE(SUM(n_trials), 0) FROM runs WHERE strategy = ? AND code_hash = ?"
            " AND kind IN ('backtest', 'optimize')",
            (strategy, code),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------ holdout
    def holdout_accesses(self, strategy: str, code: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts, data_hash, who, reason FROM holdout_access"
            " WHERE strategy = ? AND code_hash = ? ORDER BY seq",
            (strategy, code),
        ).fetchall()
        return [{"ts": r[0], "data_hash": r[1], "who": r[2], "reason": r[3]} for r in rows]

    def open_holdout(
        self, strategy: object, data_hash: str, *, who: str, reason: str, force: bool = False
    ) -> None:
        """Log an access to the locked holdout. Once per strategy version (code hash);
        a second access needs `force` and is itself logged with its reason."""
        path, code = strategy_path(strategy), code_hash(strategy)
        prior = self.holdout_accesses(path, code)
        if prior and not force:
            raise HoldoutLockedError(
                f"holdout already opened for {path} @ {code} on "
                f"{time.strftime('%Y-%m-%d', time.gmtime(prior[0]['ts'] / 1e9))}; "
                "a re-look needs --force and a reason, and weakens the evidence"
            )
        with self._db:
            self._db.execute(
                "INSERT INTO holdout_access (ts, strategy, code_hash, data_hash, who, reason)"
                " VALUES (?,?,?,?,?,?)",
                (time.time_ns(), path, code, data_hash, who, reason or "first access"),
            )
