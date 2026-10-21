"""Launch a process inside another Windows session.

A SYSTEM service cannot simply ``CreateProcess`` an interactive capture worker: the
child would stay in session 0 with no desktop. The sequence below is the supported
way to do it unattended, with no UAC prompt anywhere:

    WTSQueryUserToken(session)
      -> DuplicateTokenEx(TokenPrimary)
      -> CreateEnvironmentBlock
      -> CreateProcessAsUserW(winsta0\\default, CREATE_NO_WINDOW)
      -> DestroyEnvironmentBlock / CloseHandle

The service already runs as SYSTEM, which is the privilege this operation needs —
the operator granted it once, when the installer ran elevated.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
MAX_PATH = 260

#: ``MAXIMUM_ALLOWED`` lives in ``win32con`` on every pywin32 build; older code
#: referenced ``win32security.MAXIMUM_ALLOWED``, which does not exist and made
#: every launch raise AttributeError, so the worker silently fell back to session 0
#: where there is no desktop to capture.
MAXIMUM_ALLOWED = 0x02000000


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


def current_executable() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(sys.executable))


def worker_arguments(session_id: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return ["--worker", "--session", str(session_id)]
    # Running from source: python -m observatory_agent.main --worker --session N
    return ["-m", "observatory_agent.main", "--worker", "--session", str(session_id)]


def launch_worker(session_id: int) -> int | None:
    """Spawn the capture worker in ``session_id``; returns the PID or None."""

    executable = _session_executable(session_id)
    command_line = _build_command_line(executable, worker_arguments(session_id))
    return _create_process_as_user(session_id, command_line)


def spawn_local(arguments: list[str]) -> subprocess.Popen | None:
    """Fallback used in console mode and when no other session exists."""

    executable = current_executable()
    try:
        return subprocess.Popen(
            [executable, *arguments],
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        log.error("local spawn failed: %s", exc)
        return None


def _session_executable(session_id: int) -> str:
    return current_executable()


def _build_command_line(executable: str, arguments: list[str]) -> str:
    parts = [f'"{executable}"'] + [f'"{argument}"' if " " in argument else argument for argument in arguments]
    return " ".join(parts)


def _create_process_as_user(session_id: int, command_line: str) -> int | None:
    try:
        import win32con
        import win32process
        import win32security
        import win32ts
        import win32api
    except Exception as exc:  # noqa: BLE001
        log.error("pywin32 unavailable: %s", exc)
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    advapi32.DuplicateTokenEx.restype = wintypes.BOOL
    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,  # hExistingToken
        wintypes.DWORD,  # dwDesiredAccess
        ctypes.c_void_p,  # lpTokenAttributes (NULL is allowed)
        wintypes.DWORD,  # ImpersonationLevel
        wintypes.DWORD,  # TokenType
        ctypes.POINTER(wintypes.HANDLE),  # phNewToken
    ]
    token = None
    duplicated = None
    token_handle = None
    environment = ctypes.c_void_p()
    try:
        token = win32ts.WTSQueryUserToken(session_id)
        # Call the API directly: pywin32's DuplicateTokenEx rejects ``None`` for
        # TokenAttributes ("argument 3 must be int, not None") and there is no
        # SECURITY_ATTRIBUTES typedef exposed by ctypes, while the native call
        # accepts a NULL pointer for the default attributes.
        duplicated = wintypes.HANDLE()
        if not advapi32.DuplicateTokenEx(
            int(token),
            MAXIMUM_ALLOWED,
            None,
            win32security.SecurityImpersonation,
            win32security.TokenPrimary,
            ctypes.byref(duplicated),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_handle = duplicated.value
        if not userenv.CreateEnvironmentBlock(ctypes.byref(environment), token_handle, False):
            raise ctypes.WinError(ctypes.get_last_error())

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_HIDE
        info = PROCESS_INFORMATION()

        flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        ok = advapi32.CreateProcessAsUserW(
            token_handle,
            None,
            ctypes.c_wchar_p(command_line),
            None,
            None,
            False,
            flags,
            environment,
            None,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

        pid = int(info.dwProcessId)
        for handle in (info.hProcess, info.hThread):
            try:
                kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
        log.info("launched worker pid=%s in session=%s", pid, session_id)
        return pid
    except Exception as exc:  # noqa: BLE001
        log.warning("CreateProcessAsUser failed session=%s: %s", session_id, exc)
        return None
    finally:
        if environment:
            try:
                userenv.DestroyEnvironmentBlock(environment)
            except Exception:  # noqa: BLE001
                pass
        for handle in (token_handle, token):
            if handle:
                try:
                    win32api.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
        _ = (win32con, win32process)


__all__ = [
    "CREATE_NO_WINDOW",
    "current_executable",
    "launch_worker",
    "spawn_local",
    "worker_arguments",
]