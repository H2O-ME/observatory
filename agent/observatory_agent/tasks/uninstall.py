"""无 UAC 卸载：整目录 staging + helper 回写确认 + 确认后才 ack。

为什么旧实现错（A1，已实测复现）
--------------------------------

0.2.10 的 ``spawn_purge_helper`` 用 ``shutil.copy2`` 只把 ``observatory-agent.exe`` 复制到
``%TEMP%``（旧 ``uninstall.py:42-48``），而发布产物是 PyInstaller **onedir**（zip 根包含
``_internal/``）。单独复制出来的 exe 启动即报
``Failed to load Python DLL …\\_internal\\python314.dll`` 并退出，于是：

1. 服务端已经先收到 ``uninstall/ack``（旧 ``runner.py:718``），设备被标记“已卸载”；
2. 旧 runner 不检查 helper 返回值，照样 ``_request_shutdown``；
3. 结果：设备离线、控制台显示已卸载，机器上服务/文件/数据全在。

0.3.0 的契约（规格 §4.2 + A1）：

* helper 从**整个安装目录的副本**启动（:func:`stage_helper`），onedir 才能运行；
* helper 启动后**先写确认文件**（:func:`run_purge_helper` 第一步），受控端
  :func:`await_confirmation` 收到确认才 :func:`perform_remote_uninstall` 里 ack 服务端；
* helper 秒退/确认超时 → 上报 ``uninstall_failed``（由调用方回调注入）并**保持服务运行**；
* 只有确认之后才请求停止服务，helper 等旧 PID 退出后再清理。

非 Windows 上可 import；所有 Win32 调用都在函数内部延迟导入。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.commands import ERROR_BAD_REQUEST, Command, CommandHandler, CommandResult
from ..core.config import PRODUCT, STAGING_DIR, program_data_dir
from ..win.service import DEFAULT_IDENTITY, ServiceController, ServiceError

log = logging.getLogger(__name__)

EXE_NAME = "observatory-agent.exe"
#: A1 状态文件契约名（放在 ``%ProgramData%\\Observatory`` 下）。
UNINSTALL_STATUS_FILENAME = "uninstall-status.json"
#: 0.2.10 命名兼容别名。
CONFIRM_FILENAME = UNINSTALL_STATUS_FILENAME

#: 等待服务进程退出的上限（秒）。
WAIT_PID_TIMEOUT_S = 120.0
#: 等待 helper 写 ``ok=true`` 的上限（秒）；超时即判定 helper 起不来。
CONFIRMATION_TIMEOUT_S = 90.0
CONFIRMATION_POLL_S = 0.1
#: 删除目录的最大重试次数与基础退避。
REMOVE_ATTEMPTS = 6
REMOVE_BACKOFF_S = 1.0

#: helper 模式返回码。
RC_OK = 0
RC_CONFIRM_FAILED = 2
RC_SERVICE_FAILED = 3
RC_REMOVE_FAILED = 4

#: ``MoveFileEx`` 的延迟删除标志。
_MOVEFILE_DELAY_UNTIL_REBOOT = 0x4

AsyncAck = Callable[[], Awaitable[None]]
AsyncFailure = Callable[[str], Awaitable[None]]
ShutdownRequest = Callable[[str], None]
Spawner = Callable[..., subprocess.Popen]


class UninstallError(RuntimeError):
    """卸载失败；``reason`` 可直接作为 ``uninstall_failed`` 事件的原因。"""

    def __init__(self, reason: str, *, step: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.step = step


def default_now_ms() -> int:
    return int(time.time() * 1000)


def default_data_dir() -> Path:
    """``%OBS_PROGRAM_DATA%`` 优先，其次 ``%ProgramData%\\Observatory``（实现见 core.config）。"""

    return program_data_dir()


# ------------------------------------------------------------------ staging --
def stage_helper(
    install_dir: str | Path,
    *,
    staging_root: str | Path,
    now_ms: Callable[[], int] = default_now_ms,
) -> Path:
    """把**整个安装目录**复制到 staging 并返回副本路径。

    这是 A1 的核心修复：onedir 产物必须连同 ``_internal/`` 一起复制，单独 exe 无法启动。
    """

    source = Path(install_dir)
    if not source.is_dir():
        raise UninstallError(f"安装目录不存在: {source}", step="stage_helper")
    if not (source / EXE_NAME).is_file():
        raise UninstallError(f"安装目录缺少 {EXE_NAME}: {source}", step="stage_helper")
    root = Path(staging_root)
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"purge-{int(now_ms()) % 10_000_000:07d}"
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    try:
        shutil.copytree(source, staged)
    except OSError as exc:
        raise UninstallError(f"复制卸载 helper 目录失败: {exc}", step="stage_helper") from exc
    if not (staged / EXE_NAME).is_file():
        raise UninstallError("复制后的 staging 目录缺少可执行文件", step="stage_helper")
    log.info("uninstall helper staged: %s", staged)
    return staged


def purge_command(
    staged_dir: str | Path,
    *,
    install_dir: str | Path,
    data_dir: str | Path,
    wait_pid: int,
    confirm_file: str | Path,
) -> list[str]:
    """构造 helper 命令行（纯函数，便于单测）。"""

    return [
        str(Path(staged_dir) / EXE_NAME),
        "--purge-helper",
        "--wait-pid",
        str(int(wait_pid)),
        "--install-dir",
        str(install_dir),
        "--data-dir",
        str(data_dir),
        "--confirm-file",
        str(confirm_file),
    ]


@dataclass(frozen=True)
class PurgeTicket:
    pid: int
    staged_dir: Path
    confirm_file: Path
    log_path: Path
    #: 0.3.8 真机修复：helper 是否还活着必须问**自己的子进程句柄**
    #: （``Popen.poll()``），不要用 PID 去猜。0.3.7 因为拿 PID 猜错，把每一次
    #: 成功的卸载都报成了 ``uninstall_failed``（见 :func:`_process_alive`）。
    child_poll: Callable[[], int | None] | None = None


def spawn_purge_helper(
    staged_dir: str | Path,
    *,
    install_dir: str | Path,
    data_dir: str | Path,
    wait_pid: int,
    confirm_file: str | Path,
    spawn: Spawner = subprocess.Popen,
) -> PurgeTicket:
    """从 staging 副本启动 detached helper；启动失败抛 :class:`UninstallError`。"""

    staged = Path(staged_dir)
    confirmation = Path(confirm_file)
    arguments = purge_command(
        staged,
        install_dir=install_dir,
        data_dir=data_dir,
        wait_pid=wait_pid,
        confirm_file=confirmation,
    )
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    log_path = staged.parent / f"{staged.name}.log"
    try:
        with open(log_path, "wb") as log_file:
            process = spawn(
                arguments,
                cwd=str(staged),
                creationflags=creation_flags,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
    except OSError as exc:
        raise UninstallError(f"无法启动卸载 helper: {exc}", step="spawn_purge_helper") from exc
    log.info("uninstall helper started pid=%s (log %s)", process.pid, log_path)
    return PurgeTicket(
        pid=int(process.pid),
        staged_dir=staged,
        confirm_file=confirmation,
        log_path=log_path,
        child_poll=getattr(process, "poll", None),
    )


# ---------------------------------------------------------------- status file -
#: A1 的统一契约文件：``%ProgramData%\\Observatory\\uninstall-status.json``。
#: 内容固定为 ``{"ok": bool, "pid": int, "ts_ms": int, "error": str|null}``：
#: helper 启动成功先写 ``ok=true``；受控端**只在读到 ok=true 之后**才 ack 服务端；
#: helper 后续清理失败会把同一文件改写成 ``ok=false`` + error（可观测失败，C6）。
def status_document(*, ok: bool, pid: int, ts_ms: int, error: str | None = None) -> dict[str, Any]:
    """构造状态文件内容（纯函数）。"""

    return {
        "ok": bool(ok),
        "pid": int(pid),
        "ts_ms": int(ts_ms),
        "error": None if error is None else str(error)[:500],
    }


def write_status(path: str | Path, document: dict[str, Any]) -> None:
    """原子写状态文件（临时文件 + ``os.replace``，避免读到半截 JSON）。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.part")
    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def parse_status(raw: bytes, *, expected_pid: int | None = None) -> dict[str, Any]:
    """解析并校验状态文件；结构非法/来自别的 helper 抛 :class:`UninstallError`。"""

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UninstallError(f"状态文件不是合法 JSON: {exc}", step="parse_status") from exc
    if not isinstance(document, dict):
        raise UninstallError("状态文件必须是 JSON object", step="parse_status")
    if not isinstance(document.get("ok"), bool):
        raise UninstallError("状态文件缺少 ok 布尔值", step="parse_status")
    pid = int(document.get("pid") or 0)
    if pid <= 0:
        raise UninstallError("状态文件缺少 pid", step="parse_status")
    if expected_pid is not None and pid != int(expected_pid):
        raise UninstallError(
            f"状态文件来自其它 helper（pid={pid}，期望 {expected_pid}）", step="parse_status"
        )
    if int(document.get("ts_ms") or 0) <= 0:
        raise UninstallError("状态文件缺少 ts_ms", step="parse_status")
    error = document.get("error")
    if error is not None and not isinstance(error, str):
        raise UninstallError("状态文件 error 必须是字符串或 null", step="parse_status")
    return {"ok": bool(document["ok"]), "pid": pid, "ts_ms": int(document["ts_ms"]), "error": error}


def read_status(path: str | Path) -> dict[str, Any] | None:
    """读取状态文件；尚未写入返回 ``None``，内容损坏抛 :class:`UninstallError`。"""

    target = Path(path)
    if not target.is_file():
        return None
    return parse_status(target.read_bytes())


def wait_for_ready_status(
    path: str | Path,
    *,
    expected_pid: int | None = None,
    timeout_s: float = CONFIRMATION_TIMEOUT_S,
    process_alive: Callable[[int], bool] | None = None,
    child_poll: Callable[[], int | None] | None = None,
    log_path: Path | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = CONFIRMATION_POLL_S,
) -> dict[str, Any]:
    """等待 helper 报告 ``ok=true``；helper 提前退出/报告失败/超时都抛异常。"""

    deadline = now() + max(0.0, timeout_s)
    tail = f"，日志尾部：{_read_log_tail(log_path)}" if log_path is not None else ""
    while now() < deadline:
        try:
            document = read_status(path)
        except OSError as exc:  # 文件正在被原子替换，下一轮再读
            log.debug("status read raced: %s", exc)
            document = None
        if document is not None and document["ok"]:
            return document
        if document is not None:
            raise UninstallError(
                document["error"] or "卸载 helper 报告失败", step="wait_for_ready_status"
            )
        if expected_pid is not None and not _alive(expected_pid, process_alive, child_poll):
            raise UninstallError(
                f"卸载 helper 在确认前退出（pid={expected_pid}{_peek_exit(child_poll)}{tail}）",
                step="wait_for_ready_status",
            )
        sleep(poll_s)
    raise UninstallError(
        f"卸载 helper 在 {timeout_s:.0f}s 内未确认（status={path}{tail}）",
        step="wait_for_ready_status",
    )


def _alive(
    pid: int,
    process_alive: Callable[[int], bool] | None,
    child_poll: Callable[[], int | None] | None,
) -> bool:
    """存活判定入口：显式注入的回调优先，其次自己的孩子句柄，最后 PID 探测。"""

    if process_alive is not None:
        return bool(process_alive(pid))
    return _process_alive(pid, child_poll=child_poll)


def _peek_exit(child_poll: Callable[[], int | None] | None) -> str:
    """失败信息里带上 helper 的退出码（有的话），便于现场定位。"""

    if child_poll is None:
        return ""
    try:
        code = child_poll()
    except Exception:  # noqa: BLE001 - 拿不到退出码不影响报错
        return ""
    return "" if code is None else f"，退出码={code}"


def verify_helper_alive(
    ticket: PurgeTicket,
    *,
    grace_s: float = 2.0,
    process_alive: Callable[[int], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.1,
) -> dict[str, Any]:
    """A1 断言入口：helper 在 ``grace_s`` 内一直存活，且已回写 ``ok=true``。"""

    deadline = now() + max(0.0, grace_s)
    while now() < deadline:
        if not _alive(ticket.pid, process_alive, ticket.child_poll):
            raise UninstallError(
                f"卸载 helper 启动即退出（pid={ticket.pid}{_peek_exit(ticket.child_poll)}，"
                f"日志尾部：{_read_log_tail(ticket.log_path)}）",
                step="verify_helper_alive",
            )
        sleep(poll_s)
    document = read_status(ticket.confirm_file)
    if document is None:
        raise UninstallError(
            f"卸载 helper 未回写状态文件（pid={ticket.pid}）", step="verify_helper_alive"
        )
    if not document["ok"]:
        raise UninstallError(document["error"] or "卸载 helper 报告失败", step="verify_helper_alive")
    return document


#: 0.2.10 命名的兼容别名（旧测试/旧调用方仍可用；语义已升级为状态文件契约）。
def confirmation_document(*, pid: int, staged_to: str | Path = "", now_ms: int) -> dict[str, Any]:
    return status_document(ok=True, pid=pid, ts_ms=now_ms)


def parse_confirmation(raw: bytes, *, expected_pid: int) -> dict[str, Any]:
    return parse_status(raw, expected_pid=expected_pid)


def await_confirmation(
    ticket: PurgeTicket,
    *,
    timeout_s: float = CONFIRMATION_TIMEOUT_S,
    process_alive: Callable[[int], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = CONFIRMATION_POLL_S,
) -> dict[str, Any]:
    """等待 helper 确认（``wait_for_ready_status`` 的 ticket 版封装）。"""

    return wait_for_ready_status(
        ticket.confirm_file,
        expected_pid=ticket.pid,
        timeout_s=timeout_s,
        process_alive=process_alive,
        child_poll=ticket.child_poll,
        log_path=ticket.log_path,
        now=now,
        sleep=sleep,
        poll_s=poll_s,
    )


#: ``SYNCHRONIZE`` 访问掩码（0x00100000）。**不要**写 ``win32process.SYNCHRONIZE``：
#: pywin32 的 ``win32process`` 并不导出这个名字，0.3.7 真机实测因此抛
#: ``AttributeError``，被下面的 ``except Exception`` 吞成“进程已退出”——每一次远程
#: 卸载在 helper 刚起来（甚至还来不及写状态文件）时就被判定“确认前退出”。
_SYNCHRONIZE = 0x00100000
#: ``OpenProcess`` 在“该 PID 不存在”时返回的错误码。
_ERROR_INVALID_PARAMETER = 87


def _process_alive(
    pid: int,
    *,
    child_poll: Callable[[], int | None] | None = None,
) -> bool:
    """子进程是否仍在运行。**拿不准时返回 True**（不确定 ≠ 已退出）。

    ``child_poll`` 是 ``Popen.poll``：有自己的句柄时它是唯一权威答案（没有 PID
    复用问题）。没有句柄才退回 ``OpenProcess``/``os.kill`` 探测，且只有明确
    "不存在"才判死——权限不足、pywin32 缺失都按“还活着”处理，因为把这个不确定
    判成“已退出”的代价是：一次真实成功的卸载被上报为失败，而 helper 仍会继续
    删掉整台机器的文件（0.3.7 的真实故障）。
    """

    if child_poll is not None:
        try:
            if child_poll() is None:
                return True
        except Exception:  # noqa: BLE001 - 句柄不可用时退回 PID 探测
            log.debug("helper 句柄不可用，退回 PID 探测", exc_info=True)
        else:
            return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True
    try:
        import win32api
        import win32con
        import win32event
    except Exception:  # noqa: BLE001 - 没有 pywin32 时无法断言“已退出”
        log.warning("pywin32 不可用，跳过 helper 存活探测（按存活处理）")
        return True
    access = int(getattr(win32con, "SYNCHRONIZE", _SYNCHRONIZE))
    try:
        handle = win32api.OpenProcess(access, False, int(pid))
    except Exception as exc:  # noqa: BLE001 - 只有“不存在”才算退出
        return not _process_missing(exc)
    try:
        return win32event.WaitForSingleObject(handle, 0) != win32event.WAIT_OBJECT_0
    finally:
        with contextlib.suppress(Exception):  # 关闭句柄失败不影响判定
            win32api.CloseHandle(handle)


def _process_missing(exc: BaseException) -> bool:
    """``OpenProcess`` 抛的异常是不是“这个 PID 不存在”（而不是没权限）。"""

    code = getattr(exc, "winerror", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None
    return code == _ERROR_INVALID_PARAMETER


def _read_log_tail(path: Path, *, limit: int = 800) -> str:
    try:
        return path.read_bytes()[-limit:].decode("utf-8", "replace").strip() or "<空>"
    except OSError as exc:
        return f"<无法读取日志 {path}: {exc}>"


# ------------------------------------------------------------- orchestration -
async def perform_remote_uninstall(
    *,
    install_dir: str | Path,
    wait_pid: int,
    ack: AsyncAck,
    report_failure: AsyncFailure,
    request_shutdown: ShutdownRequest,
    data_dir: str | Path | None = None,
    staging_root: str | Path | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    confirmation_timeout_s: float = CONFIRMATION_TIMEOUT_S,
    spawn: Spawner = subprocess.Popen,
    process_alive: Callable[[int], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """远程卸载的唯一入口（命令处理器调用）。

    成功：``ack()`` → ``request_shutdown("uninstall")`` → 返回 True；
    失败：``report_failure(reason)``（调用方映射为 ``uninstall_failed`` 事件）→ 返回 False，
    **服务继续运行**（A1 的要求：卸载失败不能让设备离线）。
    """

    install = Path(install_dir)
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    staging = Path(staging_root) if staging_root is not None else STAGING_DIR
    status_path = data / UNINSTALL_STATUS_FILENAME
    try:
        # 丢弃上一次卸载残留的状态文件，否则会读到旧的 ok=true 而误 ack。
        with contextlib.suppress(FileNotFoundError):
            status_path.unlink()
        staged = stage_helper(install, staging_root=staging, now_ms=now_ms)
        ticket = spawn_purge_helper(
            staged,
            install_dir=install,
            data_dir=data,
            wait_pid=wait_pid,
            confirm_file=status_path,
            spawn=spawn,
        )
        await asyncio.to_thread(
            await_confirmation,
            ticket,
            timeout_s=confirmation_timeout_s,
            process_alive=process_alive,
            now=now,
            sleep=sleep,
        )
    except UninstallError as exc:
        log.error("uninstall aborted, service stays online: %s", exc.reason)
        try:
            await report_failure(exc.reason)
        except Exception:  # noqa: BLE001 - 上报失败不能改变“保持服务”的决定，但必须记录
            log.exception("failed to report uninstall_failed")
        return False
    log.info("uninstall helper confirmed pid=%s status=%s", ticket.pid, status_path)
    # A1：确认 ok=true 之后才允许 ack 服务端。
    await ack()
    request_shutdown("uninstall")
    return True


# ---------------------------------------------------------------- helper 模式 -
def run_purge_helper(
    *,
    wait_pid: int,
    install_dir: str | Path,
    data_dir: str | Path,
    confirm_file: str | Path,
    controller: ServiceController | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    sleep: Callable[[float], None] = time.sleep,
    exit_process: Callable[[int], None] | None = None,
) -> int:
    """purge helper 主逻辑（main.py 的 ``--purge-helper`` / ``--purge`` 分支调用）。

    执行顺序：**先写 ``uninstall-status.json``（ok=true）** 证明 helper 真的起来了 →
    等旧进程退出 → 停/删服务 → 清足迹 → 删安装/数据目录 → 计划自删。
    清理阶段失败会把同一状态文件改写成 ``ok=false`` + error（可观测失败，C6）。返回 ``RC_*``。
    """

    install = Path(install_dir)
    data = Path(data_dir)
    status_path = Path(confirm_file)
    service = controller or ServiceController(DEFAULT_IDENTITY)
    _say(f"start install_dir={install} data_dir={data} status={status_path} wait_pid={wait_pid}")
    try:
        write_status(status_path, status_document(ok=True, pid=os.getpid(), ts_ms=now_ms()))
    except OSError as exc:
        log.error("could not write uninstall status file: %s", exc)
        _say(f"写状态文件失败：{exc}")
        return RC_CONFIRM_FAILED
    _say("status ok=true 已写入")

    steps: list[tuple[str, Callable[[], Any]]] = [
        ("stop service", lambda: _clean_service_step(service)),
        ("remove footprint", _remove_footprint),
        ("remove install dir", lambda: _remove_tree(install, sleep=sleep)),
        ("remove data dir", lambda: _remove_tree(data, sleep=sleep)),
    ]
    status = RC_OK
    if not service.wait_for_process_exit(wait_pid, timeout_s=WAIT_PID_TIMEOUT_S):
        log.warning("旧服务进程 %s 在 %.0fs 内未退出，继续清理", wait_pid, WAIT_PID_TIMEOUT_S)
    for name, step in steps:
        try:
            result = step()
        except Exception:  # noqa: BLE001 - 单步失败不阻断后续清理，但必须记录并反映到返回码
            log.exception("uninstall step failed: %s", name)
            status = RC_REMOVE_FAILED if "remove" in name else RC_SERVICE_FAILED
            continue
        if result is False:
            status = RC_REMOVE_FAILED if "remove" in name else RC_SERVICE_FAILED
    if status != RC_OK:
        message = f"卸载清理未完全成功（rc={status}），详见 staging 目录下的 purge-*.log 与系统日志"
        with contextlib.suppress(OSError):  # 数据目录可能已被删除，写不进去就只能靠日志
            write_status(status_path, status_document(ok=False, pid=os.getpid(), ts_ms=now_ms(), error=message))
    _schedule_self_delete(Path(sys.executable).parent)
    if data.exists():
        # 数据目录里可能残留 helper 自己的 staging（正在使用无法立即删），登记重启时删除。
        _schedule_self_delete(data)
    log.info("uninstall finished (status=%s)", status)
    _say(f"finished status={status}")
    if exit_process is not None:
        exit_process(status)
    return status


def _say(message: str) -> None:
    """helper 也往 stdout 说一句。

    父进程把 helper 的 stdout/stderr 重定向进 ``staging/purge-*.log``，而 helper
    自己的 logging 写的是 ``%ProgramData%\\Observatory\\logs\\uninstall*.log``。
    0.3.7 真机现场失败信息只剩 ``日志尾部：<空>``，因为日志文件里本来就没有内容；
    关键节点在 stdout 重复一遍，失败原因才查得到。
    """

    with contextlib.suppress(Exception):  # 控制台可能已关闭/管道已断开
        print(f"[purge-helper pid={os.getpid()}] {message}", flush=True)


def _clean_service_step(service: ServiceController) -> bool:
    stopped = False
    try:
        stopped = service.stop()
    except ServiceError as exc:
        log.error("stop service failed: %s", exc)
    try:
        removed = service.remove()
    except ServiceError as exc:
        log.error("remove service failed: %s", exc)
        return False
    return stopped and removed


def _remove_footprint() -> None:
    """清理安装器可能留下的防火墙规则 / Defender 排除项 / 注册表键。

    每一步都是 best-effort，但失败要记录（旧实现把所有异常直接吞掉）。
    """

    _remove_firewall_rules()
    _remove_defender_exclusion()
    _remove_registry_keys()


def _remove_firewall_rules() -> None:
    if os.name != "nt":
        return
    try:
        import win32com.client
    except Exception as exc:  # noqa: BLE001 - pywin32 缺失不代表卸载失败
        log.warning("firewall cleanup skipped: %s", exc)
        return
    try:
        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        for rule in list(policy.Rules):
            with contextlib.suppress(Exception):  # 单条规则读取失败（权限/类型）不影响其它规则
                if PRODUCT.lower() in str(rule.Name).lower():
                    policy.Rules.Remove(rule.Name)
                    log.info("removed firewall rule %s", rule.Name)
    except Exception as exc:  # noqa: BLE001 - COM 不可用时记录并继续
        log.warning("firewall cleanup failed: %s", exc)


def _remove_defender_exclusion() -> None:
    if os.name != "nt":
        return
    command = (
        "Remove-MpPreference -ExclusionPath '"
        + str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / PRODUCT)
        + "' -ErrorAction SilentlyContinue"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("defender cleanup skipped: %s", exc)
        return
    if completed.returncode != 0:
        log.warning(
            "defender cleanup returned %s: %s",
            completed.returncode,
            (completed.stderr or b"").decode("utf-8", "replace").strip()[:200],
        )


def _remove_registry_keys() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError as exc:  # 非 Windows 理论上不会走到这里
        log.warning("registry cleanup skipped: %s", exc)
        return
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{PRODUCT}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{PRODUCT}"),
    ):
        try:
            winreg.DeleteKey(root, path)
            log.info("removed registry key %s", path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("registry cleanup failed for %s: %s", path, exc)


def _remove_tree(root: Path, *, sleep: Callable[[float], None]) -> bool:
    for attempt in range(1, REMOVE_ATTEMPTS + 1):
        if not root.exists():
            return True
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            log.info("removed %s", root)
            return True
        sleep(REMOVE_BACKOFF_S * attempt)
    log.warning("could not remove %s after %s attempts (rest scheduled for reboot)", root, REMOVE_ATTEMPTS)
    _schedule_self_delete(root)
    return False


def _schedule_self_delete(root: Path) -> None:
    """把 helper 自身/残留文件登记为“重启时删除”，保证最后一步能自清。"""

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel32.MoveFileExW.restype = ctypes.c_int
    except Exception as exc:  # noqa: BLE001 - 无法登记时至少已记录
        log.warning("self-delete scheduling unavailable: %s", exc)
        return
    candidates = [root]
    if root.is_dir():
        candidates.extend(sorted(root.rglob("*"), reverse=True))
    for candidate in candidates:
        try:
            if kernel32.MoveFileExW(str(candidate), None, _MOVEFILE_DELAY_UNTIL_REBOOT):
                continue
            log.debug("could not schedule deletion for %s: %s", candidate, ctypes.get_last_error())
        except Exception as exc:  # noqa: BLE001 - 单个路径失败不影响其它路径
            log.debug("schedule deletion failed for %s: %s", candidate, exc)


# ---------------------------------------------------------------- interactive -
def interactive_uninstall(
    *,
    install_dir: str | Path,
    data_dir: str | Path | None = None,
    staging_root: str | Path | None = None,
    wait_pid: int | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    spawn: Spawner = subprocess.Popen,
) -> int:
    """本机提权卸载（``--uninstall``）：staging → 起 helper → 等状态 ok → 返回。

    调用方（main.py）在返回 0 后必须退出进程，helper 在等 ``wait_pid`` 消失后继续清理。
    """

    install = Path(install_dir)
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    staging = Path(staging_root) if staging_root is not None else STAGING_DIR
    status_path = data / UNINSTALL_STATUS_FILENAME
    try:
        # 丢弃上一次卸载残留的状态文件，否则会读到旧的 ok=true 而误 ack。
        with contextlib.suppress(FileNotFoundError):
            status_path.unlink()
        staged = stage_helper(install, staging_root=staging, now_ms=now_ms)
        ticket = spawn_purge_helper(
            staged,
            install_dir=install,
            data_dir=data,
            wait_pid=os.getpid() if wait_pid is None else int(wait_pid),
            confirm_file=status_path,
            spawn=spawn,
        )
        verify_helper_alive(ticket, grace_s=1.0)
        await_confirmation(ticket, timeout_s=min(CONFIRMATION_TIMEOUT_S, 30.0))
    except UninstallError as exc:
        log.error("interactive uninstall failed: %s", exc.reason)
        print(f"卸载失败：{exc.reason}", file=sys.stderr)
        return 1
    print(f"卸载程序已启动（pid {ticket.pid}），本进程退出后继续清理。")
    return 0


def purge(install_dir: str | Path, wait_pid: int | None = None, **kwargs: Any) -> int:
    """本地卸载的简明入口（Lead 约定 ``purge(install_dir, wait_pid) -> int``）。"""

    return interactive_uninstall(install_dir=install_dir, wait_pid=wait_pid, **kwargs)


# ---------------------------------------------------------------- handlers ---
def make_uninstall_handler(
    *,
    install_dir: str | Path,
    ack: AsyncAck,
    report_failure: AsyncFailure,
    request_shutdown: ShutdownRequest,
    data_dir: str | Path | None = None,
    staging_root: str | Path | None = None,
    spawn_task: Callable[[Awaitable[Any]], Any] | None = None,
    current_pid: int | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    confirmation_timeout_s: float = CONFIRMATION_TIMEOUT_S,
    spawn: Spawner = subprocess.Popen,
) -> CommandHandler:
    """构造 ``uninstall`` 命令处理器。

    处理器立即返回 ``started`` 回执；后台任务完成“staging → 起 helper → 等确认 → ack →
    请求停服”。任何失败都走 ``report_failure``（``uninstall_failed``）并且**不停服**。
    """

    state = {"running": False}

    def handle(command: Command) -> CommandResult:
        if state["running"]:
            return CommandResult(
                ok=False, result={}, error="已有卸载在进行中", error_code=ERROR_BAD_REQUEST
            )
        state["running"] = True
        wait_pid = os.getpid() if current_pid is None else int(current_pid)

        async def run() -> None:
            try:
                await perform_remote_uninstall(
                    install_dir=install_dir,
                    wait_pid=wait_pid,
                    ack=ack,
                    report_failure=report_failure,
                    request_shutdown=request_shutdown,
                    data_dir=data_dir,
                    staging_root=staging_root,
                    now_ms=now_ms,
                    confirmation_timeout_s=confirmation_timeout_s,
                    spawn=spawn,
                )
            finally:
                state["running"] = False

        launcher = spawn_task or asyncio.create_task
        launcher(run())
        return CommandResult(ok=True, result={"started": True})

    return handle


def register_handlers(dispatcher: Any, **kwargs: Any) -> None:
    """把 ``uninstall`` 注册到 :class:`core.commands.CommandDispatcher`（兼容裸 handlers dict）。"""

    handler = make_uninstall_handler(**kwargs)
    register = getattr(dispatcher, "register", None)
    if callable(register):
        register("uninstall", handler)
    else:
        dispatcher.handlers["uninstall"] = handler


__all__ = [
    "CONFIRMATION_TIMEOUT_S",
    "CONFIRM_FILENAME",
    "EXE_NAME",
    "PRODUCT",
    "PurgeTicket",
    "RC_CONFIRM_FAILED",
    "RC_OK",
    "RC_REMOVE_FAILED",
    "RC_SERVICE_FAILED",
    "UNINSTALL_STATUS_FILENAME",
    "UninstallError",
    "WAIT_PID_TIMEOUT_S",
    "await_confirmation",
    "confirmation_document",
    "default_data_dir",
    "default_now_ms",
    "interactive_uninstall",
    "make_uninstall_handler",
    "parse_confirmation",
    "parse_status",
    "perform_remote_uninstall",
    "purge",
    "purge_command",
    "read_status",
    "register_handlers",
    "run_purge_helper",
    "spawn_purge_helper",
    "stage_helper",
    "status_document",
    "verify_helper_alive",
    "wait_for_ready_status",
    "write_status",
]