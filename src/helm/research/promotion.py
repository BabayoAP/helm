"""Strategy promotion lifecycle (PRD §13): DRAFT → BACKTESTED → VALIDATED → PAPER →
LIVE_SMALL → LIVE.

- A strategy *version* is (strategy path, code hash, params hash). Any code or
  parameter change is a new version and starts at DRAFT (the conservative reading
  of "resets to DRAFT/BACKTESTED as appropriate").
- Promotion is one step at a time, needs its gate to pass, and is a manual,
  journaled approval (who, when, evidence). Demotion is always allowed.
- The live gate (EXE-1 c) reads the stage from here, not from what a config
  file claims (decision 0030).

Storage: an append-only table next to the experiment log.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from helm.core.config import PromotionConfig
from helm.core.enums import LifecycleStage
from helm.core.errors import HelmError
from helm.research.experiments.store import ExperimentStore, RunRecord

S = LifecycleStage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promotions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    strategy    TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    from_stage  TEXT NOT NULL,
    to_stage    TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    note        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS promotions_no_update BEFORE UPDATE ON promotions
BEGIN SELECT RAISE(ABORT, 'promotion log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS promotions_no_delete BEFORE DELETE ON promotions
BEGIN SELECT RAISE(ABORT, 'promotion log is append-only'); END;
"""

ORDER: list[LifecycleStage] = list(LifecycleStage)


class PromotionError(HelmError):
    pass


@dataclass(frozen=True)
class Version:
    strategy: str
    code_hash: str
    params_hash: str

    def __str__(self) -> str:
        return f"{self.strategy}@{self.code_hash}/{self.params_hash}"


@dataclass
class AccountStats:
    """Evidence from a paper or live-small account journal."""

    days: float = 0.0
    fills: int = 0
    round_trips: int = 0
    halts: int = 0
    currently_halted: bool = False
    risk_rejections: int = 0


@dataclass
class Evidence:
    backtests: list[RunRecord] = field(default_factory=lambda: [])
    validations: list[RunRecord] = field(default_factory=lambda: [])
    lookahead_ok: bool | None = None
    paper: AccountStats | None = None
    live_small: AccountStats | None = None
    parity_confirmed: bool = False  # paper/backtest parity on a recorded feed (TEST-4)
    divergence_confirmed: bool = False  # PAP-4 divergence report reviewed
    rejections_reviewed: bool = False


@dataclass
class GateResult:
    target: LifecycleStage
    passed: bool
    reasons: list[str]
    evidence: dict[str, Any]


def _friction_on(rec: RunRecord) -> bool:
    fr = rec.report.get("settings", {}).get("friction", {})
    return bool(fr.get("fees")) and bool(fr.get("spread"))


def check_gate(target: LifecycleStage, ev: Evidence, cfg: PromotionConfig) -> GateResult:
    """Pure gate evaluation for promoting *into* `target` (§13 table)."""
    reasons: list[str] = []
    summary: dict[str, Any] = {}
    if target is S.BACKTESTED:
        good = [r for r in ev.backtests if _friction_on(r) and r.metrics.get("trade_count", 0) > 0]
        summary["backtest_runs"] = [r.run_id for r in good]
        if not good:
            reasons.append("no event-driven backtest with friction on and at least one trade")
        if ev.lookahead_ok is not True:
            reasons.append("lookahead truncation check has not passed")
    elif target is S.VALIDATED:
        passed = [r for r in ev.validations if r.passed]
        summary["validation_runs"] = [r.run_id for r in passed]
        if not passed:
            reasons.append("no passing validation run (helm research validate)")
        else:
            rep = passed[0].report
            checks = {c["name"]: c for c in rep.get("checks", [])}
            if "holdout" not in checks:
                reasons.append("locked holdout not evaluated (run validate with --holdout)")
            trades = checks.get("min_trades", {}).get("value", 0)
            if trades < cfg.min_trades_backtest:
                reasons.append(f"{trades} trades < promotion.min_trades_backtest")
            dsr = checks.get("deflated_sharpe", {}).get("value", 0.0)
            if dsr < float(cfg.min_deflated_sharpe):
                reasons.append(f"DSR {dsr:.3f} < promotion.min_deflated_sharpe")
    elif target is S.PAPER:
        summary["note"] = "code frozen: this version's code hash is the one recorded"
    elif target is S.LIVE_SMALL:
        p = ev.paper
        if p is None:
            reasons.append("no paper-account journal evidence")
        else:
            summary["paper"] = asdict(p)
            if p.days < cfg.min_paper_days and p.round_trips < cfg.min_paper_trades:
                reasons.append(
                    f"paper: {p.days:.1f} days / {p.round_trips} trades "
                    f"(need {cfg.min_paper_days} days or {cfg.min_paper_trades} trades)"
                )
            if p.currently_halted:
                reasons.append("paper engine is HALTED: resolve before promoting")
        if not ev.parity_confirmed:
            reasons.append("paper/backtest parity on a recorded feed not confirmed")
        if not ev.rejections_reviewed:
            reasons.append("risk rejections not confirmed as reviewed")
    elif target is S.LIVE:
        ls = ev.live_small
        if ls is None:
            reasons.append("no LIVE_SMALL account journal evidence")
        else:
            summary["live_small"] = asdict(ls)
            if ls.days < cfg.min_live_small_days:
                reasons.append(f"LIVE_SMALL for {ls.days:.1f} days < {cfg.min_live_small_days}")
            if ls.currently_halted:
                reasons.append("live engine is HALTED")
        if not ev.divergence_confirmed:
            reasons.append("paper-vs-live divergence report (PAP-4) not confirmed")
    else:
        reasons.append(f"cannot promote into {target.value}")
    return GateResult(target, not reasons, reasons, summary)


class PromotionRegistry:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def stage(self, v: Version) -> LifecycleStage:
        row = self._db.execute(
            "SELECT to_stage FROM promotions WHERE strategy=? AND code_hash=? AND params_hash=?"
            " ORDER BY seq DESC LIMIT 1",
            (v.strategy, v.code_hash, v.params_hash),
        ).fetchone()
        return LifecycleStage(row[0]) if row else S.DRAFT

    def history(self, strategy: str | None = None) -> list[dict[str, Any]]:
        if strategy:
            rows = self._db.execute(
                "SELECT * FROM promotions WHERE strategy = ? ORDER BY seq", (strategy,)
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM promotions ORDER BY seq").fetchall()
        cols = [d[0] for d in self._db.execute("SELECT * FROM promotions LIMIT 0").description]
        out = [dict(zip(cols, r, strict=True)) for r in rows]
        for r in out:
            r["evidence"] = json.loads(r["evidence"])
        return out

    def _append(
        self,
        v: Version,
        frm: LifecycleStage,
        to: LifecycleStage,
        ev: dict[str, Any],
        who: str,
        note: str,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO promotions (ts, strategy, code_hash, params_hash, from_stage,"
                " to_stage, evidence, approved_by, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    time.time_ns(),
                    v.strategy,
                    v.code_hash,
                    v.params_hash,
                    frm.value,
                    to.value,
                    json.dumps(ev, sort_keys=True, default=str),
                    who,
                    note,
                ),
            )

    def promote(
        self,
        v: Version,
        target: LifecycleStage,
        gate: GateResult,
        *,
        approved_by: str,
        note: str = "",
    ) -> LifecycleStage:
        cur = self.stage(v)
        if ORDER.index(target) != ORDER.index(cur) + 1:
            raise PromotionError(
                f"{v} is {cur.value}: next step is one stage up, not {target.value}"
            )
        if gate.target is not target or not gate.passed:
            raise PromotionError(f"gate into {target.value} not passed: {'; '.join(gate.reasons)}")
        if not approved_by:
            raise PromotionError("promotion needs a named manual approver")
        self._append(v, cur, target, gate.evidence, approved_by, note)
        return target

    def demote(self, v: Version, target: LifecycleStage, *, by: str, note: str) -> None:
        cur = self.stage(v)
        if ORDER.index(target) >= ORDER.index(cur):
            raise PromotionError(f"{target.value} is not below {cur.value}")
        self._append(v, cur, target, {"demotion": True}, by, note or "demoted")


def gather_evidence(
    store: ExperimentStore, v: Version, *, lookahead_ok: bool | None = None
) -> Evidence:
    runs = [r for r in store.runs(strategy=v.strategy, code=v.code_hash, limit=10_000)]
    mine = [r for r in runs if r.params_hash == v.params_hash]
    return Evidence(
        backtests=[r for r in mine if r.kind == "backtest"],
        validations=sorted(
            (r for r in mine if r.kind == "validation"), key=lambda r: r.ts, reverse=True
        ),
        lookahead_ok=lookahead_ok,
    )


def latest(records: Sequence[RunRecord]) -> RunRecord | None:
    return max(records, key=lambda r: r.ts) if records else None
