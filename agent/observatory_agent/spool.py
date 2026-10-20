"""Disk-backed frame spool.

Frames are produced faster than a flaky uplink can carry them, and losing a frame
because the network blinked is unacceptable for a monitoring product. The spool is
a bounded directory of self-describing files: the service appends here when an
upload fails and drains it in the background, oldest first.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .pipe_proto import decode, encode

log = logging.getLogger(__name__)

MAX_SPOOL_FILES = 2000
MAX_AGE_S = 48 * 3600


class FrameSpool:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counter = 0

    def put(self, header: dict, payload: bytes) -> bool:
        with self._lock:
            self._counter += 1
            ts = int(header.get("ts") or time.time() * 1000)
            name = f"{ts:013d}-{self._counter:06d}.frame"
            path = self.root / name
            try:
                path.write_bytes(encode(header, payload))
                self._trim()
                return True
            except OSError as exc:
                log.warning("spool write failed: %s", exc)
                return False

    def pending(self) -> int:
        try:
            return sum(1 for _ in self.root.glob("*.frame"))
        except OSError:
            return 0

    def pop_batch(self, limit: int = 4) -> list[tuple[dict, bytes, Path]]:
        items: list[tuple[dict, bytes, Path]] = []
        try:
            paths = sorted(self.root.glob("*.frame"))[: max(1, limit)]
        except OSError:
            return items
        for path in paths:
            try:
                header, payload = decode(path.read_bytes())
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            items.append((header, payload, path))
        return items

    def ack(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def drop(self, path: Path) -> None:
        self.ack(path)

    def cleanup(self, max_age_s: int = MAX_AGE_S) -> int:
        removed = 0
        cutoff = time.time() - max_age_s
        try:
            for path in self.root.glob("*.frame"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            return removed
        return removed

    def _trim(self) -> None:
        try:
            paths = sorted(self.root.glob("*.frame"))
        except OSError:
            return
        if len(paths) <= MAX_SPOOL_FILES:
            return
        for path in paths[: len(paths) - MAX_SPOOL_FILES]:
            try:
                os.unlink(path)
            except OSError:
                continue


__all__ = ["FrameSpool", "MAX_AGE_S", "MAX_SPOOL_FILES"]