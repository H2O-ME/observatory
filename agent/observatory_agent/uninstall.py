"""UAC-free removal.

Requirement: the only elevation in the product's life is the one UAC prompt during
installation. Everything after that — including a remote uninstall of an unattended
machine — must work without a prompt, so removal is performed by a detached helper
that inherits the service's SYSTEM token:

1. The service starts a copy of itself from ``%TEMP%`` with ``--purge``.
2. The service stops; the helper waits for the old PID to disappear.
3. The helper deletes the Windows service, the install directory, the data
   directory and any footprint, then schedules its own deletion for reboot.

Nothing here asks the user for anything.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import INSTALL_DIR, PRODUCT, PROGRAM_DATA, SERVICE_NAME

log = logging.getLogger(__name__)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
WAIT_TIMEOUT_S = 120


def spawn_purge_helper(wait_pid: int | None = None, install_dir: Path | None = None) -> int | None:
    """Copy ourselves to TEMP and start the detached purger. Returns its PID."""

    source = Path(sys.executable)
    helper = Path(tempfile.gettempdir()) / f"observatory-purge-{secrets.token_hex(4)}.exe"
    try:
        shutil.copy2(source, helper)
    except OSError as exc:
        log.error("could not stage uninstaller: %s", exc)
        return None

    arguments = [
        str(helper),
        "--purge",
        "--wait-pid",
        str(wait_pid if wait_pid is not None else os.getpid()),
        "--install-dir",
        str(install_dir or INSTALL_DIR),
    ]
    try:
        process = subprocess.Popen(
            arguments,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        log.info("uninstall helper started pid=%s", process.pid)
        return process.pid
    except OSError as exc:
        log.error("could not start uninstaller: %s", exc)
        return None


def purge(wait_pid: int, install_dir: str | None = None) -> int:
    """Helper mode: remove every trace. Must run as SYSTEM or elevated admin."""

    from .logging_setup import setup_logging

    setup_logging("uninstaller")
    log.info("uninstaller starting (waiting for pid %s)", wait_pid)

    _wait_for_process_exit(wait_pid, WAIT_TIMEOUT_S)
    _stop_service()
    _delete_service()
    _remove_footprint()
    _remove_tree(Path(install_dir or INSTALL_DIR))
    _remove_tree(Path(PROGRAM_DATA))
    _schedule_self_delete()

    log.info("uninstall finished")
    return 0


# ------------------------------------------------------------------ helpers ---
def _wait_for_process_exit(pid: int, timeout_s: float) -> None:
    if pid <= 0:
        return
    try:
        import win32api
        import win32event
        import win32process

        handle = win32api.OpenProcess(win32process.SYNCHRONIZE, False, pid)
        try:
            win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
        finally:
            win32api.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("wait for pid failed: %s", exc)
        time.sleep(2)


def _stop_service() -> None:
    try:
        import win32service
        import win32serviceutil

        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        if status == win32service.SERVICE_STOPPED:
            return
        win32serviceutil.StopService(SERVICE_NAME)
        deadline = time.time() + 60
        while time.time() < deadline:
            if win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1] == win32service.SERVICE_STOPPED:
                return
            time.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        log.debug("stop service failed: %s", exc)


def _delete_service() -> None:
    try:
        import win32service

        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            handle = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            try:
                win32service.DeleteService(handle)
                log.info("service %s deleted", SERVICE_NAME)
            finally:
                win32service.CloseServiceHandle(handle)
        except Exception as exc:  # noqa: BLE001
            log.debug("open service for delete failed: %s", exc)
        finally:
            win32service.CloseServiceHandle(manager)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not delete service: %s", exc)


def _remove_footprint() -> None:
    _remove_firewall_rules()
    _remove_defender_exclusion()
    _remove_registry_keys()


def _remove_firewall_rules() -> None:
    try:
        import win32com.client  # type: ignore[import-not-found]

        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        for rule in list(policy.Rules):
            try:
                if PRODUCT.lower() in str(rule.Name).lower():
                    policy.Rules.Remove(rule.Name)
                    log.info("removed firewall rule %s", rule.Name)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("firewall cleanup skipped: %s", exc)


def _remove_defender_exclusion() -> None:
    try:
        import subprocess as sp

        sp.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Remove-MpPreference -ExclusionPath '{INSTALL_DIR}' -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("defender cleanup skipped: %s", exc)


def _remove_registry_keys() -> None:
    try:
        import winreg

        for root, path in (
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{PRODUCT}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{PRODUCT}"),
        ):
            try:
                winreg.DeleteKey(root, path)
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("registry cleanup skipped: %s", exc)


def _remove_tree(root: Path) -> None:
    for attempt in range(6):
        if not root.exists():
            return
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            log.info("removed %s", root)
            return
        time.sleep(1.0 + attempt)


def _schedule_self_delete() -> None:
    try:
        import ctypes

        executable = str(Path(sys.executable))
        ctypes.windll.kernel32.MoveFileExW(executable, None, MOVEFILE_DELAY_UNTIL_REBOOT)
        log.info("scheduled self-deletion on reboot: %s", executable)
    except Exception as exc:  # noqa: BLE001
        log.debug("self-delete scheduling failed: %s", exc)


def interactive_uninstall() -> int:
    """``--uninstall`` from an elevated prompt; refuses silently otherwise."""

    if not _is_admin():
        print("卸载需要管理员权限。远程卸载由受控端服务自行完成，不会弹 UAC。")
        return 1

    pid = spawn_purge_helper(wait_pid=0)
    if pid is None:
        print("无法启动卸载程序。")
        return 1
    print(f"卸载程序已启动（pid {pid}），窗口将在几秒后关闭。")
    return 0


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["interactive_uninstall", "purge", "spawn_purge_helper"]