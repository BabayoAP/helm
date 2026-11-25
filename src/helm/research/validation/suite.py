"""Validation suite (RES-4, PRD §13 BACKTESTED -> VALIDATED).

Answers one question: is the best parameter set more than the luckiest of many
tries? Checks, each pass/fail with numbers:

1. selection on the development window (grid or Optuna, RES-3), trial count kept
2. minimum trade count
3. deflated Sharpe ratio against the number of trials actually run
4. parameter plateau: neighbours of the best point are not much worse
5. walk-forward: re-optimize on each train window, score on the next, unseen one
6. purged K-fold with embargo: same, with folds anywhere in time, train data near
   the test fold removed so overlapping information cannot leak
7. locked final holdout (optional): scored once, access logged by the caller

The suite only needs an `Evaluator`: (params, bars) -> EvalResult. The app layer
supplies the event-driven backtester (promotion evidence); tests can supply fast
synthetic evaluators. Research code never imports the engine.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeVar

from helm.backtest.metrics.metrics import deflated_sharpe, moments
from helm.strategy.params import ParamSpace, ParamValue

T = TypeVar("T")
Params = dict[str, ParamValue]


@dataclass(frozen=True)
class EvalResult:
    sharpe: float  # annualized; used for ranking and fold scores
    trades: int
    returns: tuple[float, ...]  # per-period returns (DSR input)
    total_return: float = 0.0

    @property
    def period_sharpe(self) -> float:
        r = self.returns
        if len(r) < 2:
            return 0.0
        sd = statistics.pstdev(r)
        return statistics.fmean(r) / sd if sd > 0 else 0.0


Evaluator = Callable[[Mapping[str, ParamValue], Sequence[T]], EvalResult]


@dataclass(frozen=True)
class ValidationConfig:
    holdout_frac: float = 0.2
    wf_folds: int = 4
    wf_anchored: bool = True
    cv_folds: int = 5
    purge_frac: float = 0.01  # label/lookback horizon removed before each test fold
    embargo_frac: float = 0.01  # removed after each test fold
    min_trades: int = 30
    min_dsr: float = 0.95
    min_wf_efficiency: float = 0.5  # mean OOS Sharpe / mean IS Sharpe
    min_oos_sharpe: float = 0.0
    min_positive_folds: float = 0.5
    plateau_min_ratio: float = 0.5  # median neighbour Sharpe / best Sharpe
    min_holdout_sharpe: float = 0.0
    optimizer: Literal["grid", "optuna"] = "grid"
    optuna_trials: int = 50
    seed: int = 0
    prior_trials: int = 0  # trials already spent on this strategy version elsewhere


@dataclass
class Check:
    name: str
    passed: bool
    value: float
    threshold: float
    detail: str = ""


@dataclass
class FoldResult:
    train: tuple[int, int]
    test: tuple[int, int]
    params: Params
    is_sharpe: float
    oos_sharpe: float
    oos_trades: int


@dataclass
class ValidationReport[T]:
    best_params: Params
    n_trials: int
    dev_range: tuple[int, int]
    holdout_range: tuple[int, int]
    checks: list[Check] = field(default_factory=lambda: [])
    walk_forward: list[FoldResult] = field(default_factory=lambda: [])
    purged_cv: list[FoldResult] = field(default_factory=lambda: [])
    holdout: dict[str, float] | None = None
    trials: list[tuple[Params, float]] = field(default_factory=lambda: [])

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("trials")
        d["passed"] = self.passed
        return d


# ------------------------------------------------------------------ splits
def walk_forward_splits(
    n: int, folds: int, *, anchored: bool = True
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """(train, test) half-open index ranges. The first chunk is training only."""
    if folds < 1 or n < folds + 1:
        raise ValueError("not enough data for walk-forward")
    size = n // (folds + 1)
    out: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for k in range(1, folds + 1):
        test = (k * size, n if k == folds else (k + 1) * size)
        train = (0 if anchored else (k - 1) * size, k * size)
        out.append((train, test))
    return out


def purged_kfold(
    n: int, k: int, *, purge: int, embargo: int
) -> list[tuple[list[tuple[int, int]], tuple[int, int]]]:
    """K contiguous test folds; train = everything else minus `purge` bars before
    and `embargo` bars after the test fold (López de Prado). Train is returned as
    contiguous segments so event-driven backtests never jump across a gap."""
    if k < 2 or n < 2 * k:
        raise ValueError("not enough data for purged k-fold")
    bounds = [n * i // k for i in range(k + 1)]
    out: list[tuple[list[tuple[int, int]], tuple[int, int]]] = []
    for i in range(k):
        t0, t1 = bounds[i], bounds[i + 1]
        segs = [(0, max(0, t0 - purge)), (min(n, t1 + embargo), n)]
        out.append(([s for s in segs if s[1] - s[0] > 0], (t0, t1)))
    return out


# ---------------------------------------------------------------- optimize
def _segment_score[T](
    evaluate: Evaluator[T], p: Params, data: Sequence[T], segs: Sequence[tuple[int, int]]
) -> EvalResult:
    """Score params over several contiguous segments: trade-weighted mean Sharpe."""
    results = [evaluate(p, data[a:b]) for a, b in segs]
    trades = sum(r.trades for r in results)
    rets = tuple(x for r in results for x in r.returns)
    weights = [max(b - a, 1) for a, b in segs]
    sharpe = sum(r.sharpe * w for r, w in zip(results, weights, strict=True)) / sum(weights)
    return EvalResult(sharpe=sharpe, trades=trades, returns=rets)


def optimize[T](
    evaluate: Evaluator[T],
    space: ParamSpace,
    data: Sequence[T],
    segs: Sequence[tuple[int, int]],
    cfg: ValidationConfig,
) -> tuple[Params, EvalResult, list[tuple[Params, EvalResult]]]:
    trials: list[tuple[Params, EvalResult]] = []
    if cfg.optimizer == "grid":
        for p in space.grid():
            trials.append((p, _segment_score(evaluate, p, data, segs)))
    else:
        trials = _optuna(evaluate, space, data, segs, cfg)
    if not trials:
        raise ValueError("parameter space is empty")
    best = max(trials, key=lambda t: (t[1].sharpe, t[1].trades))
    return best[0], best[1], trials


def _optuna[T](
    evaluate: Evaluator[T],
    space: ParamSpace,
    data: Sequence[T],
    segs: Sequence[tuple[int, int]],
    cfg: ValidationConfig,
) -> list[tuple[Params, EvalResult]]:
    import optuna  # noqa: PLC0415 - heavy import, only when asked for

    from helm.strategy.params import Choice, FloatRange, IntRange  # noqa: PLC0415

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    seen: dict[str, tuple[Params, EvalResult]] = {}

    def objective(trial: optuna.Trial) -> float:
        p: Params = dict(space.fixed)
        for name, dim in sorted(space.dims.items()):
            if isinstance(dim, IntRange):
                p[name] = trial.suggest_int(name, dim.lo, dim.hi, step=dim.step)
            elif isinstance(dim, FloatRange):
                p[name] = trial.suggest_float(name, dim.lo, dim.hi, step=dim.step)
            elif isinstance(dim, Choice):
                p[name] = trial.suggest_categorical(name, list(dim.options))  # type: ignore[arg-type]
        if not space.constraint(p):
            raise optuna.TrialPruned("constraint")
        key = repr(sorted(p.items()))
        if key not in seen:
            seen[key] = (p, _segment_score(evaluate, p, data, segs))
        return seen[key][1].sharpe

    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=cfg.seed)
    )
    study.optimize(objective, n_trials=cfg.optuna_trials)
    return list(seen.values())


# -------------------------------------------------------------------- suite
def _fold[T](
    evaluate: Evaluator[T],
    space: ParamSpace,
    data: Sequence[T],
    train: Sequence[tuple[int, int]],
    test: tuple[int, int],
    cfg: ValidationConfig,
) -> FoldResult:
    p, is_res, _ = optimize(evaluate, space, data, train, cfg)
    oos = evaluate(p, data[test[0] : test[1]])
    return FoldResult(
        train=(train[0][0], train[-1][1]),
        test=test,
        params=p,
        is_sharpe=is_res.sharpe,
        oos_sharpe=oos.sharpe,
        oos_trades=oos.trades,
    )


def validate[T](
    evaluate: Evaluator[T],
    space: ParamSpace,
    data: Sequence[T],
    cfg: ValidationConfig | None = None,
    *,
    open_holdout: Callable[[], None] | None = None,
) -> ValidationReport[T]:
    """Run the suite. Pass `open_holdout` (which logs the access and may refuse)
    to also score the locked holdout; without it the holdout is never touched."""
    cfg = cfg or ValidationConfig()
    n = len(data)
    dev_n = int(n * (1 - cfg.holdout_frac))
    dev = data[:dev_n]
    best, best_res, trials = optimize(evaluate, space, dev, [(0, dev_n)], cfg)
    n_trials = len(trials) + cfg.prior_trials
    rep: ValidationReport[T] = ValidationReport(
        best_params=best,
        n_trials=n_trials,
        dev_range=(0, dev_n),
        holdout_range=(dev_n, n),
        trials=[(p, r.sharpe) for p, r in trials],
    )

    # 2. trade count
    rep.checks.append(
        Check(
            "min_trades",
            best_res.trades >= cfg.min_trades,
            best_res.trades,
            cfg.min_trades,
            f"{best_res.trades} trades (need {cfg.min_trades})",
        )
    )

    # 3. deflated Sharpe: variance of the trial Sharpes estimates how far luck alone goes
    skew, kurt = moments(best_res.returns)
    trial_srs = [r.period_sharpe for _, r in trials]
    sr_var = statistics.pvariance(trial_srs) if len(trial_srs) > 1 else None
    dsr = deflated_sharpe(
        best_res.period_sharpe, len(best_res.returns), skew, kurt, n_trials, sr_var or None
    )
    rep.checks.append(
        Check(
            "deflated_sharpe",
            dsr >= cfg.min_dsr,
            dsr,
            cfg.min_dsr,
            f"DSR {dsr:.3f} over {n_trials} trials (need {cfg.min_dsr})",
        )
    )

    # 4. plateau
    by_key = {repr(sorted(p.items())): r for p, r in trials}
    neigh = [
        by_key[k].sharpe for q in space.neighbors(best) if (k := repr(sorted(q.items()))) in by_key
    ]
    if best_res.sharpe <= 0:
        ratio = 0.0
    elif not neigh:
        ratio = math.nan
    else:
        ratio = statistics.median(neigh) / best_res.sharpe
    rep.checks.append(
        Check(
            "parameter_plateau",
            (not math.isnan(ratio)) and ratio >= cfg.plateau_min_ratio,
            ratio,
            cfg.plateau_min_ratio,
            "no evaluated neighbours (Optuna sparse sample?)"
            if math.isnan(ratio)
            else f"median neighbour Sharpe is {ratio:.0%} of the best",
        )
    )

    # 5. walk-forward
    for train, test in walk_forward_splits(dev_n, cfg.wf_folds, anchored=cfg.wf_anchored):
        rep.walk_forward.append(_fold(evaluate, space, dev, [train], test, cfg))
    _fold_checks(rep, "walk_forward", rep.walk_forward, cfg, efficiency=True)

    # 6. purged k-fold
    purge, embargo = int(dev_n * cfg.purge_frac), int(dev_n * cfg.embargo_frac)
    for train_segs, test in purged_kfold(dev_n, cfg.cv_folds, purge=purge, embargo=embargo):
        rep.purged_cv.append(_fold(evaluate, space, dev, train_segs, test, cfg))
    _fold_checks(rep, "purged_cv", rep.purged_cv, cfg, efficiency=False)

    # 7. locked holdout, only if explicitly opened (and logged) by the caller
    if open_holdout is not None and dev_n < n:
        open_holdout()
        h = evaluate(best, data[dev_n:])
        rep.holdout = {"sharpe": h.sharpe, "trades": float(h.trades), "return": h.total_return}
        rep.checks.append(
            Check(
                "holdout",
                h.sharpe >= cfg.min_holdout_sharpe and h.trades > 0,
                h.sharpe,
                cfg.min_holdout_sharpe,
                f"holdout Sharpe {h.sharpe:.2f}, {h.trades} trades",
            )
        )
    return rep


def _fold_checks(
    rep: ValidationReport[Any],
    name: str,
    folds: Sequence[FoldResult],
    cfg: ValidationConfig,
    *,
    efficiency: bool,
) -> None:
    oos = [f.oos_sharpe for f in folds]
    mean_oos = statistics.fmean(oos)
    positive = sum(1 for s in oos if s > 0) / len(oos)
    rep.checks.append(
        Check(
            f"{name}_oos_sharpe",
            mean_oos > cfg.min_oos_sharpe and positive >= cfg.min_positive_folds,
            mean_oos,
            cfg.min_oos_sharpe,
            f"mean OOS Sharpe {mean_oos:.2f}, {positive:.0%} of folds positive",
        )
    )
    if efficiency:
        mean_is = statistics.fmean(f.is_sharpe for f in folds)
        wfe = mean_oos / mean_is if mean_is > 0 else 0.0
        rep.checks.append(
            Check(
                f"{name}_efficiency",
                wfe >= cfg.min_wf_efficiency,
                wfe,
                cfg.min_wf_efficiency,
                f"OOS keeps {wfe:.0%} of in-sample Sharpe",
            )
        )
