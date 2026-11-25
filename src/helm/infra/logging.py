"""Structured JSON logging with secret redaction (OPS-4, SEC-2, I8)."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any, TextIO

import structlog

REDACTED = "***REDACTED***"

_SENSITIVE_KEYS = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|private[_-]?key|seed)", re.I
)


class SecretRegistry:
    """Holds the literal secret values loaded at runtime so they can be scrubbed."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        if value and len(value) >= 4:
            self._values.add(value)

    def scrub(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            if v in text:
                text = text.replace(v, REDACTED)
        return text

    def clear(self) -> None:
        self._values.clear()


SECRETS = SecretRegistry()


def _scrub_value(value: Any, registry: SecretRegistry) -> Any:
    if isinstance(value, str):
        return registry.scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_kv(k, v, registry) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return type(value)(_scrub_value(v, registry) for v in value)  # pyright: ignore[reportUnknownArgumentType,reportUnknownVariableType]
    return value


def _scrub_kv(key: object, value: Any, registry: SecretRegistry) -> Any:
    if isinstance(key, str) and _SENSITIVE_KEYS.search(key) and value not in (None, "", False):
        return REDACTED
    return _scrub_value(value, registry)


def redaction_processor(registry: SecretRegistry = SECRETS) -> structlog.types.Processor:
    def _proc(
        _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for k in list(event_dict.keys()):
            event_dict[k] = _scrub_kv(k, event_dict[k], registry)
        return event_dict

    return _proc


def configure_logging(
    *,
    level: str = "INFO",
    mode_badge: str | None = None,
    stream: TextIO | None = None,
    extra_processors: Iterable[structlog.types.Processor] = (),
) -> None:
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        *extra_processors,
    ]
    if mode_badge:

        def _badge(_l: Any, _m: str, ed: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
            ed.setdefault("mode", mode_badge)
            return ed

        processors.append(_badge)
    processors += [
        structlog.processors.format_exc_info,
        redaction_processor(),
        structlog.processors.JSONRenderer(sort_keys=True),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(**bind: Any) -> Any:
    return structlog.get_logger().bind(**bind)
