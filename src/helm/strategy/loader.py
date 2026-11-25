"""Resolve "module:Class" strategy paths. Only helm.strategy.* modules are allowed."""

from __future__ import annotations

import importlib

from helm.strategy.api import Strategy

ALLOWED_PREFIXES = ("helm.strategy.",)


def load_strategy(path: str, strategy_id: str | None = None) -> Strategy:
    module_name, _, cls_name = path.partition(":")
    if not cls_name or not module_name.startswith(ALLOWED_PREFIXES):
        raise ValueError(f"strategy path must be 'helm.strategy.<module>:<Class>', got {path!r}")
    cls = getattr(importlib.import_module(module_name), cls_name)
    strat = cls(strategy_id) if strategy_id else cls()
    if not isinstance(strat, Strategy):
        raise TypeError(f"{path} does not implement the Strategy protocol")
    return strat
