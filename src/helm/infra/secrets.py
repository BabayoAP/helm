"""Secret access (SEC-2). Only the `engine` process may import this module.

Secrets are decrypted (SOPS/age, or the OS keyring in local dev) into process
memory at engine start. M0 ships the interface plus an environment-backed store
for tests and container secrets mounted via tmpfs; plaintext `.env` files are
never read. Every loaded value is registered for log redaction (OPS-4).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from helm.core.errors import ConfigError
from helm.infra.logging import SECRETS


class SecretStore(Protocol):
    def get(self, name: str) -> str: ...


class EnvSecretStore:
    """Reads `HELM_SECRET_<NAME>` from the process environment."""

    prefix = "HELM_SECRET_"

    def get(self, name: str) -> str:
        key = self.prefix + name.upper()
        value = os.environ.get(key)
        if not value:
            raise ConfigError(f"secret {name!r} not set (expected env {key})")
        SECRETS.register(value)
        return value


class FileSecretStore:
    """Reads one-secret-per-file from a tmpfs mount (Docker secrets: /run/secrets)."""

    def __init__(self, root: Path | str = "/run/secrets") -> None:
        self.root = Path(root)

    def get(self, name: str) -> str:
        path = self.root / name
        if path.name.startswith(".env") or path.suffix == ".env":
            raise ConfigError("plaintext .env files are not a secret store (SEC-2)")
        if not path.is_file():
            raise ConfigError(f"secret {name!r} not found under {self.root}")
        value = path.read_text(encoding="utf-8").strip()
        SECRETS.register(value)
        return value
