"""OPS-6 restore drill: back up a live journal (WAL, engine still writing), restore it
from the encrypted archive, verify integrity and append-only triggers (decision 0028)."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from helm.core.enums import EngineState
from helm.core.events import EngineStateChanged
from helm.infra.backup import BackupError, backup, restore, verify_journal
from helm.oms.journal import Journal

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age CLI not installed")


def _keypair(tmp: Path) -> tuple[str, Path]:
    ident = tmp / "key.txt"
    subprocess.run(["age-keygen", "-o", str(ident)], check=True, capture_output=True)
    pub = next(
        line.split(": ", 1)[1]
        for line in ident.read_text().splitlines()
        if line.startswith("# public key:")
    )
    return pub, ident


def test_backup_and_restore_drill(tmp_path: Path) -> None:
    data = tmp_path / "data"
    j = Journal(data / "accounts" / "paper-main" / "journal.sqlite")
    for i in range(50):
        ev = EngineStateChanged(
            previous=EngineState.RUNNING, current=EngineState.PAUSED, reason=f"r{i}", ts=i
        )
        j.append(ev, ts=i, run_id="r1", account_id="paper-main", key="engine")
    (data / "accounts" / "paper-main" / "starting_equity.json").write_text('{"x": "1"}')
    pub, ident = _keypair(tmp_path)

    out = backup(data, tmp_path / "backups", pub)  # journal still open in WAL mode
    assert out.suffix == ".age" and b"journal" not in out.read_bytes()  # encrypted
    j.close()

    rep = restore(out, ident, tmp_path / "restored")
    assert rep.entries == {"paper-main": 50}
    assert (tmp_path / "restored/accounts/paper-main/starting_equity.json").exists()
    restored = tmp_path / "restored/accounts/paper-main/journal.sqlite"
    with sqlite3.connect(restored) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM journal")  # still append-only after restore

    with pytest.raises(BackupError, match="not empty"):
        restore(out, ident, tmp_path / "restored")
    (tmp_path / "other").mkdir()
    other_pub, other_ident = _keypair(tmp_path / "other")
    assert other_pub != pub
    with pytest.raises(BackupError, match="decryption failed"):
        restore(out, other_ident, tmp_path / "wrong-key")
    with pytest.raises(BackupError, match="age public key"):
        backup(data, tmp_path / "backups", "not-a-key")
    with pytest.raises(BackupError, match="nothing to back up"):
        backup(tmp_path / "empty", tmp_path / "backups", pub)


def test_retention_and_verify_detects_missing_triggers(tmp_path: Path) -> None:
    data = tmp_path / "data"
    Journal(data / "accounts" / "a" / "journal.sqlite", mirror=False).close()
    pub, _ = _keypair(tmp_path)
    backup(data, tmp_path / "b", pub, keep=2)
    for i in range(3):
        (tmp_path / "b" / f"helm-2000010{i}-000000.tar.gz.age").write_bytes(b"old")
    backup(data, tmp_path / "b", pub, keep=2)
    assert len(list((tmp_path / "b").glob("*.age"))) == 2  # oldest pruned
    bad = tmp_path / "bad.sqlite"
    with sqlite3.connect(bad) as c:
        c.execute("CREATE TABLE journal (seq INTEGER)")
    with pytest.raises(BackupError, match="triggers missing"):
        verify_journal(bad)
