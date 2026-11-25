"""Config loading from TOML (Appendix A). Secrets are never read from config files."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from helm.core.config import HelmConfig, PromotionConfig, RiskConfig, SecurityConfig
from helm.core.errors import ConfigError

_FORBIDDEN_KEYS = ("api_key", "secret", "password", "private_key", "token")


def _check_no_secrets(data: dict[str, Any], path: str = "") -> None:
    for k, v in data.items():
        here = f"{path}.{k}" if path else k
        if any(f in k.lower() for f in _FORBIDDEN_KEYS):
            raise ConfigError(
                f"config key {here!r} looks like a secret; use the secrets store (SEC-2)"
            )
        if isinstance(v, dict):
            _check_no_secrets(v, here)  # pyright: ignore[reportUnknownArgumentType]


def load_toml(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    _check_no_secrets(data)
    return data


def load_config(
    helm_path: Path | str | None = None,
    risk_path: Path | str | None = None,
    promotion_path: Path | str | None = None,
) -> HelmConfig:
    merged: dict[str, Any] = {}
    if helm_path:
        merged.update(load_toml(helm_path))
    if risk_path:
        r = load_toml(risk_path)
        merged["risk"] = r.get("risk", {})
        if "security" in r:
            merged["security"] = r["security"]
    if promotion_path:
        merged["promotion"] = load_toml(promotion_path).get("promotion", {})
    try:
        return HelmConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


__all__ = [
    "HelmConfig",
    "PromotionConfig",
    "RiskConfig",
    "SecurityConfig",
    "load_config",
    "load_toml",
]
