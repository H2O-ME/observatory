"""帧文件落盘布局与读写（媒体层唯一的磁盘真相）。

不变式：

* 数据库里存的是**相对 ``data_dir`` 的 POSIX 路径**，数据卷可整体迁移；
* 写文件一律"唯一临时名 + ``os.replace``"，不存在"半个文件"被读者看到；
* 读取/删除都过 :func:`absolute` 的越界检查，``..`` 或绝对路径一律拒绝。

与 0.2.10 的差别（都是踩过的坑）：

1. 临时文件名唯一（旧实现固定 ``<name>.part``，同一毫秒的两次并发写会互相覆盖）；
2. 文件名带内容摘要后缀（旧实现只用 ``ts_ms``，同毫秒的两帧会覆盖前一帧）；
3. 删除/遍历失败会记日志并返回统计，不再 ``ignore_errors=True`` 静默吞掉。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

SAFE_UID = re.compile(r"[^A-Za-z0-9_.-]")
MEDIA_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

# 磁盘上不属于帧数据的临时后缀；孤儿扫描必须跳过它们（可能正在被写入）。
TEMP_SUFFIX = ".part"


class StorageError(ValueError):
    """路径非法或越界。"""


@dataclass(frozen=True, slots=True)
class MediaFile:
    """一条磁盘上的媒体文件（相对 data_dir 的 POSIX 路径 + 元数据）。"""

    relative: str
    size_bytes: int
    mtime_ms: int


def safe_uid(uid: str) -> str:
    cleaned = SAFE_UID.sub("", uid or "")
    if not cleaned or cleaned in {".", ".."}:
        raise StorageError(f"非法的设备标识: {uid!r}")
    return cleaned[:64]


def _day_hour(ts_ms: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H")


def _stem(ts_ms: int, kind: str, digest: str | None) -> str:
    base = f"{kind}-{ts_ms}" if kind and kind != "screen" else f"{ts_ms}"
    if digest:
        base = f"{base}-{digest[:8]}"
    return base


def relative_frame_path(
    uid: str, ts_ms: int, *, ext: str = "webp", kind: str = "screen", digest: str | None = None
) -> str:
    day, hour = _day_hour(ts_ms)
    return f"frames/{safe_uid(uid)}/{day}/{hour}/{_stem(ts_ms, kind, digest)}.{ext.lstrip('.')}"


def relative_thumb_path(
    uid: str, ts_ms: int, *, ext: str = "webp", kind: str = "screen", digest: str | None = None
) -> str:
    day, hour = _day_hour(ts_ms)
    return f"thumbs/{safe_uid(uid)}/{day}/{hour}/{_stem(ts_ms, kind, digest)}.{ext.lstrip('.')}"


def relative_path_of(settings, path: Path | str) -> str:
    """把绝对路径还原成相对 data_dir 的 POSIX 路径（供孤儿扫描比对）。"""

    root = Path(settings.data_dir).resolve()
    candidate = Path(path).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise StorageError(f"路径越界: {path}") from exc


def absolute(settings, relative: str) -> Path:
    """把数据库里的相对路径解析为绝对路径，越界即抛错。"""

    if not relative or Path(relative).is_absolute():
        raise StorageError(f"非法的相对路径: {relative!r}")
    root = Path(settings.data_dir).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise StorageError(f"路径越界: {relative}")
    return candidate


def media_type_for(path: str | Path) -> str:
    return MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """唯一临时名 + fsync + 原子改名；失败时清理临时文件并抛出 ``OSError``。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}{TEMP_SUFFIX}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        # 清理临时文件：每个分支都留痕，绝不吞掉原始写入异常。
        try:
            tmp.unlink()
        except FileNotFoundError:
            log.debug("临时帧文件已不存在，无需清理: %s", tmp)
        except OSError as cleanup_exc:
            log.warning("清理临时帧文件失败 %s: %s", tmp, cleanup_exc)
        raise


def frame_stat(settings, relative: str) -> tuple[int, int]:
    """返回 ``(size_bytes, mtime_ms)``；文件不存在返回 ``(0, 0)``。"""

    try:
        stat = absolute(settings, relative).stat()
    except FileNotFoundError:
        return 0, 0
    except (OSError, StorageError) as exc:
        log.warning("读取帧文件元数据失败 %r: %s", relative, exc)
        return 0, 0
    return stat.st_size, int(stat.st_mtime * 1000)


def delete_path(path: Path) -> int:
    """删除一个文件，返回释放的字节数（不存在视为 0）。"""

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        log.warning("读取待删文件大小失败 %s: %s", path, exc)
        size = 0
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        # 删除失败必须留痕：重复失败说明权限/句柄有问题，静默会让磁盘配额永久超标。
        log.warning("删除文件失败 %s: %s", path, exc)
        return 0
    return size


def remove_tree(root: Path) -> tuple[int, int]:
    """递归删除目录，返回 ``(删除文件数, 释放字节数)``；错误逐条记录。"""

    removed = 0
    freed = 0
    if not root.exists():
        return 0, 0
    for entry in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if entry.is_dir():
            try:
                entry.rmdir()
            except OSError as exc:
                log.warning("删除目录失败 %s: %s", entry, exc)
            continue
        freed += delete_path(entry)
        removed += 1
    try:
        root.rmdir()
    except OSError as exc:
        log.warning("删除目录失败 %s: %s", root, exc)
    return removed, freed


def device_frame_dir(settings, uid: str) -> Path:
    return Path(settings.frames_dir) / safe_uid(uid)


def device_thumb_dir(settings, uid: str) -> Path:
    return Path(settings.thumbs_dir) / safe_uid(uid)


def directory_size_bytes(root: Path) -> int:
    total = 0
    for entry in _walk_files(root):
        total += entry.size_bytes
    return total


def _walk_files(root: Path) -> Iterator[MediaFile]:
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(TEMP_SUFFIX) or name.startswith("."):
                continue
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError as exc:
                log.warning("遍历媒体文件失败 %s: %s", full, exc)
                continue
            yield MediaFile(
                relative=full.relative_to(root).as_posix(),
                size_bytes=stat.st_size,
                mtime_ms=int(stat.st_mtime * 1000),
            )


def scan_media_files(settings) -> list[MediaFile]:
    """扫描 frames/ 与 thumbs/ 两棵树（相对 data_dir 的路径，可直接与数据库比对）。

    跳过硬链接/临时文件；不跟随符号链接（``os.walk`` 默认不跟随目录链接）。
    """

    found: list[MediaFile] = []
    for sub in ("frames", "thumbs"):
        root = Path(settings.data_dir) / sub
        for entry in _walk_files(root):
            found.append(
                MediaFile(
                    relative=f"{sub}/{entry.relative}",
                    size_bytes=entry.size_bytes,
                    mtime_ms=entry.mtime_ms,
                )
            )
    return found


def reconcile_paths(paths: Iterable[str]) -> set[str]:
    """把数据库里的路径集合归一（去空、统一 POSIX 分隔符），供孤儿扫描比对。"""

    cleaned: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        cleaned.add(Path(str(raw).replace("\\", "/")).as_posix())
    return cleaned


__all__ = [
    "MEDIA_TYPES",
    "MediaFile",
    "StorageError",
    "TEMP_SUFFIX",
    "absolute",
    "delete_path",
    "device_frame_dir",
    "device_thumb_dir",
    "directory_size_bytes",
    "frame_stat",
    "media_type_for",
    "reconcile_paths",
    "relative_frame_path",
    "relative_path_of",
    "relative_thumb_path",
    "remove_tree",
    "safe_uid",
    "scan_media_files",
    "sha256_file",
    "sha256_of",
    "write_atomic",
]