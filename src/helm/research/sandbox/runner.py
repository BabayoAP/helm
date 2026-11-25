"""RES-5 stage 3: run a draft strategy inside a locked-down child process.

Started by `run_sandboxed` as `python -I -m helm.research.sandbox.runner <draft.py>`
with an empty environment (no credentials), a temp working directory, CPU and
memory limits, and a wall-clock timeout. Before any draft code runs, an audit
hook is installed that blocks file writes, network sockets, subprocesses,
os.system/exec/fork, ctypes, deleting/renaming files and changing the
environment. Then the draft gets the lookahead truncation test and a smoke
backtest on seeded synthetic data. The result is one JSON line on stdout.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_BLOCKED_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.chmod",
    "os.chown",
    "os.putenv",
    "os.unsetenv",
    "os.truncate",
    "shutil.",
    "ctypes.",
    "urllib.",
    "http.client.",
    "ftplib.",
    "smtplib.",
    "webbrowser.",
    "sys._getframe",
    "sys.setrecursionlimit",
    "sqlite3.connect",
    "pty.",
    "fcntl.",
    "msvcrt.",
    "winreg.",
)
_ALLOWED_SQLITE = (":memory:",)


class SandboxViolation(RuntimeError):
    pass


# every blocked attempt is recorded here: the engine catches strategy exceptions (by
# design), so the raise alone could be swallowed; the record cannot
VIOLATIONS: list[str] = []


def _deny(msg: str) -> None:
    VIOLATIONS.append(msg)
    raise SandboxViolation(msg)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        path, mode = args[0], args[1] if len(args) > 1 else "r"
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            _deny(f"sandbox: write to {path!r} blocked")
        if isinstance(mode, int) and mode & 0o3:  # os.open flags: O_WRONLY / O_RDWR
            _deny(f"sandbox: write-open of {path!r} blocked")
        return
    if event == "sqlite3.connect" and args and str(args[0]) in _ALLOWED_SQLITE:
        return
    if event == "sqlite3.connect/handle":
        return  # follows a connect that already passed the check above
    if event == "os.mkdir" and args and os.path.isdir(str(args[0])):
        return  # mkdir(exist_ok=True) of an existing dir changes nothing
    if event.startswith(_BLOCKED_PREFIXES):
        _deny(f"sandbox: {event} blocked")


def _limits(cpu_s: int, mem_mb: int) -> None:
    import resource  # noqa: PLC0415

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    for lim in ("RLIMIT_AS", "RLIMIT_DATA"):
        try:
            res = getattr(resource, lim)
            resource.setrlimit(res, (mem_mb * 2**20, mem_mb * 2**20))
        except (ValueError, OSError, AttributeError):
            continue  # not enforceable on this OS (macOS ignores RLIMIT_AS)


def main(argv: list[str]) -> int:
    path = argv[0]
    cpu_s = int(argv[1]) if len(argv) > 1 else 60
    mem_mb = int(argv[2]) if len(argv) > 2 else 1024
    with open(path, encoding="utf-8") as fh:  # read the draft BEFORE locking down
        source = fh.read()
    # everything the checks need is imported before the hook, so the draft cannot
    # use our imports to reach anything new
    import asyncio  # noqa: PLC0415
    import types  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    from helm.app.backtest import BacktestSpec, run_backtest_async  # noqa: PLC0415
    from helm.app.research import instrument_for_symbol as instrument  # noqa: PLC0415
    from helm.data.synthetic import frame_to_bars, gbm_frame  # noqa: PLC0415
    from helm.research.validation.lookahead import check_strategy  # noqa: PLC0415
    from helm.strategy.api import BaseStrategy  # noqa: PLC0415
    from helm.strategy.examples import common as _common  # noqa: F401, PLC0415
    from helm.strategy.params import ParamSpace  # noqa: F401, PLC0415

    symbol = "fake:BTC/USD"
    bars = list(frame_to_bars(gbm_frame(3000, start_ns=0, timeframe="1h", seed=11), symbol, "1h"))
    loop = asyncio.new_event_loop()  # its self-pipe socket is created before lockdown
    _limits(cpu_s, mem_mb)
    sys.addaudithook(_hook)

    out: dict[str, Any] = {"stage": "load"}
    try:
        mod = types.ModuleType("helm_draft")
        exec(compile(source, "<draft>", "exec"), mod.__dict__)  # noqa: S102 - the sandboxed draft
        classes = [
            v
            for v in mod.__dict__.values()
            if isinstance(v, type) and issubclass(v, BaseStrategy) and v is not BaseStrategy
        ]
        if len(classes) != 1:
            raise SandboxViolation(f"expected one strategy class, found {len(classes)}")
        cls = classes[0]
        space = getattr(cls, "param_space", None)
        params: dict[str, Any] = next(space.grid(), {}) if space is not None else {}
        insts = [instrument(symbol)]

        def run(bs: Any) -> list[Any]:
            if not bs:
                return []
            spec = BacktestSpec(strategy=cls(), instruments=insts, params=dict(params))
            res = loop.run_until_complete(run_backtest_async(spec, bs))
            index = {b.ts_close: i for i, b in enumerate(bs)}
            return [(index[o.ts_created], o.intent) for o in res.orders]

        out["stage"] = "lookahead"
        look = check_strategy(cls.__name__, run, bars[:900], (300, 600))
        out["lookahead_ok"] = look.ok
        out["stage"] = "smoke_backtest"
        spec = BacktestSpec(
            strategy=cls(), instruments=insts, params=dict(params), starting_equity=Decimal(10_000)
        )
        res = loop.run_until_complete(run_backtest_async(spec, bars))
        m = res.metrics
        out |= {
            "stage": "done",
            "ok": look.ok and not VIOLATIONS,
            "class": cls.__name__,
            "strategy_id": getattr(cls, "id", ""),
            "params": params,
            "orders": len(res.orders),
            "trades": m.trade_count,
            "total_return": m.total_return,
            "sharpe": m.sharpe,
            "max_drawdown": m.max_drawdown,
            "risk_rejections": res.stats.get("rejected", 0),
            "strategy_errors": res.stats.get("strategy_errors", 0),
        }
    except BaseException as exc:
        out |= {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    out["violations"] = VIOLATIONS[:20]
    if VIOLATIONS:
        out["ok"] = False
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
