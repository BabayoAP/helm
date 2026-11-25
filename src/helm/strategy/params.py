"""Declared parameter spaces (RES-3). Pure data: strategies may import this.

A strategy declares `param_space = ParamSpace(...)` so optimizers search only
what the author intended and the trial count is honest (it feeds the deflated
Sharpe ratio). `constraint` rejects nonsensical combinations (e.g. fast >= slow).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

ParamValue = float | int | str | bool


@dataclass(frozen=True)
class IntRange:
    lo: int
    hi: int
    step: int = 1

    def values(self) -> list[int]:
        return list(range(self.lo, self.hi + 1, self.step))


@dataclass(frozen=True)
class FloatRange:
    lo: float
    hi: float
    step: float

    def values(self) -> list[float]:
        n = round((self.hi - self.lo) / self.step)
        return [round(self.lo + i * self.step, 12) for i in range(n + 1)]


@dataclass(frozen=True)
class Choice:
    options: tuple[ParamValue, ...]

    def values(self) -> list[ParamValue]:
        return list(self.options)


Dim = IntRange | FloatRange | Choice


def _always(_p: Mapping[str, ParamValue]) -> bool:
    return True


@dataclass(frozen=True)
class ParamSpace:
    dims: dict[str, Dim]
    fixed: dict[str, ParamValue] = field(default_factory=lambda: {})
    constraint: Callable[[Mapping[str, ParamValue]], bool] = _always

    def grid(self) -> Iterator[dict[str, ParamValue]]:
        names = sorted(self.dims)
        for combo in itertools.product(*(self.dims[n].values() for n in names)):
            p: dict[str, ParamValue] = {**self.fixed, **dict(zip(names, combo, strict=True))}
            if self.constraint(p):
                yield p

    def size(self) -> int:
        return sum(1 for _ in self.grid())

    def neighbors(self, p: Mapping[str, ParamValue]) -> list[dict[str, ParamValue]]:
        """Adjacent grid points (one dimension moved one step), used by the plateau check."""
        out: list[dict[str, ParamValue]] = []
        for name, dim in sorted(self.dims.items()):
            vals: list[ParamValue] = list(dim.values())
            if p[name] not in vals:
                continue
            i = vals.index(p[name])
            for j in (i - 1, i + 1):
                if 0 <= j < len(vals):
                    q = {**p, name: vals[j]}
                    if self.constraint(q):
                        out.append(q)
        return out
