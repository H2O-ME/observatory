"""Structured logging and secret redaction.

Rules enforced here (C6/A22):
* no ``except: pass`` in the code base — call sites use ``contextlib.suppress``
  only with a written reason, or log through :func:`log_event`;
* data that may contain credentials is redacted before it reaches a log record.
"""

from __future__ import annotations

import logging
from typing import Any

#: Keys whose values must never appear in a log line or an error payload.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "secret_key",
        "admin_password",
        "password",
        "current_password",
        "new_password",
        "archive_token",
        "token",
        "device_token",
        "install_token",
        "totp_secret",
        "authorization",
        "cookie",
    }
)
REDACTED = "***redacted***"

_HANDLER_MARK = "_observatory_handler"


def redact(value: Any, *, depth: int = 0) -> Any:
    """Recursively mask secret-looking keys in mappings and sequences."""

    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                result[key] = REDACTED
            else:
                result[key] = redact(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in value]
    return value


def configure_logging(level: str = "INFO") -> None:
    """Install one stderr handler; safe to call from every ``create_app``."""

    numeric = getattr(logging, (level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    if any(getattr(handler, _HANDLER_MARK, False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log a structured event with secrets redacted.

    ``event`` is a stable dotted name (``link.session.registered``); fields are
    rendered as ``key=value`` pairs so ``grep`` stays useful.
    """

    if not logger.isEnabledFor(level):
        return
    safe = redact(fields)
    rendered = " ".join(f"{key}={value!r}" for key, value in safe.items())
    logger.log(level, "%s %s", event, rendered)


__all__ = [
    "REDACTED",
    "SECRET_KEYS",
    "configure_logging",
    "get_logger",
    "log_event",
    "redact",
]