"""RES-5 stage 1: static allowlist check of a draft strategy (no code is executed).

A draft may import only pure, deterministic modules and the Strategy API. It may
not touch files, the network, processes, the environment, the clock, randomness
or interpreter internals. This is the first of two layers: the sandbox runner
also blocks those operations at runtime with audit hooks, so a trick that slips
past the static check still fails.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "decimal",
        "enum",
        "itertools",
        "math",
        "statistics",
        "typing",
        "helm.core.enums",
        "helm.core.models",
        "helm.strategy.api",
        "helm.strategy.params",
        "helm.strategy.examples.common",
    }
)
ALLOWED_DUNDER_ATTRS: frozenset[str] = frozenset({"__init__"})  # super().__init__(...)
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "memoryview",
        "help",
        "exit",
        "quit",
    }
)


@dataclass
class AstReport:
    ok: bool
    problems: list[str] = field(default_factory=lambda: [])
    strategy_class: str | None = None


def check_source(source: str) -> AstReport:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return AstReport(False, [f"syntax error: {exc.msg} (line {exc.lineno})"])
    problems: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    problems.append(f"line {line}: import {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or mod not in ALLOWED_IMPORTS:
                problems.append(f"line {line}: from {'.' * node.level}{mod} import is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {line}: use of {node.id}() is not allowed")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
            and node.attr not in ALLOWED_DUNDER_ATTRS
        ):
            problems.append(f"line {line}: dunder attribute {node.attr} is not allowed")
        elif isinstance(node, ast.Name) and node.id.startswith("__") and node.id.endswith("__"):
            if node.id not in ("__name__",):
                problems.append(f"line {line}: dunder name {node.id} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {line}: global/nonlocal state is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            problems.append(f"line {line}: async code is not allowed in strategies")
    classes = [
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id == "BaseStrategy" for b in n.bases)
    ]
    if len(classes) != 1:
        problems.append(f"expected exactly one BaseStrategy subclass, found {len(classes)}")
    for n in tree.body:  # module level: only imports, classes, functions, constants, docstring
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef)):
            continue
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue
        problems.append(f"line {n.lineno}: top-level statement {type(n).__name__} is not allowed")
    return AstReport(not problems, problems, classes[0].name if len(classes) == 1 else None)
