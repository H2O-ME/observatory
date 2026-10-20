"""Windows service host.

The service control loop stays in the main thread and only forwards a stop signal;
all real work happens on the runner thread. That separation is why a stuck network
call can no longer make the service unresponsive to ``sc stop``.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from .config import (
    SERVICE_DESCRIPTION,
    SERVICE_DISPLAY_NAME,
    SERVICE_NAME,
    ensure_dirs,
)
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _load_win32():
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    return servicemanager, win32event, win32service, win32serviceutil


if sys.platform == "win32":
    servicemanager, win32event, win32service, win32serviceutil = _load_win32()

    class ObservatoryService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._runner = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            log.info("service stop requested")
            win32event.SetEvent(self._stop_event)
            runner = self._runner
            if runner is not None:
                runner.stop(timeout=8)

        def SvcInterrogate(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        def SvcDoRun(self) -> None:
            ensure_dirs()
            setup_logging("agent")
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            from .runner import AgentRunner

            log.info("service starting pid=%s", os.getpid())
            self._runner = AgentRunner(service_mode=True)
            self._runner.on_shutdown = self._on_shutdown_requested
            self._runner.start()

            try:
                while True:
                    result = win32event.WaitForSingleObject(self._stop_event, 2000)
                    if result == win32event.WAIT_OBJECT_0:
                        break
            finally:
                log.info("service stopping")
                self._runner.stop(timeout=10)
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STOPPED,
                    (self._svc_name_, ""),
                )

        def _on_shutdown_requested(self, reason: str) -> None:
            log.info("runner asked to stop service: %s", reason)
            win32event.SetEvent(self._stop_event)


def run_service() -> int:
    """Hand control to the service dispatcher; falls back to console mode."""

    if sys.platform != "win32":
        print("受控端只能在 Windows 上作为服务运行。")
        return 1
    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ObservatoryService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    except Exception as exc:  # noqa: BLE001
        winerror = getattr(exc, "winerror", None)
        if winerror == 1063:
            # Not started by the SCM (double-clicked / run from a shell).
            from .main import run_console

            return run_console()
        print(f"无法作为服务启动: {exc}")
        return 1


def install_service(executable: str | None = None) -> int:
    executable = executable or sys.executable
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
    except Exception as exc:  # noqa: BLE001
        print(f"无法打开服务管理器（需要管理员权限）: {exc}")
        return 1

    handle = None
    try:
        handle = win32service.CreateService(
            manager,
            SERVICE_NAME,
            SERVICE_DISPLAY_NAME,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            f'"{executable}"',
            None,
            0,
            None,
            None,
            None,
        )
    except Exception as exc:  # noqa: BLE001
        winerror = getattr(exc, "winerror", None)
        if winerror == 1073:  # already installed
            try:
                handle = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
                win32service.ChangeServiceConfig(
                    handle,
                    win32service.SERVICE_NO_CHANGE,
                    win32service.SERVICE_AUTO_START,
                    win32service.SERVICE_NO_CHANGE,
                    f'"{executable}"',
                    None,
                    0,
                    None,
                    None,
                    None,
                    SERVICE_DISPLAY_NAME,
                )
            except Exception as inner:  # noqa: BLE001
                print(f"更新已有服务失败: {inner}")
                return 1
        else:
            print(f"创建服务失败: {exc}")
            return 1

    try:
        _configure(handle)
    except Exception as exc:  # noqa: BLE001
        log.warning("service configuration incomplete: %s", exc)
    finally:
        if handle is not None:
            win32service.CloseServiceHandle(handle)
        win32service.CloseServiceHandle(manager)

    print(f"服务 {SERVICE_NAME} 已安装：{executable}")
    return 0


def _configure(handle) -> None:
    win32service.ChangeServiceConfig2(handle, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESCRIPTION)
    try:
        win32service.ChangeServiceConfig2(
            handle, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, {"DelayedAutostart": True}
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        win32service.ChangeServiceConfig2(
            handle,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": [
                    (win32service.SC_ACTION_RESTART, 5000),
                    (win32service.SC_ACTION_RESTART, 15000),
                    (win32service.SC_ACTION_RESTART, 60000),
                ],
            },
        )
    except Exception:  # noqa: BLE001
        pass
    # The agent must be able to stop itself during an update or a remote uninstall;
    # the SCM would otherwise wait for a graceful stop that never comes.
    try:
        win32service.ChangeServiceConfig2(
            handle, win32service.SERVICE_CONFIG_SERVICE_SID_INFO, win32service.SERVICE_SID_TYPE_UNRESTRICTED
        )
    except Exception:  # noqa: BLE001
        pass


def start_service() -> int:
    try:
        win32serviceutil.StartService(SERVICE_NAME)
        print(f"服务 {SERVICE_NAME} 已启动")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"启动服务失败: {exc}")
        return 1


def stop_service() -> int:
    try:
        win32serviceutil.StopService(SERVICE_NAME)
        print(f"服务 {SERVICE_NAME} 已停止")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"停止服务失败: {exc}")
        return 1


def service_status() -> dict:
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        return {"state": int(status[1]), "pid": int(status[7]) if len(status) > 7 else 0}
    except Exception as exc:  # noqa: BLE001
        return {"state": -1, "error": str(exc)}


def is_installed() -> bool:
    return int(service_status().get("state", -1)) >= 0


def wait_for_state(target: int, timeout_s: float = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if int(service_status().get("state", -1)) == target:
            return True
        time.sleep(0.5)
    return False


__all__ = [
    "ObservatoryService",
    "install_service",
    "is_installed",
    "run_service",
    "service_status",
    "start_service",
    "stop_service",
    "wait_for_state",
]
_ = Path