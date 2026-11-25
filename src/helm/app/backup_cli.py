"""`helm backup` / `helm restore` (OPS-6, decision 0028)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_backup(a: argparse.Namespace) -> int:
    from helm.infra.backup import BackupError, backup

    try:
        out = backup(Path(a.data_dir), Path(a.out_dir), a.recipient, keep=a.keep)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(out)
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    from helm.infra.backup import BackupError, restore

    try:
        rep = restore(Path(a.archive), Path(a.identity), Path(a.to))
    except BackupError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"journals": rep.journals, "entries": rep.entries}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    b = sub.add_parser("backup", help="encrypted backup of journals + account state (OPS-6)")
    b.add_argument("--data-dir", default="data")
    b.add_argument("--out-dir", default="backups")
    b.add_argument("--recipient", required=True, help="age public key (age1...)")
    b.add_argument("--keep", type=int, default=14, help="keep the newest N archives")
    b.set_defaults(handler=cmd_backup)
    r = sub.add_parser("restore", help="decrypt + verify a backup into an empty directory")
    r.add_argument("archive")
    r.add_argument("--identity", required=True, help="age identity (private key) file")
    r.add_argument("--to", required=True, help="empty target directory")
    r.set_defaults(handler=cmd_restore)
