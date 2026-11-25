"""RES-5: malicious / accidental draft code is blocked (static allowlist AND runtime
audit hooks in an isolated, credential-free child process); a good draft passes;
a draft can never be promoted or deployed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helm.research.sandbox.ast_check import check_source
from helm.research.sandbox.pipeline import check_draft, run_sandboxed
from helm.strategy.loader import load_strategy

GOOD = Path("src/helm/strategy/examples/sma_cross.py").read_text().replace('"0.01"', '"1"')

HEADER = """
from helm.core.models import Bar, OrderIntent
from helm.strategy.api import BaseStrategy, Context
"""


def strat(body: str, top: str = "") -> str:
    return f"""{HEADER}{top}
class Evil(BaseStrategy):
    id = "evil"

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
{body}
        return []
"""


@pytest.mark.parametrize(
    ("src", "needle"),
    [
        (strat("        pass", "import os"), "import os"),
        (strat("        pass", "import subprocess"), "import subprocess"),
        (strat("        pass", "from socket import socket"), "from socket"),
        (strat("        pass", "import helm.infra.secrets"), "import helm.infra.secrets"),
        (strat("        pass", "from . import x"), "from . import"),
        (strat("        open('/tmp/x', 'w').write('pwned')"), "open()"),
        (strat("        eval('1+1')"), "eval()"),
        (strat("        exec('import os')"), "exec()"),
        (strat("        __import__('os').system('id')"), "__import__"),
        (strat("        ().__class__.__bases__[0].__subclasses__()"), "__class__"),
        (strat("        getattr(bar, 'x')"), "getattr()"),
        (strat("        pass", "import random"), "import random"),
        (strat("        pass", "import time"), "import time"),
        (strat("        pass", "print('side effect at import')"), "top-level statement Expr"),
        (strat("        global X"), "global/nonlocal"),
        ("x = 1\n", "exactly one BaseStrategy"),
        ("def broken(:\n", "syntax error"),
    ],
)
def test_static_check_blocks(src: str, needle: str) -> None:
    rep = check_source(src)
    assert not rep.ok
    assert any(needle in p for p in rep.problems), rep.problems


def test_good_draft_passes_full_pipeline(tmp_path: Path) -> None:
    rep = check_draft(GOOD, drafts_dir=tmp_path / "drafts")
    assert rep.ok, rep.to_dict()
    sb = rep.sandbox
    assert sb["lookahead_ok"] and sb["orders"] > 0 and sb["violations"] == []
    assert rep.draft_path and Path(rep.draft_path).exists()
    assert Path(rep.draft_path.replace(".py", ".report.json")).exists()
    # a draft can never reach promotion/deploy: the loader only takes helm.strategy.*
    with pytest.raises(ValueError, match=r"helm\.strategy"):
        load_strategy(f"drafts.{Path(rep.draft_path).stem}:SmaCross")


def test_failed_static_check_never_executes(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    src = strat("        pass", f"import os\nos.system('touch {marker}')")
    rep = check_draft(src)
    assert not rep.ok and rep.sandbox == {} and not marker.exists()


# Second layer: skip the static check on purpose and prove the runtime guard holds.
@pytest.mark.parametrize(
    ("src", "needle"),
    [
        ("import os\nos.system('echo hi')\n" + strat("        pass"), "os.system"),
        ("open('pwned.txt', 'w')\n" + strat("        pass"), "write to"),
        (
            "import socket\nsocket.create_connection(('1.1.1.1', 80))\n" + strat("        pass"),
            "socket",
        ),
        ("import subprocess\nsubprocess.run(['id'])\n" + strat("        pass"), "subprocess"),
        ("import shutil\nshutil.rmtree('/tmp/whatever')\n" + strat("        pass"), "shutil"),
        ("import os\nos.remove('x')\n" + strat("        pass"), "os.remove"),
        # inside on_bar: the engine swallows strategy exceptions, the violation log does not
        (strat("        open('pwned.txt', 'a')"), "write to"),
    ],
)
def test_runtime_guard_blocks_even_without_static_check(src: str, needle: str) -> None:
    out = run_sandboxed(src, timeout_s=60)
    assert out["ok"] is False, out
    assert needle in (out.get("error", "") + " ".join(out.get("violations", []))), out


def test_sandbox_has_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELM_SECRET_ALPACA_PAPER_SECRET_KEY", "super-secret-value-123")
    src = strat(
        "        import os\n"
        "        assert os.environ.get('HELM_SECRET_ALPACA_PAPER_SECRET_KEY') is None",
        "",
    )
    out = run_sandboxed(src, timeout_s=60)
    assert out.get("strategy_errors", 0) == 0, out  # the env var is simply not there
    assert "super-secret-value-123" not in str(out)
    assert os.environ["HELM_SECRET_ALPACA_PAPER_SECRET_KEY"]  # parent untouched


def test_runaway_draft_is_killed() -> None:
    out = run_sandboxed(strat("        while True:\n            pass"), timeout_s=5, cpu_s=3)
    assert out["ok"] is False
    assert "timeout" in out.get("error", "") or "crashed" in out.get("error", "")


# ------------------------------------------------------------- idea -> draft (Claude)
class _Resp:
    def __init__(self, text: str, stop: str = "end_turn") -> None:
        self.stop_reason = stop
        self.content = [type("B", (), {"type": "text", "text": text})()]


def test_generator_sends_only_the_idea_and_returns_code() -> None:
    from helm.research.sandbox.generate import MODEL, GenerationError, generate_draft

    seen: dict[str, object] = {}

    def fake_create(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp(f"Here you go:\n```python\n{GOOD}```\n")

    code = generate_draft("buy when the 10 bar SMA crosses the 30 bar SMA", create=fake_create)
    assert "class SmaCross(BaseStrategy)" in code
    assert seen["model"] == MODEL == "claude-opus-5-5"
    assert seen["output_config"] == {"effort": "high"} and seen["fallbacks"] == "default"
    msgs = seen["messages"]
    assert isinstance(msgs, list) and len(msgs) == 1
    assert msgs[0]["content"].endswith("buy when the 10 bar SMA crosses the 30 bar SMA")
    for leak in ("equity", "position", "account", "journal", "HELM_SECRET"):
        assert leak not in msgs[0]["content"]  # no account/position data ever (RES-5)
    with pytest.raises(GenerationError, match="declined"):
        generate_draft("x", create=lambda **_: _Resp("", stop="refusal"))
    with pytest.raises(GenerationError, match="python block"):
        generate_draft("x", create=lambda **_: _Resp("no code here"))
    with pytest.raises(GenerationError, match="empty"):
        generate_draft("  ")


def test_draft_cli_from_file_and_from_idea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from helm.cli import main
    from helm.research.sandbox import generate

    f = tmp_path / "d.py"
    f.write_text(GOOD)
    assert main(["research", "draft", "--file", str(f), "--drafts-dir", str(tmp_path / "dr")]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    evil = strat("        pass", "import os")
    monkeypatch.setattr(generate, "generate_draft", lambda idea: evil)
    assert main(["research", "draft", "--idea", "x", "--drafts-dir", str(tmp_path / "dr")]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["static"] and rep["sandbox"] == {}  # generated code is untrusted: never ran
    assert main(["research", "draft"]) == 2
