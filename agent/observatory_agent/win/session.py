"""WTS 会话发现与 SYSTEM → 用户会话的进程跨越。

为什么旧实现错
--------------

* 旧 ``session_launch.py`` 只给 ``DuplicateTokenEx`` 设了 ``argtypes``，
  ``CreateProcessAsUserW`` / ``CreateEnvironmentBlock`` / ``DestroyEnvironmentBlock``
  都是无原型的裸调用：64 位下 ``HANDLE`` 走 ctypes 默认的 ``c_int``，一旦句柄值超出
  32 位（或高位被符号扩展）就会被截断。现在每个 Win32 调用都有完整 ``argtypes`` /
  ``restype``。
* 旧实现的命令行是手写拼接（只给含空格的参数加引号、不转义引号），带引号的路径会拼错；
  现在统一用 ``subprocess.list2cmdline``。
* 旧实现失败时返回 ``None``，上层只能看到“没起来”；新实现抛 :class:`SessionError`，
  带 ``step`` 与 ``winerror``，失败原因可观测（规格 C6）。

非 Windows 上本模块可 import；所有 Win32 调用都会抛 :class:`WindowsOnlyOperation`。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import subprocess
import sys
from collections.abc import Iterable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from . import WindowsOnlyOperation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- constants --
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
#: ``MAXIMUM_ALLOWED``；旧代码用 ``win32security.MAXIMUM_ALLOWED``（不存在）导致
#: 每次拉起都 AttributeError 并被静默吞掉，worker 永远回退 session 0。
MAXIMUM_ALLOWED = 0x02000000

#: WTS_CONNECTSTATE_CLASS 取值。
WTS_ACTIVE = 0
WTS_CONNECTED = 1
WTS_CONNECTQUERY = 2
WTS_SHADOW = 3
WTS_DISCONNECTED = 4
WTS_IDLE = 5
WTS_LISTEN = 6
WTS_RESET = 7
WTS_DOWN = 8
WTS_INIT = 9

STATE_NAMES: dict[int, str] = {
    WTS_ACTIVE: "active",
    WTS_CONNECTED: "connected",
    WTS_CONNECTQUERY: "connectquery",
    WTS_SHADOW: "shadow",
    WTS_DISCONNECTED: "disconnected",
    WTS_IDLE: "idle",
    WTS_LISTEN: "listen",
    WTS_RESET: "reset",
    WTS_DOWN: "down",
    WTS_INIT: "init",
}

#: 可采集的会话状态。
#:
#: 0.3.1（F36）：加入 ``disconnected``——RDP 断开后（mstsc 直接关窗）会话进入该状态，
#: 其桌面栈仍在、``CreateProcessAsUser`` 仍可用，不收录会导致无人值守形态失联；
#: 移除 ``connected``——``WTS_CONNECTQUERY``（正在接入中）桌面未就绪，在该状态下
#: 拉起 worker 常失败且白耗重试。
CAPTURABLE_STATES = frozenset({"active", "disconnected"})


class SessionError(RuntimeError):
    """会话枚举或跨会话拉起失败；``step`` 标明失败步骤。"""

    def __init__(self, message: str, *, step: str = "", winerror: int | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.winerror = winerror


# ------------------------------------------------------------------ structs --
class STARTUPINFOW(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    )


# ----------------------------------------------------------------- sessions --
@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    username: str
    state: str = "Unknown"
    station: str = ""
    is_console: bool = False


def _require_windows(operation: str) -> None:
    if sys.platform != "win32":
        raise WindowsOnlyOperation(f"{operation} 只能在 Windows 上执行")


def _winerror(exc: BaseException) -> int | None:
    code = getattr(exc, "winerror", None)
    if isinstance(code, int):
        return code
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _session_modules():
    _require_windows("会话枚举")
    import win32ts

    return win32ts


def console_session_id() -> int:
    """当前活动控制台会话 ID；拿不到返回 0（表示没有交互式控制台）。"""

    try:
        win32ts = _session_modules()
    except WindowsOnlyOperation:
        raise
    try:
        return int(win32ts.WTSGetActiveConsoleSessionId())
    except Exception as exc:  # noqa: BLE001 - 枚举失败是环境问题，返回 0 供纯函数降级
        log.warning("WTSGetActiveConsoleSessionId failed: %s", exc)
        return 0


def list_sessions() -> list[SessionInfo]:
    """枚举交互式会话（跳过 session 0：服务会话没有桌面）。"""

    win32ts = _session_modules()
    try:
        try:
            raw = win32ts.WTSEnumerateSessions(None)
        except TypeError:
            # 老 pywin32 的签名是 (server, reserved, version)。
            raw = win32ts.WTSEnumerateSessions(None, 0, 1)
    except Exception as exc:  # noqa: BLE001 - 枚举失败向上抛，调用方决定是否降级
        raise SessionError(f"枚举会话失败: {exc}", step="WTSEnumerateSessions", winerror=_winerror(exc)) from exc

    console = console_session_id()
    sessions: list[SessionInfo] = []
    for entry in raw:
        session_id = int(entry.get("SessionId", 0))
        if session_id == 0:
            continue
        state_value = int(entry.get("State", WTS_DOWN))
        username = ""
        with contextlib.suppress(Exception):  # 取不到用户名是正常情况（未登录/监听会话）
            username = str(win32ts.WTSQuerySessionInformation(None, session_id, win32ts.WTSUserName) or "")
        sessions.append(
            SessionInfo(
                session_id=session_id,
                username=username,
                state=STATE_NAMES.get(state_value, str(state_value)),
                station=str(entry.get("WinStationName") or entry.get("StationName") or ""),
                is_console=session_id == console,
            )
        )
    return sessions


def select_capturable(sessions: Iterable[SessionInfo]) -> int | None:
    """选定需要拉 worker 的会话：控制台优先，否则第一个有登录名的可用会话。

    纯函数（无 Win32 依赖），可直接单测。没有真实用户名（``WTSQueryUserToken`` 必回
    1008）或状态不在 :data:`CAPTURABLE_STATES`（active/disconnected，见 F36）的会话
    一律跳过——旧实现在这类“残留会话”上每 30 秒重试一次，白耗 CPU 并刷日志。
    """

    candidates = [
        session
        for session in sessions
        if session.username.strip() and session.state in CAPTURABLE_STATES
    ]
    if not candidates:
        return None
    for session in candidates:
        if session.is_console:
            return session.session_id
    return candidates[0].session_id


def session_user_sid(session_id: int) -> str:
    """会话登录用户的 SID 字符串（管道 ACL 用）。失败抛 :class:`SessionError`。"""

    _require_windows("解析会话用户 SID")
    try:
        import win32api
        import win32security
        import win32ts
    except Exception as exc:  # noqa: BLE001 - pywin32 缺失是部署问题
        raise SessionError(f"pywin32 不可用: {exc}", step="import") from exc

    token = None
    try:
        try:
            token = win32ts.WTSQueryUserToken(session_id)
        except Exception as exc:  # noqa: BLE001 - 会话没有登录用户
            raise SessionError(
                f"会话 {session_id} 没有可用的用户令牌: {exc}",
                step="WTSQueryUserToken",
                winerror=_winerror(exc),
            ) from exc
        if not token:
            raise SessionError(f"会话 {session_id} 没有可用的用户令牌", step="WTSQueryUserToken")
        info = win32security.GetTokenInformation(token, win32security.TokenUser)
        sid = info[0] if isinstance(info, (tuple, list)) else info
        return str(win32security.ConvertSidToStringSid(sid))
    except SessionError:
        raise
    except Exception as exc:  # noqa: BLE001 - 令牌查询失败向上抛
        raise SessionError(
            f"读取会话用户 SID 失败: {exc}", step="GetTokenInformation", winerror=_winerror(exc)
        ) from exc
    finally:
        if token:
            with contextlib.suppress(Exception):  # 关闭令牌失败不影响调用方
                win32api.CloseHandle(token)


# --------------------------------------------------------------- launching ---
def executable_path() -> Path:
    """当前进程的可执行文件（frozen 时是打包 exe，源码运行时是 python）。"""

    return Path(sys.executable).resolve()


def worker_arguments(session_id: int, *, frozen: bool | None = None) -> list[str]:
    """worker 子进程参数；``frozen`` 可注入以便单测。"""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return ["--worker", "--session", str(session_id)]
    return ["-m", "observatory_agent", "--worker", "--session", str(session_id)]


def build_command_line(executable: str | Path, arguments: Sequence[str]) -> str:
    """按 Windows 命令行规则拼接（引号/反斜杠转义交给 stdlib）。"""

    return subprocess.list2cmdline([str(executable), *[str(argument) for argument in arguments]])


def _load_win32_dlls():
    """加载并给所有用到的 Win32 函数设置原型。"""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateTokenEx.restype = wintypes.BOOL

    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

    userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
    return kernel32, advapi32, userenv


def _create_process_as_user(session_id: int, application: str, command_line: str) -> int:
    """在 ``session_id`` 的用户上下文里创建进程，返回 PID。失败抛 SessionError。"""

    _require_windows("跨会话创建进程")
    try:
        import win32api
        import win32security
        import win32ts
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"pywin32 不可用: {exc}", step="import") from exc

    kernel32, advapi32, userenv = _load_win32_dlls()
    token = None
    duplicated: wintypes.HANDLE | None = None
    environment = ctypes.c_void_p()
    try:
        try:
            token = win32ts.WTSQueryUserToken(session_id)
        except Exception as exc:  # noqa: BLE001
            raise SessionError(
                f"会话 {session_id} 无法取得用户令牌: {exc}",
                step="WTSQueryUserToken",
                winerror=_winerror(exc),
            ) from exc
        if not token:
            raise SessionError(f"会话 {session_id} 无法取得用户令牌", step="WTSQueryUserToken")

        duplicated = wintypes.HANDLE()
        if not advapi32.DuplicateTokenEx(
            wintypes.HANDLE(int(token)),
            MAXIMUM_ALLOWED,
            None,
            win32security.SecurityImpersonation,
            win32security.TokenPrimary,
            ctypes.byref(duplicated),
        ):
            raise SessionError(
                f"DuplicateTokenEx 失败 (winerror={ctypes.get_last_error()})",
                step="DuplicateTokenEx",
                winerror=ctypes.get_last_error(),
            )
        if not userenv.CreateEnvironmentBlock(ctypes.byref(environment), duplicated, False):
            raise SessionError(
                f"CreateEnvironmentBlock 失败 (winerror={ctypes.get_last_error()})",
                step="CreateEnvironmentBlock",
                winerror=ctypes.get_last_error(),
            )

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_HIDE
        info = PROCESS_INFORMATION()
        command_buffer = ctypes.create_unicode_buffer(command_line)
        application_buffer = ctypes.c_wchar_p(application)
        flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        if not advapi32.CreateProcessAsUserW(
            duplicated,
            application_buffer,
            command_buffer,
            None,
            None,
            False,
            flags,
            environment,
            None,
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            raise SessionError(
                f"CreateProcessAsUserW 失败 (winerror={ctypes.get_last_error()})",
                step="CreateProcessAsUserW",
                winerror=ctypes.get_last_error(),
            )
        pid = int(info.dwProcessId)
        for handle in (info.hProcess, info.hThread):
            with contextlib.suppress(Exception):  # 句柄关闭失败不影响已创建的进程
                kernel32.CloseHandle(handle)
        log.info("launched worker pid=%s session=%s", pid, session_id)
        return pid
    finally:
        if environment:
            with contextlib.suppress(Exception):  # 环境块释放失败只可能是重复释放
                userenv.DestroyEnvironmentBlock(environment)
        if duplicated:
            with contextlib.suppress(Exception):  # 句柄可能已被系统回收
                kernel32.CloseHandle(duplicated)
        if token:
            with contextlib.suppress(Exception):  # PyHANDLE 关闭失败不影响结果
                win32api.CloseHandle(token)


def launch_worker(
    session_id: int,
    *,
    executable: str | Path | None = None,
    extra_args: Sequence[str] = (),
    frozen: bool | None = None,
) -> int:
    """在指定会话拉起 worker，返回 PID；失败抛 :class:`SessionError`。"""

    exe = Path(executable) if executable is not None else executable_path()
    arguments = [*worker_arguments(session_id, frozen=frozen), *extra_args]
    command_line = build_command_line(exe, arguments)
    return _create_process_as_user(session_id, str(exe), command_line)


def spawn_local(arguments: Sequence[str], *, executable: str | Path | None = None) -> subprocess.Popen:
    """console 模式：在当前会话直接起 worker（不跨会话）。"""

    exe = str(executable) if executable is not None else str(executable_path())
    try:
        return subprocess.Popen(
            [exe, *[str(argument) for argument in arguments]],
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise SessionError(f"本机拉起 worker 失败: {exc}", step="Popen") from exc


__all__ = [
    "CAPTURABLE_STATES",
    "CREATE_NO_WINDOW",
    "MAXIMUM_ALLOWED",
    "STATE_NAMES",
    "PROCESS_INFORMATION",
    "STARTUPINFOW",
    "SessionError",
    "SessionInfo",
    "build_command_line",
    "console_session_id",
    "executable_path",
    "launch_worker",
    "list_sessions",
    "select_capturable",
    "session_user_sid",
    "spawn_local",
    "worker_arguments",
]