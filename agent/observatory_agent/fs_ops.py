"""File operations executed in the user's session.

Full Explorer-equivalent surface: drives, list, stat, mkdir, delete, rename, copy,
move, search, chunked read, chunked write and hashing. Errors are translated into
the same wording Windows itself would use, because "操作失败" tells an operator
nothing while "拒绝访问 (5)" immediately explains the problem.

Never trust a client-supplied path: it is expanded and used as-is, but every
operation is confined to what the *worker's* account can already reach. The console
cannot escalate through this interface.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import logging
import os
import shutil
import stat as stat_module
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_LIST_ENTRIES = 5000
MAX_SEARCH_RESULTS = 500
MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 4 * 1024 * 1024
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

ERRORS = {
    2: "找不到文件",
    3: "找不到路径",
    5: "拒绝访问",
    32: "文件正被占用",
    80: "文件已存在",
    112: "磁盘空间不足",
    123: "文件名不合法",
    206: "文件名或扩展名过长",
    267: "目录名无效",
}


class FileOpError(RuntimeError):
    pass


def _describe(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror and winerror in ERRORS:
        return f"{ERRORS[winerror]}（{winerror}）"
    if isinstance(exc, PermissionError):
        return "拒绝访问（5）"
    if isinstance(exc, FileNotFoundError):
        return "找不到文件或路径（2）"
    if isinstance(exc, FileExistsError):
        return "文件已存在（80）"
    if isinstance(exc, OSError) and exc.errno in ERRORS:
        return f"{ERRORS[exc.errno]}（{exc.errno}）"
    return str(exc) or exc.__class__.__name__


def _resolve(raw: str) -> Path:
    text = (raw or "").strip().strip('"')
    if not text:
        raise FileOpError("缺少路径")
    expanded = os.path.expandvars(os.path.expanduser(text))
    return Path(expanded)


def _mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


def _is_hidden(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def _entry(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
        is_dir = path.is_dir()
        size = 0 if is_dir else int(info.st_size)
    except OSError:
        is_dir = False
        size = 0
    return {
        "name": path.name or str(path),
        "path": str(path),
        "is_dir": is_dir,
        "size": size,
        "mtime_ms": _mtime_ms(path),
        "hidden": _is_hidden(path),
        "drive": path.drive,
    }


# ----------------------------------------------------------------- drives ---
def drives() -> dict[str, Any]:
    import ctypes

    items: list[dict[str, Any]] = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001
        mask = 0
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        root = f"{chr(65 + index)}:\\"
        drive_type = 0
        try:
            drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        except Exception:  # noqa: BLE001
            pass
        if drive_type == 1:  # DRIVE_NO_ROOT_DIR
            continue
        label, filesystem = _volume_info(ctypes, root)
        total = free = 0
        try:
            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(free_bytes), ctypes.byref(total_bytes), None
            )
            total = int(total_bytes.value)
            free = int(free_bytes.value)
        except Exception:  # noqa: BLE001
            pass
        type_name = {2: "可移动磁盘", 3: "本地磁盘", 4: "网络驱动器", 5: "光驱", 6: "RAM 磁盘"}.get(
            drive_type, "驱动器"
        )
        items.append(
            {
                "name": f"{label or type_name} ({root[:2]})",
                "path": root,
                "is_dir": True,
                "size": total,
                "mtime_ms": 0,
                "hidden": False,
                "drive": root[:2],
                "free": free,
                "filesystem": filesystem,
            }
        )
    return {"drives": items, "server_time_ms": 0}


def _volume_info(ctypes, root: str) -> tuple[str, str]:
    try:
        label_buffer = ctypes.create_unicode_buffer(261)
        fs_buffer = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            label_buffer,
            ctypes.sizeof(label_buffer),
            None,
            None,
            None,
            fs_buffer,
            ctypes.sizeof(fs_buffer),
        )
        return label_buffer.value, fs_buffer.value
    except Exception:  # noqa: BLE001
        return "", ""


# ------------------------------------------------------------------- ops ---
def list_dir(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileOpError(f"找不到路径（3）：{path}")
    if not path.is_dir():
        raise FileOpError(f"不是目录：{path}")

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(path) as iterator:
            for item in iterator:
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                try:
                    is_dir = item.is_dir()
                    size = 0 if is_dir else int(item.stat().st_size)
                except OSError:
                    is_dir = False
                    size = 0
                attributes = 0
                try:
                    attributes = getattr(item.stat(), "st_file_attributes", 0)
                except OSError:
                    attributes = 0
                entries.append(
                    {
                        "name": item.name,
                        "path": item.path,
                        "is_dir": is_dir,
                        "size": size,
                        "mtime_ms": int(item.stat().st_mtime * 1000) if not is_dir else int(item.stat().st_mtime * 1000),
                        "hidden": bool(attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)),
                        "drive": path.drive,
                    }
                )
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc

    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    parent = str(path.parent) if path.parent != path else None
    return {
        "path": str(path),
        "parent": parent,
        "entries": entries,
        "truncated": truncated,
        "drives": None,
    }


def stat_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileOpError(f"找不到文件或路径（2）：{path}")
    entry = _entry(path)
    try:
        info = path.stat()
        entry["attributes"] = int(getattr(info, "st_file_attributes", 0))
        entry["created_ms"] = int(info.st_ctime * 1000)
        entry["readonly"] = not os.access(path, os.W_OK)
    except OSError:
        pass
    return entry


def mkdir(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileOpError(f"已存在：{path}") from exc
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path)}


def delete(path: Path, recursive: bool) -> dict[str, Any]:
    if not path.exists():
        raise FileOpError(f"找不到文件或路径（2）：{path}")
    try:
        if path.is_dir() and not path.is_symlink():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path)}


def rename(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        path.rename(destination)
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "target": str(destination)}


def copy(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        if path.is_dir():
            shutil.copytree(path, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "target": str(destination)}


def move(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "target": str(destination)}


def search(path: Path, pattern: str, recursive: bool) -> dict[str, Any]:
    needle = (pattern or "*").strip("*") or "*"
    results: list[dict[str, Any]] = []
    truncated = False
    base_depth = len(path.parts)
    try:
        for root, dirnames, filenames in os.walk(path):
            if not recursive and len(Path(root).parts) - base_depth >= 1:
                dirnames[:] = []
            for name in filenames + dirnames:
                if not fnmatch.fnmatch(name.lower(), f"*{needle.lower()}*"):
                    continue
                candidate = Path(root) / name
                results.append(_entry(candidate))
                if len(results) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    break
            if truncated:
                break
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "entries": results, "truncated": truncated}


def read_chunk(path: Path, offset: int, length: int) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise FileOpError(f"找不到文件（2）：{path}")
    size = path.stat().st_size
    want = length if length > 0 else MAX_READ_BYTES
    want = max(1, min(want, MAX_READ_BYTES))
    start = max(0, min(offset, size))
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(want)
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {
        "offset": start,
        "length": len(data),
        "size": size,
        "eof": start + len(data) >= size,
        "data": base64.b64encode(data).decode("ascii"),
    }


def write_chunk(path: Path, offset: int, data: bytes, truncate: bool, allow_write: bool) -> dict[str, Any]:
    if not allow_write:
        raise FileOpError("服务器已禁用文件写入")
    if len(data) > MAX_WRITE_BYTES:
        raise FileOpError("单个分片超过 4MiB")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if (truncate or offset == 0) else "r+b"
        with open(path, mode) as handle:
            if offset > 0:
                handle.seek(offset)
            handle.write(data)
            handle.flush()
        size = path.stat().st_size
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "offset": offset, "written": len(data), "size": size}


def hash_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileOpError(f"找不到文件（2）：{path}")
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FileOpError(_describe(exc)) from exc
    return {"path": str(path), "sha256": digest.hexdigest(), "size": path.stat().st_size}


def perform(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one operation and return the ``FileOpResult`` payload."""

    allow_write = bool(payload.get("allow_write", True))
    started = _now_ms()

    if op == "drives":
        data = drives()
    elif op == "list":
        data = list_dir(_resolve(payload.get("path", "")))
    elif op == "stat":
        data = stat_path(_resolve(payload.get("path", "")))
    elif op == "mkdir":
        data = mkdir(_resolve(payload.get("path", "")))
    elif op == "delete":
        data = delete(_resolve(payload.get("path", "")), bool(payload.get("recursive")))
    elif op == "rename":
        data = rename(_resolve(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "copy":
        data = copy(_resolve(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "move":
        data = move(_resolve(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "search":
        data = search(
            _resolve(payload.get("path", "")), str(payload.get("pattern") or ""), bool(payload.get("recursive"))
        )
    elif op == "read":
        data = read_chunk(
            _resolve(payload.get("path", "")), int(payload.get("offset") or 0), int(payload.get("length") or 0)
        )
    elif op == "write":
        raw = payload.get("data") or ""
        try:
            payload_bytes = base64.b64decode(raw)
        except Exception as exc:  # noqa: BLE001
            raise FileOpError("写入数据无法解码") from exc
        data = write_chunk(
            _resolve(payload.get("path", "")),
            int(payload.get("offset") or 0),
            payload_bytes,
            bool(payload.get("truncate")),
            allow_write,
        )
    elif op == "hash":
        data = hash_file(_resolve(payload.get("path", "")))
    else:
        raise FileOpError(f"不支持的操作: {op}")

    data["server_time_ms"] = _now_ms()
    return {"ok": True, "op": op, "data": data, "error": None, "elapsed_ms": _now_ms() - started}


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


_ = (stat_module,)

__all__ = [
    "FileOpError",
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_SEARCH_RESULTS",
    "MAX_WRITE_BYTES",
    "drives",
    "perform",
]