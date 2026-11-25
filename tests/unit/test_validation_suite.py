"""RES-4 validation suite + RES-2 experiment store.

The regression test PRD RES-4 asks for: a deliberately overfit strategy (pick the
luckiest of 200 pure-noise variants) must be REJECTED, while a strategy with a
real (synthetic) edge must PASS, so the suite is not just rejecting everything.
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from helm.research.experiments.store import (
    ExperimentStore,
    HoldoutLockedError,
    code_hash,
    params_hash,
)
from helm.research.validation.suite import (
    EvalResult,
    ValidationConfig,
    purged_kfold,
    validate,
    walk_forward_splits,
)
from helm.strategy.examples.sma_cross import SmaCross
from helm.strategy.params import Choice, FloatRange, IntRange, ParamSpace, ParamValue

N = 2000
DATA = list(range(N))  # bar indices: slices keep their global position


def _noise(n_variants: int, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.gauss(0, 0.01) for _ in range(N)] for _ in range(n_variants)]


NOISE = _noise(200, 1)


def _result(rets: list[float]) -> EvalResult:
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sr = statistics.fmean(rets) / sd * math.sqrt(252) if sd > 0 else 0.0
    return EvalResult(
        sharpe=sr, trades=len(rets) // 10, returns=tuple(rets), total_return=sum(rets)
    )


def overfit(p: Mapping[str, ParamValue], data: Sequence[int]) -> EvalResult:
    """Each 'seed' is a different pure-noise return stream: no variant has an edge."""
    s = int(p["seed"])
    return _result([NOISE[s][i] for i in data])


def genuine(p: Mapping[str, ParamValue], data: Sequence[int]) -> EvalResult:
    """Every variant earns a small real drift; k only changes the noise."""
    k = int(p["k"])
    return _result([0.0012 + NOISE[k][i] for i in data])


FAST = ValidationConfig(min_trades=30)


def test_overfit_strategy_is_rejected() -> None:
    rep = validate(overfit, ParamSpace({"seed": IntRange(0, 199)}), DATA, FAST)
    assert not rep.passed
    by = {c.name: c for c in rep.checks}
    assert rep.n_trials == 200
    assert by["min_trades"].passed  # it trades plenty: count alone can't catch it
    # the selected variant looked good in-sample...
    best_is = max(s for _, s in rep.trials)
    assert best_is > 1.0
    # ...but luck is exposed from several independent angles
    assert not by["deflated_sharpe"].passed
    assert not by["walk_forward_efficiency"].passed or not by["walk_forward_oos_sharpe"].passed
    assert not by["parameter_plateau"].passed
    assert rep.failures() and rep.to_dict()["passed"] is False


def test_genuine_edge_passes_and_holdout_is_logged_once(tmp_path: Path) -> None:
    opened: list[int] = []
    rep = validate(
        genuine,
        ParamSpace({"k": IntRange(0, 4)}),
        DATA,
        FAST,
        open_holdout=lambda: opened.append(1),
    )
    assert rep.passed, rep.failures()
    assert opened == [1] and rep.holdout is not None and rep.holdout["sharpe"] > 0
    assert rep.dev_range == (0, 1600) and rep.holdout_range == (1600, 2000)
    assert len(rep.walk_forward) == 4 and len(rep.purged_cv) == 5
    # no holdout callback => the holdout is never evaluated
    assert validate(genuine, ParamSpace({"k": IntRange(0, 4)}), DATA, FAST).holdout is None


def test_optuna_optimizer_is_seeded_and_respects_constraints() -> None:
    space = ParamSpace(
        {"seed": IntRange(0, 199), "x": FloatRange(0.0, 1.0, 0.5), "c": Choice(("a", "b"))},
        constraint=lambda p: p["c"] == "a" or float(p["x"]) < 1.0,
    )
    cfg = ValidationConfig(optimizer="optuna", optuna_trials=25, seed=3, wf_folds=2, cv_folds=2)
    a = validate(overfit, space, DATA, cfg)
    b = validate(overfit, space, DATA, cfg)
    assert a.best_params == b.best_params  # deterministic with a seed
    assert all(p["c"] == "a" or float(p["x"]) < 1.0 for p, _ in a.trials)
    assert not a.passed


def test_splits_are_disjoint_ordered_and_purged() -> None:
    wf = walk_forward_splits(1000, 4)
    assert wf[0] == ((0, 200), (200, 400)) and wf[-1][1] == (800, 1000)
    assert all(tr[1] <= te[0] for tr, te in wf)  # train strictly before test
    rolling = walk_forward_splits(1000, 4, anchored=False)
    assert rolling[2][0] == (400, 600)
    folds = purged_kfold(1000, 5, purge=10, embargo=20)
    for segs, (t0, t1) in folds:
        for a, b in segs:
            assert b <= t0 - 10 or a >= t1 + 20  # nothing within purge/embargo of test
    assert folds[0][0] == [(220, 1000)] and folds[-1][0] == [(0, 790)]
    with pytest.raises(ValueError):
        walk_forward_splits(3, 4)
    with pytest.raises(ValueError):
        purged_kfold(3, 2, purge=0, embargo=0)
    with pytest.raises(ValueError, match="empty"):
        validate(overfit, ParamSpace({"seed": IntRange(0, 1)}, constraint=lambda p: False), DATA)


def test_param_space_grid_and_neighbors() -> None:
    sp = SmaCross.param_space
    grid = list(sp.grid())
    assert all(int(p["fast"]) < int(p["slow"]) for p in grid) and len(grid) == sp.size()
    nb = sp.neighbors({"fast": 10, "slow": 40})
    assert {"fast": 5, "slow": 40} in nb and {"fast": 10, "slow": 60} in nb
    assert sp.neighbors({"fast": 7, "slow": 40}) == [
        {"fast": 7, "slow": 20},
        {"fast": 7, "slow": 60},
    ]
    assert FloatRange(0.1, 0.3, 0.1).values() == [0.1, 0.2, 0.3]


def test_experiment_store_is_append_only_and_counts_trials(tmp_path: Path) -> None:
    st = ExperimentStore(tmp_path / "exp.sqlite")
    strat = SmaCross()
    for i, kind in enumerate(("backtest", "optimize", "validation")):
        st.record(
            run_id=f"r{i}",
            kind=kind,
            strategy=strat,
            params={"fast": 10, "slow": 30},
            data_hash="d",
            data_desc="synthetic",
            spec_hash="s",
            seed=0,
            n_trials=1 if kind == "backtest" else 36,
            metrics={"sharpe": 1.0},
            result_hash="h",
            passed=True if kind == "validation" else None,
            git="abc",
        )
    path = "helm.strategy.examples.sma_cross:SmaCross"
    ch = code_hash(strat)
    assert st.trial_count(path, ch) == 37  # validation runs don't double count
    rec = st.get("r2")
    assert (
        rec is not None
        and rec.passed is True
        and rec.params_hash == params_hash({"slow": 30, "fast": 10})
    )
    assert [r.run_id for r in st.runs(kind="backtest")] == ["r0"]
    assert st.get("nope") is None
    with pytest.raises(sqlite3.IntegrityError):
        st._db.execute("DELETE FROM runs")  # pyright: ignore[reportPrivateUsage]
    # locked holdout: once per strategy version, re-looks need force and are logged
    st.open_holdout(strat, "d", who="cli", reason="")
    with pytest.raises(HoldoutLockedError):
        st.open_holdout(strat, "d", who="cli", reason="again")
    st.open_holdout(strat, "d", who="cli", reason="bug in data, re-run", force=True)
    log = st.holdout_accesses(path, ch)
    assert [a["reason"] for a in log] == ["first access", "bug in data, re-run"]
    st.close()
