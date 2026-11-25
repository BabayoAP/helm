"""Encrypted backups of journals and account state (OPS-6, decision 0028).

`backup()` snapshots every `accounts/*/journal.sqlite` with SQLite's online
backup API (consistent under WAL, engine keeps running), adds the JSONL
mirrors and small JSON state files, tars them and encrypts the archive to an
`age` recipient public key. Only the public key lives on the server.

`restore()` decrypts with an identity file and verifies each journal:
`PRAGMA integrity_check` is ok and the append-only triggers are present.
"""

from __future__ import annotations

import io
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from helm.core.errors import HelmError


class BackupError(HelmError):
    pass


def age_binary() -> str:
    path = shutil.which("age")
    if path is None:
        raise BackupError("the `age` CLI is required for encrypted backups (brew/apt install age)")
    return path


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _archive(data_dir: Path) -> bytes:
    accounts = data_dir / "accounts"
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for acct in sorted(p for p in accounts.glob("*") if p.is_dir()):
            for f in sorted(acct.iterdir()):
                arc = f"accounts/{acct.name}/{f.name}"
                if f.suffix == ".sqlite":
                    snap = Path(tmp) / f"{acct.name}-{f.name}"
                    _snapshot_sqlite(f, snap)
                    tar.add(snap, arcname=arc)
                elif f.suffix in (".jsonl", ".json"):
                    tar.add(f, arcname=arc)
    return buf.getvalue()


def backup(data_dir: Path, out_dir: Path, recipient: str, *, keep: int = 14) -> Path:
    if not recipient.startswith("age1"):
        raise BackupError("recipient must be an age public key (age1...)")
    if not (data_dir / "accounts").is_dir():
        raise BackupError(f"nothing to back up under {data_dir / 'accounts'}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"helm-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.tar.gz.age"
    proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
        [age_binary(), "-r", recipient, "-o", str(out)],
        input=_archive(data_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise BackupError(f"age encryption failed: {proc.stderr.decode()[:200]}")
    old = sorted(out_dir.glob("helm-*.tar.gz.age"))
    for stale in old[:-keep] if keep > 0 else []:
        stale.unlink()
    return out


@dataclass
class RestoreReport:
    journals: list[str] = field(default_factory=lambda: [])
    entries: dict[str, int] = field(default_factory=lambda: {})


def verify_journal(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"{path}: integrity_check failed")
        triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if not {"journal_no_update", "journal_no_delete"} <= triggers:
            raise BackupError(f"{path}: append-only triggers missing")
        return int(c.execute("SELECT COUNT(*) FROM journal").fetchone()[0])


def restore(archive: Path, identity: Path, dest: Path) -> RestoreReport:
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"restore target {dest} is not empty; refusing to overwrite")
    proc = subprocess.run(  # noqa: S603
        [age_binary(), "-d", "-i", str(identity), str(archive)], capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise BackupError(f"age decryption failed: {proc.stderr.decode()[:200]}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    rep = RestoreReport()
    for j in sorted(dest.glob("accounts/*/journal.sqlite")):
        rep.journals.append(str(j.relative_to(dest)))
        rep.entries[j.parent.name] = verify_journal(j)
    return rep
