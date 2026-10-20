"""Storage layout for captured frames.

A frame's media type is derived from its path suffix rather than stored in a
column, so historical ``.jpg`` files keep working next to the ``.webp`` files this
version writes. Paths stored in the database are always relative to ``data_dir``
so the data volume can be moved without rewriting rows.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

SAFE_UID = re.compile(r"[^A-Za-z0-9_.-]")
MEDIA_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class StorageError(ValueError):
    pass


def safe_uid(uid: str) -> str:
    cleaned = SAFE_UID.sub("", uid or "")
    if not cleaned or cleaned in {".", ".."}:
        raise StorageError(f"非法的设备标识: {uid!r}")
    return cleaned[:64]


def _day_parts(ts_ms: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H")


def relative_frame_path(uid: str, ts_ms: int, *, ext: str = "webp", kind: str = "screen") -> str:
    day, hour = _day_parts(ts_ms)
    stem = f"{ts_ms}"
    if kind != "screen":
        stem = f"{kind}-{ts_ms}"
    return f"frames/{safe_uid(uid)}/{day}/{hour}/{stem}.{ext.lstrip('.')}"


def relative_thumb_path(uid: str, ts_ms: int, *, ext: str = "webp", kind: str = "screen") -> str:
    day, hour = _day_parts(ts_ms)
    stem = f"{ts_ms}"
    if kind != "screen":
        stem = f"{kind}-{ts_ms}"
    return f"thumbs/{safe_uid(uid)}/{day}/{hour}/{stem}.{ext.lstrip('.')}"


def absolute(settings, relative: str) -> Path:
    """Resolve a stored relative path, refusing anything outside the data dir."""

    root = Path(settings.data_dir).resolve()
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root) + os.sep) and candidate != root:
        raise StorageError(f"路径越界: {relative}")
    return candidate


def media_type_for(path: str | Path) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frame_stat(settings, relative: str) -> tuple[int, int]:
    """Return ``(size_bytes, mtime_ms)`` for a stored frame."""

    path = absolute(settings, relative)
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    return stat.st_size, int(stat.st_mtime * 1000)


def delete_path(path: Path) -> int:
    """Delete a file if present; returns bytes freed (best effort)."""

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    try:
        path.unlink()
        return size
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


def device_frame_dir(settings, uid: str) -> Path:
    return Path(settings.frames_dir) / safe_uid(uid)


def device_thumb_dir(settings, uid: str) -> Path:
    return Path(settings.thumbs_dir) / safe_uid(uid)


def directory_size_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "MEDIA_TYPES",
    "StorageError",
    "absolute",
    "delete_path",
    "device_frame_dir",
    "device_thumb_dir",
    "directory_size_bytes",
    "frame_stat",
    "media_type_for",
    "now_ms",
    "relative_frame_path",
    "relative_thumb_path",
    "safe_uid",
    "sha256_of",
    "write_atomic",
]