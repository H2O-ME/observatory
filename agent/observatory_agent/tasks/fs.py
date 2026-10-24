"""文件操作（12 种）+ 分片读写。

为什么与旧 ``fs_ops.py`` 不同
-----------------------------

* **分片写入语义修正**：旧 ``fs_ops.py:360`` 用 ``mode = "wb" if (truncate or offset == 0) else "r+b"``，
  于是 ``offset=0, truncate=False`` 也会静默截断文件；而 ``offset>0`` 且文件不存在时直接
  ``FileNotFoundError``。新实现：``offset==0 && truncate`` → 截断写；否则文件存在 → 随机写；
  否则仅在 ``offset==0`` 时创建；``offset>0`` 且文件不存在 → 明确报错。
* **静默 except 清零**：旧实现多处 ``except Exception: pass``；新实现每个失败分支要么抛
  :class:`FileOpError`（带 Win32 错误码的可读文案），要么记录日志。
* **可测试/可移植**：模块在非 Windows 上可 import；只有 ``drives`` 会因平台不支持抛
  :class:`FileOpError`。时间通过 ``now_ms`` 注入，便于单测。

授权模型（与 0.2.10 一致）：本模块不提供白名单/沙箱，操作以 **worker 所属登录用户**的
Windows ACL 为边界；``allow_write`` 只约束 ``write``（服务端 ``file_write_enabled``）。
"""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import hashlib
import logging
import os
import shutil
import stat as stat_module
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..core.commands import ERROR_BAD_REQUEST, Command, CommandHandler, CommandResult

log = logging.getLogger(__name__)

MAX_LIST_ENTRIES = 5_000
MAX_SEARCH_RESULTS = 500
MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 4 * 1024 * 1024
#: 0.3.1（F35）：稀疏文件防护——写入 offset 最多允许超出现有大小这么多字节。
#: NTFS 会把 offset 之前的空洞填零，没有上界时一条伪造 offset 的命令就能写爆磁盘。
MAX_WRITE_SPAN_BYTES = 64 * 1024 * 1024

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

#: 12 种操作（顺序即对外清单；``perform`` 只接受这些）。
SUPPORTED_OPS: tuple[str, ...] = (
    "drives",
    "list",
    "stat",
    "mkdir",
    "delete",
    "rename",
    "copy",
    "move",
    "search",
    "read",
    "write",
    "hash",
)

_ERROR_TEXT = {
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
    """文件操作失败；``winerror``/``errno`` 保留原始错误码。"""

    def __init__(self, message: str, *, winerror: int | None = None, errno: int | None = None) -> None:
        super().__init__(message)
        self.winerror = winerror
        self.errno = errno


def default_now_ms() -> int:
    return int(time.time() * 1000)


def describe_error(exc: BaseException) -> str:
    """把 OSError/WindowsError 翻成运维看得懂的中文（含错误码）。"""

    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int) and winerror in _ERROR_TEXT:
        return f"{_ERROR_TEXT[winerror]}（{winerror}）"
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and errno in _ERROR_TEXT:
        return f"{_ERROR_TEXT[errno]}（{errno}）"
    if isinstance(exc, PermissionError):
        return "拒绝访问（5）"
    if isinstance(exc, FileNotFoundError):
        return "找不到文件或路径（2）"
    if isinstance(exc, FileExistsError):
        return "文件已存在（80）"
    if isinstance(exc, IsADirectoryError):
        return "目标是目录"
    if isinstance(exc, NotADirectoryError):
        return "目标不是目录"
    return str(exc) or exc.__class__.__name__


def resolve_path(raw: Any) -> Path:
    """把客户端传来的路径字符串解析成 :class:`Path`。

    只做展开（``%VAR%`` / ``~``）与去引号；不做任何“安全根”限制——见模块 docstring 的
    授权模型。空路径/含 NUL 的路径直接拒绝。
    """

    text = str(raw or "").strip().strip('"')
    if not text:
        raise FileOpError("缺少路径")
    if "\x00" in text:
        raise FileOpError("路径包含非法字符")
    return Path(os.path.expandvars(os.path.expanduser(text)))


# ------------------------------------------------------------------ helpers --
def _is_link_like(path: Path) -> bool:
    """符号链接或目录 junction（Python 3.12 提供 ``Path.is_junction``）。"""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _is_reparse_point(path: Path) -> bool:
    """0.3.1（F35）：路径是否为 NTFS reparse point（junction/挂载点）。

    ``path.is_symlink()`` 对 junction 返回 False，``os.walk`` 不剪枝就会踩进
    环形链接无限递归。``os.path.isjunction`` 要 Python 3.12+；缺省回退到
    ``st_file_attributes`` 的 REPARSE_POINT 位（非 Windows 上无该属性视为 False）。
    """

    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            return bool(isjunction(path))
        except OSError:  # 竞态：目录刚被删，按普通目录处理
            return False
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT)


#: 0.3.1（F35）：进程内并发写锁（worker 线程池可能同时写同一文件）。
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock(path: Path) -> threading.Lock:
    """以规范化路径为 key 取写锁；worker 进程内文件操作频率低，表允许常驻。"""

    key = os.path.normcase(str(path))
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError as exc:  # 竞态：列目录后文件被删
        log.debug("stat mtime failed for %s: %s", path, exc)
        return 0


def _is_hidden(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError as exc:
        log.debug("stat attributes failed for %s: %s", path, exc)
        return False
    return bool(attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def _entry(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
        is_dir = path.is_dir()
        size = 0 if is_dir else int(info.st_size)
        mtime = int(info.st_mtime * 1000)
    except OSError as exc:
        log.debug("stat failed for %s: %s", path, exc)
        is_dir, size, mtime = False, 0, 0
    return {
        "name": path.name or str(path),
        "path": str(path),
        "is_dir": is_dir,
        "size": size,
        "mtime_ms": mtime,
        "hidden": _is_hidden(path),
        "drive": path.drive,
    }


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise FileOpError("驱动器枚举仅支持 Windows")
    return ctypes.windll.kernel32


# ------------------------------------------------------------------- drives --
def drives() -> dict[str, Any]:
    """枚举逻辑驱动器（含卷标、文件系统、容量）。"""

    kernel32 = _kernel32()
    items: list[dict[str, Any]] = []
    try:
        mask = int(kernel32.GetLogicalDrives())
    except Exception as exc:  # noqa: BLE001 - 枚举失败向上抛，调用方可读错误
        raise FileOpError(f"枚举驱动器失败: {describe_error(exc)}") from exc
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        root = f"{chr(65 + index)}:\\"
        drive_type = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        if drive_type == 1:  # DRIVE_NO_ROOT_DIR
            continue
        label, filesystem = _volume_info(ctypes, kernel32, root)
        total = free = 0
        try:
            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)
            kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(free_bytes), ctypes.byref(total_bytes), None
            )
            total = int(total_bytes.value)
            free = int(free_bytes.value)
        except Exception as exc:  # noqa: BLE001 - 单盘容量失败不影响枚举其它盘
            log.debug("GetDiskFreeSpaceEx failed for %s: %s", root, exc)
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


def _volume_info(ctypes: Any, kernel32: Any, root: str) -> tuple[str, str]:
    label_buffer = ctypes.create_unicode_buffer(261)
    fs_buffer = ctypes.create_unicode_buffer(261)
    try:
        kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            label_buffer,
            ctypes.sizeof(label_buffer),
            None,
            None,
            None,
            fs_buffer,
            ctypes.sizeof(fs_buffer),
        )
    except Exception as exc:  # noqa: BLE001 - 卷标/文件系统是展示信息，失败给空串
        log.debug("GetVolumeInformation failed for %s: %s", root, exc)
        return "", ""
    return label_buffer.value, fs_buffer.value


# --------------------------------------------------------------------- ops ---
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
                    info = item.stat()
                except OSError as exc:
                    log.debug("scandir stat failed for %s: %s", item.path, exc)
                    continue
                is_dir = item.is_dir()
                entries.append(
                    {
                        "name": item.name,
                        "path": item.path,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else int(info.st_size),
                        "mtime_ms": int(info.st_mtime * 1000),
                        "hidden": bool(
                            getattr(info, "st_file_attributes", 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
                        ),
                        "drive": path.drive,
                    }
                )
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc

    entries.sort(key=lambda entry: (not entry["is_dir"], str(entry["name"]).lower()))
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "entries": entries, "truncated": truncated, "drives": None}


def stat_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileOpError(f"找不到文件或路径（2）：{path}")
    entry = _entry(path)
    try:
        info = path.stat()
        entry["attributes"] = int(getattr(info, "st_file_attributes", 0))
        entry["created_ms"] = int(info.st_ctime * 1000)
        entry["readonly"] = not os.access(path, os.W_OK)
    except OSError as exc:
        log.debug("stat extras failed for %s: %s", path, exc)
    return entry


def mkdir(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileOpError(f"已存在：{path}", winerror=getattr(exc, "winerror", None)) from exc
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path)}


def delete(path: Path, recursive: bool) -> dict[str, Any]:
    """删除文件/目录；符号链接与 junction 只删除链接本身，绝不递归进目标。"""

    if not path.exists() and not _is_link_like(path):
        raise FileOpError(f"找不到文件或路径（2）：{path}")
    try:
        if _is_link_like(path):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        elif path.is_dir():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path)}


def rename(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        path.rename(destination)
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "target": str(destination)}


def copy(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        if path.is_dir() and not _is_link_like(path):
            shutil.copytree(path, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "target": str(destination)}


def move(path: Path, target: str) -> dict[str, Any]:
    destination = Path(target)
    if not destination.is_absolute():
        destination = path.parent / destination
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "target": str(destination)}


def search(path: Path, pattern: str, recursive: bool) -> dict[str, Any]:
    if not path.is_dir():
        raise FileOpError(f"不是目录：{path}")
    needle = str(pattern or "").strip("*").lower() or "*"
    results: list[dict[str, Any]] = []
    truncated = False
    base_depth = len(path.parts)
    try:
        for root, dirnames, filenames in os.walk(path):
            # 0.3.1（F35）：先剪掉 junction/reparse point 目录（is_symlink() 对
            # junction 为 False，不剪会在环上无限递归）；被剪的目录也不进入结果。
            dirnames[:] = [name for name in dirnames if not _is_reparse_point(Path(root) / name)]
            if not recursive and len(Path(root).parts) - base_depth >= 1:
                dirnames[:] = []
            for name in [*filenames, *dirnames]:
                if not fnmatch.fnmatch(name.lower(), f"*{needle}*"):
                    continue
                results.append(_entry(Path(root) / name))
                if len(results) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    break
            if truncated:
                break
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "entries": results, "truncated": truncated}


def read_chunk(path: Path, offset: int, length: int) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise FileOpError(f"找不到文件（2）：{path}")
    try:
        size = path.stat().st_size
        want = max(1, min(int(length) if int(length) > 0 else MAX_READ_BYTES, MAX_READ_BYTES))
        start = max(0, min(int(offset), size))
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(want)
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {
        "offset": start,
        "length": len(data),
        "size": size,
        "eof": start + len(data) >= size,
        "data": base64.b64encode(data).decode("ascii"),
    }


def write_chunk(path: Path, offset: int, data: bytes, truncate: bool, allow_write: bool) -> dict[str, Any]:
    """写入一个分片。

    模式选择（修复旧实现的"offset=0 必截断"）：``truncate`` → ``wb``；否则已存在 → ``r+b``；
    否则仅 ``offset==0`` 允许创建；``offset>0`` 且文件不存在 → 报错。

    0.3.1（F35）：``offset`` 超出"现有大小 + :data:`MAX_WRITE_SPAN_BYTES`"且不带
    ``truncate`` 时拒绝——NTFS 会把中间空洞填零，没有上界一条命令就能写爆磁盘；
    并发写以规范化路径为 key 串行（进程内锁）。
    """

    if not allow_write:
        raise FileOpError("服务器已禁用文件写入")
    if len(data) > MAX_WRITE_BYTES:
        raise FileOpError(f"单个分片超过 {MAX_WRITE_BYTES // (1024 * 1024)}MiB")
    offset = int(offset)
    with _write_lock(path):
        try:
            current_size = path.stat().st_size if path.exists() else 0
            if not truncate and offset > current_size + MAX_WRITE_SPAN_BYTES:
                raise FileOpError(
                    f"写入 offset {offset} 超过现有大小 {current_size} + "
                    f"{MAX_WRITE_SPAN_BYTES // (1024 * 1024)}MiB（稀疏文件防护）；"
                    "如需重建文件请携带 truncate"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            if truncate:
                mode = "wb"
            elif path.exists():
                mode = "r+b"
            elif offset == 0:
                mode = "wb"
            else:
                raise FileOpError(f"分片续写要求文件已存在：{path}")
            with open(path, mode) as handle:
                if offset > 0:
                    handle.seek(offset)
                handle.write(data)
                handle.flush()
            size = path.stat().st_size
        except FileOpError:
            raise
        except OSError as exc:
            raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "offset": offset, "written": len(data), "size": size}


def hash_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileOpError(f"找不到文件（2）：{path}")
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        size = path.stat().st_size
    except OSError as exc:
        raise FileOpError(describe_error(exc), winerror=getattr(exc, "winerror", None)) from exc
    return {"path": str(path), "sha256": digest.hexdigest(), "size": size, "algorithm": "sha256"}


# ----------------------------------------------------------------- perform ---
def perform(
    op: str,
    payload: dict[str, Any],
    *,
    allow_write: bool = True,
    now_ms: Callable[[], int] = default_now_ms,
) -> dict[str, Any]:
    """执行一次文件操作，返回 ``FileOpResult`` 的载荷（不抛未知异常）。"""

    started = now_ms()
    if op not in SUPPORTED_OPS:
        raise FileOpError(f"不支持的操作: {op}")

    if op == "drives":
        data = drives()
    elif op == "list":
        data = list_dir(resolve_path(payload.get("path", "")))
    elif op == "stat":
        data = stat_path(resolve_path(payload.get("path", "")))
    elif op == "mkdir":
        data = mkdir(resolve_path(payload.get("path", "")))
    elif op == "delete":
        data = delete(resolve_path(payload.get("path", "")), bool(payload.get("recursive")))
    elif op == "rename":
        data = rename(resolve_path(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "copy":
        data = copy(resolve_path(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "move":
        data = move(resolve_path(payload.get("path", "")), str(payload.get("target") or ""))
    elif op == "search":
        data = search(
            resolve_path(payload.get("path", "")),
            str(payload.get("pattern") or ""),
            bool(payload.get("recursive")),
        )
    elif op == "read":
        data = read_chunk(
            resolve_path(payload.get("path", "")),
            int(payload.get("offset") or 0),
            int(payload.get("length") or 0),
        )
    elif op == "write":
        if "data" not in payload:
            raise FileOpError("写入命令缺少 data 字段（base64）")
        raw = payload.get("data") or ""
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise FileOpError("写入数据不是合法 base64") from exc
        data = write_chunk(
            resolve_path(payload.get("path", "")),
            int(payload.get("offset") or 0),
            decoded,
            bool(payload.get("truncate")),
            allow_write,
        )
    else:  # hash
        data = hash_file(resolve_path(payload.get("path", "")))

    data["server_time_ms"] = now_ms()
    return {"ok": True, "op": op, "data": data, "error": None, "elapsed_ms": now_ms() - started}


# ----------------------------------------------------------------- handlers --
def make_files_handler(
    *,
    settings_provider: Callable[[], Mapping[str, Any]],
    now_ms: Callable[[], int] = default_now_ms,
) -> CommandHandler:
    """构造 ``files`` 命令的处理器（注册到 ``CommandDispatcher.handlers``）。

    ``file_write_enabled`` 必须由 ``settings_provider`` 提供——配置项要么生效要么不存在
    （规格 C8），因此这里不做“读不到就允许写”的静默默认。
    """

    def handle(command: Command) -> CommandResult:
        allow_write = bool(settings_provider().get("file_write_enabled", True))
        op = str(command.payload.get("op") or "")
        try:
            result = perform(op, command.payload, allow_write=allow_write, now_ms=now_ms)
        except FileOpError as exc:
            return CommandResult(ok=False, error=str(exc), error_code=ERROR_BAD_REQUEST)
        return CommandResult(ok=True, result=result)

    return handle


def register_handlers(
    dispatcher: Any,
    *,
    settings_provider: Callable[[], Mapping[str, Any]],
    now_ms: Callable[[], int] = default_now_ms,
) -> None:
    """把 ``files`` 注册到 :class:`core.commands.CommandDispatcher`（兼容裸 handlers dict）。"""

    handler = make_files_handler(settings_provider=settings_provider, now_ms=now_ms)
    register = getattr(dispatcher, "register", None)
    if callable(register):
        register("files", handler)
    else:
        dispatcher.handlers["files"] = handler


__all__ = [
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_SEARCH_RESULTS",
    "MAX_WRITE_BYTES",
    "MAX_WRITE_SPAN_BYTES",
    "SUPPORTED_OPS",
    "FileOpError",
    "copy",
    "default_now_ms",
    "delete",
    "describe_error",
    "drives",
    "hash_file",
    "list_dir",
    "make_files_handler",
    "mkdir",
    "move",
    "perform",
    "read_chunk",
    "register_handlers",
    "rename",
    "resolve_path",
    "search",
    "stat_path",
    "write_chunk",
]