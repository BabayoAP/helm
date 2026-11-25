"""RES-5 pipeline: static allowlist -> lint -> sandboxed lookahead test + smoke backtest.

Output is a *draft* and a report. Drafts are stored outside the `helm.strategy`
package, and `helm.strategy.loader` only loads `helm.strategy.*`, so a draft
can never be backtested for promotion, promoted or deployed without a human
moving it into the codebase (where review, tests and the promotion gates apply).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from helm.research.sandbox.ast_check import check_source


@dataclass
class DraftReport:
    ok: bool
    source_hash: str
    static: list[str] = field(default_factory=lambda: [])
    lint: list[str] | None = None  # None = linter unavailable
    sandbox: dict[str, Any] = field(default_factory=lambda: {})
    draft_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lint(path: Path) -> list[str] | None:
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    if not Path(ruff).exists():
        return None
    proc = subprocess.run(  # noqa: S603
        [ruff, "check", "--no-cache", "--output-format", "concise", "--select", "E,F,B", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [
        ln for ln in proc.stdout.splitlines() if ln and not ln.startswith(("Found", "All checks"))
    ]


def run_sandboxed(
    source: str, *, timeout_s: int = 120, cpu_s: int = 90, mem_mb: int = 1536
) -> dict[str, Any]:
    """Run the draft in a child process with no environment and resource limits."""
    with tempfile.TemporaryDirectory(prefix="helm-sandbox-") as tmp:
        f = Path(tmp) / "draft.py"
        f.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(  # noqa: S603 - fixed interpreter + module
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "helm.research.sandbox.runner",
                    str(f),
                    str(cpu_s),
                    str(mem_mb),
                ],
                cwd=tmp,
                env={"PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin"},  # no credentials, ever
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout after {timeout_s}s"}
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        return {
            "ok": False,
            "error": f"sandbox crashed (exit {proc.returncode}): {proc.stderr[-400:]}",
        }
    result: dict[str, Any] = json.loads(lines[-1])
    return result


def check_draft(
    source: str, *, drafts_dir: Path | None = None, timeout_s: int = 120
) -> DraftReport:
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    rep = DraftReport(ok=False, source_hash=digest)
    st = check_source(source)
    rep.static = st.problems
    if not st.ok:
        return rep  # never execute code that failed the static check
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "draft.py"
        p.write_text(source, encoding="utf-8")
        rep.lint = _lint(p)
    rep.sandbox = run_sandboxed(source, timeout_s=timeout_s)
    rep.ok = bool(rep.sandbox.get("ok")) and not rep.lint
    if drafts_dir is not None:
        drafts_dir.mkdir(parents=True, exist_ok=True)
        name = f"{(st.strategy_class or 'draft').lower()}-{digest}"
        (drafts_dir / f"{name}.py").write_text(source, encoding="utf-8")
        (drafts_dir / f"{name}.report.json").write_text(
            json.dumps(rep.to_dict(), indent=2, default=str)
        )
        rep.draft_path = str(drafts_dir / f"{name}.py")
    return rep
