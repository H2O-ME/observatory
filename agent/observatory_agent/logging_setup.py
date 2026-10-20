"""Logging for both the service and per-session workers.

The service and each worker write their own file so a hung user session cannot
hide service-level failures. Rotation is size based because a long-running agent
on an unattended machine must never fill the disk with logs.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LOG_DIR, ensure_dirs

FORMAT = "%(asctime)s %(levelname)-7s [%(process)d:%(threadName)s] %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(role: str = "agent", *, level: int = logging.INFO, console: bool = False) -> Path:
    ensure_dirs()
    path = Path(LOG_DIR) / f"{role}.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass

    if console or not getattr(sys, "frozen", False):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return path


def read_tail(lines: int = 200, role: str = "agent") -> list[str]:
    path = Path(LOG_DIR) / f"{role}.log"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, 256 * 1024)
            handle.seek(size - block)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-max(1, lines) :]


__all__ = ["BACKUP_COUNT", "FORMAT", "MAX_BYTES", "read_tail", "setup_logging"]