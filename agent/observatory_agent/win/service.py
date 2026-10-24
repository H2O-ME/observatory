"""SCM 生命周期、失败恢复动作、安装/删除服务，以及唯一的重启实现。

为什么旧实现错
--------------

* **A2 重启**：0.2.10 的 ``runner._run_service_command("restart_agent")`` 只把
  ``_stopping`` 置位并唤醒事件（旧 ``runner.py:585-587``），服务于是**正常**上报
  ``SERVICE_STOPPED``；SCM 的 failure actions 只在服务**非正常终止**（没有上报
  STOPPED）时触发，所以“重启受控端”实际是“把受控端关掉直到下次开机”。
  本模块的做法：先注册一次性的计划任务（秒级 StartBoundary），再把服务的 failure
  actions 配成 5s/15s/60s 重启，最后让进程以**非零退出码**终止且不向 SCM 上报
  STOPPED —— SCM 据此判定失败并恢复服务。任一机制可用即保证 ≤60s 重新上线；两个
  都不可用时**不退出**，向上抛 :class:`ServiceError`（宁可重启失败也不把设备弄下线）。

* **重复的 wait/stop**：旧 ``updater.py:350-386`` 与 ``uninstall.py:95-128`` 各抄了一份
  等进程退出/停服务的实现，且 updater 里硬编码 ``"ObservatoryAgent"`` 而不读常量。
  0.3.0 只有 :class:`ServiceController` 这一份（规格 §4.2 “win/service.py 一份”）。

约定
----

* 所有等待/重试的时间都通过构造函数注入（``now``/``sleep``/``process_alive``），
  纯规划函数（:func:`plan_restart` / :func:`restart_task_xml`）无 IO，可直接单测。
* 失败一律抛 :class:`ServiceError` 或返回 ``False`` 并记录日志；没有静默 except。

0.3.1（F31）应用对象契约
------------------------

:func:`_make_framework` 生成的服务宿主对应用对象只依赖两个必需方法（``start``/
``stop(timeout_s)``）和一个**可选**方法 ``is_alive() -> bool``：

* 应用若提供 ``is_alive``，服务主循环每 2s 轮询一次（``WaitForSingleObject`` 的
  超时节奏）；返回 ``False`` 表示运行线程已死、进程却还活着——继续上报 RUNNING
  只会变成僵尸服务。此时服务以**非零退出码**直接终止且不向 SCM 上报 STOPPED，
  让 SCM recovery / 一次性计划任务接管重启（与 A2 重启同一机制）。
  ``core/runtime.py`` 的 ServiceRuntime 0.3.1 会实现该方法（另一分片接线）。
* 不提供 ``is_alive`` 的应用行为与 0.3.0 完全一致（探活自动禁用）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..core.config import (
    SERVICE_DESCRIPTION,
    SERVICE_DISPLAY_NAME,
    SERVICE_NAME,
)
from . import WindowsOnlyOperation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- identity ---
# 服务标识只有一个来源：``core.config``（与安装模板、旧版本保持一致）。

#: ``StartServiceCtrlDispatcher`` 在“不是由 SCM 启动”时返回的 winerror。
#: 上层据此回退到 console 模式，而不是把双击启动当成失败。
NOT_STARTED_BY_SCM = 1063
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062

#: SCM 状态码（win32service 的值，写死以便非 Windows 上也能单测状态判断）。
SERVICE_STOPPED = 1
SERVICE_START_PENDING = 2
SERVICE_STOP_PENDING = 3
SERVICE_RUNNING = 4
SERVICE_CONTINUE_PENDING = 5
SERVICE_PAUSE_PENDING = 6
SERVICE_PAUSED = 7

#: 默认失败恢复动作：5s、15s、1min 各重启一次；一天后重置计数。
DEFAULT_RECOVERY_DELAYS_MS: tuple[int, ...] = (5_000, 15_000, 60_000)
RECOVERY_RESET_PERIOD_S = 86_400

DEFAULT_STOP_TIMEOUT_S = 60.0
DEFAULT_START_ATTEMPTS = 6
DEFAULT_START_SETTLE_S = 25.0
DEFAULT_START_CONFIRM_S = 3.0
DEFAULT_START_BACKOFF_S = 3.0

#: 0.3.1（F31）：停服预算与 SvcStop 上报的 waitHint 必须一致——慢停止超过 waitHint 且
#: checkpoint 停滞时会被 SCM 强杀。旧值 10s 太短（IO 繁忙时清理超时），统一提到 30s。
STOP_BUDGET_S = 30.0
STOP_WAIT_HINT_MS = 30_000
#: 收尾期间向 SCM 重报 STOP_PENDING（每次调用 checkpoint 自动 +1）的间隔。
STOP_CHECKPOINT_INTERVAL_S = 5.0

#: 一次性重启计划任务在目标时间之后最多保留一分钟（执行完自删）。
RESTART_TASK_TTL = "PT2M"
#: 任务执行超时；超过视为失败，避免残留任务。
RESTART_TASK_EXEC_LIMIT = "PT1M"

#: 计划任务的 XML 命名空间（schtasks /Create /XML 要求）。
_TASK_XMLNS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class ServiceError(RuntimeError):
    """SCM 操作失败；``step`` 标明失败步骤，``winerror`` 保留原始错误码。"""

    def __init__(self, message: str, *, step: str = "", winerror: int | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.winerror = winerror


@dataclass(frozen=True)
class ServiceIdentity:
    """服务标识；默认值与安装模板/旧版本保持一致。"""

    name: str = SERVICE_NAME
    display_name: str = SERVICE_DISPLAY_NAME
    description: str = SERVICE_DESCRIPTION


DEFAULT_IDENTITY = ServiceIdentity()


@dataclass(frozen=True)
class ServiceStatus:
    state: int
    pid: int = 0
    win32_exit_code: int = 0

    @property
    def is_running(self) -> bool:
        return self.state == SERVICE_RUNNING

    @property
    def is_stopped(self) -> bool:
        return self.state == SERVICE_STOPPED


class ServiceApplication(Protocol):
    """``win.service.run_service`` 需要的应用协议。

    ``start()`` 必须尽快返回（真正的循环由应用放到自己的线程/task 里）；
    ``stop(timeout_s)`` 必须幂等（SCM 的 stop 路径与 finally 可能各调一次）。

    可选方法 ``is_alive() -> bool``（0.3.1 F31，见模块 docstring 的契约）：服务主循环
    每 2s 轮询；返回 ``False`` 表示运行线程已死，服务将以非零码退出走 SCM recovery。
    """

    def start(self) -> None: ...

    def stop(self, timeout_s: float) -> None: ...


#: 应用工厂签名：拿到“请求服务停止”的回调（应用内部触发 remote shutdown 时用）。
AppFactory = Callable[[Callable[[str], None]], ServiceApplication]


# ----------------------------------------------------------------- helpers ---
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


def _service_modules() -> tuple[Any, Any, Any, Any]:
    """延迟导入 pywin32 服务模块（非 Windows 上 import 本模块不应失败）。"""

    _require_windows("服务控制")
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    return servicemanager, win32event, win32service, win32serviceutil


# ------------------------------------------------------------------- host ---
def _make_framework(identity: ServiceIdentity, app_factory: AppFactory, prepare: Callable[[], None] | None):
    servicemanager, win32event, win32service, win32serviceutil = _service_modules()

    class _ServiceHost(win32serviceutil.ServiceFramework):
        _svc_name_ = identity.name
        _svc_display_name_ = identity.display_name
        _svc_description_ = identity.description

        def __init__(self, args: Sequence[str]) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._application: ServiceApplication | None = None

        def SvcStop(self) -> None:
            # 控制处理线程只做“置事件 + 报状态”，真正的 stop 在 SvcDoRun 的 finally 里做；
            # 这样即使 stop 很慢也不会让 SCM 的 control handler 超时。
            # 0.3.1（F31）：必须带 waitHint 上报，否则慢停止（> 默认的隐式窗口）会被 SCM
            # 强杀。pywin32 的 ReportServiceStatus 没有 checkpoint 参数，但每次调用会把
            # 内部计数 +1，所以收尾循环周期性重报即等于递增 checkpoint。
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_WAIT_HINT_MS)
            log.info("service stop requested")
            win32event.SetEvent(self._stop_event)

        def SvcInterrogate(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        def SvcDoRun(self) -> None:
            if prepare is not None:
                prepare()
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            log.info("service starting pid=%s", os.getpid())
            self._application = app_factory(self._request_shutdown)
            try:
                self._application.start()
                while win32event.WaitForSingleObject(self._stop_event, 2_000) != win32event.WAIT_OBJECT_0:
                    # 0.3.1（F31）：借 2s 超时节奏轮询应用探活；返回 False 时进程以非零码
                    # 退出（不报 STOPPED），SCM recovery 会把服务拉起来。
                    self._check_application_alive()
            finally:
                log.info("service stopping")
                self._stop_application()
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STOPPED,
                    (self._svc_name_, ""),
                )

        def _check_application_alive(self) -> None:
            """0.3.1（F31）僵尸服务检测：应用 ``is_alive()`` 返回 False 则故障退出。

            契约见模块 docstring。探活调用本身失败按“还活着”处理——宁可误报活着，
            也不能把健康服务误杀重启。
            """

            application = self._application
            if application is None:
                return
            is_alive = getattr(application, "is_alive", None)
            if not callable(is_alive):
                return
            try:
                alive = bool(is_alive())
            except Exception:  # noqa: BLE001 - 探活失败按存活处理
                log.exception("application is_alive() probe failed; treating as alive")
                return
            if alive:
                return
            log.error("application reports dead; exiting nonzero so SCM recovery restarts the service")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_WAIT_HINT_MS)
            logging.shutdown()
            os._exit(1)

        def _stop_application(self) -> None:
            """0.3.1（F31）：停应用，期间周期向 SCM 重报 STOP_PENDING 递增 checkpoint。

            ``application.stop`` 可能阻塞到 ``STOP_BUDGET_S``；放到独立线程执行，本线程按
            :data:`STOP_CHECKPOINT_INTERVAL_S` 轮询，checkpoint 不会在 waitHint 内停滞，
            SCM 不会因“无进展”强杀慢停止。
            """

            application = self._application
            if application is None:
                return
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_WAIT_HINT_MS)
            done = threading.Event()
            error: list[BaseException] = []

            def _do_stop() -> None:
                try:
                    application.stop(timeout_s=STOP_BUDGET_S)
                except BaseException as exc:  # noqa: BLE001 - 停服失败也要走完收尾、上报 STOPPED
                    error.append(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=_do_stop, name="service-stop", daemon=True)
            worker.start()
            while not done.wait(STOP_CHECKPOINT_INTERVAL_S):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_WAIT_HINT_MS)
            if error:
                log.error("application.stop failed: %s", error[0])

        def _request_shutdown(self, reason: str) -> None:
            log.info("application asked to stop the service: %s", reason)
            win32event.SetEvent(self._stop_event)

    return _ServiceHost


def run_service(
    app_factory: AppFactory,
    *,
    identity: ServiceIdentity = DEFAULT_IDENTITY,
    prepare: Callable[[], None] | None = None,
) -> int:
    """把控制权交给 SCM 调度器。

    返回 ``0`` 表示服务正常退出；返回 :data:`NOT_STARTED_BY_SCM` 表示当前进程不是由
    SCM 拉起（调用方应回退到 console 模式）；其它失败抛 :class:`ServiceError`。

    ``prepare`` 在 ``SvcDoRun`` 最前面调用（上层用它初始化日志），保证日志先于任何
    业务代码落盘；本模块自身只写 logging，不碰全局 logging 配置。
    """

    _require_windows("作为服务运行")
    servicemanager, _win32event, _win32service, _win32serviceutil = _service_modules()
    framework = _make_framework(identity, app_factory, prepare)
    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(framework)
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:  # noqa: BLE001 - 这里必须分类：1063 有明确语义，其余是失败
        code = _winerror(exc)
        if code == NOT_STARTED_BY_SCM:
            log.info("process was not started by the SCM; caller should fall back to console mode")
            return NOT_STARTED_BY_SCM
        log.exception("service dispatcher failed")
        raise ServiceError(f"服务调度器失败: {exc}", step="StartServiceCtrlDispatcher", winerror=code) from exc
    return 0


# -------------------------------------------------------------- controller ---
class ServiceController:
    """服务查询/停止/启动/删除的唯一实现。

    时间全部可注入：

    * ``now``/``sleep`` 用于轮询等待（默认 ``time.monotonic`` / ``time.sleep``）；
    * ``process_alive`` 用于等待进程退出（默认走 Win32；测试可注入假的实现）。
    """

    def __init__(
        self,
        identity: ServiceIdentity = DEFAULT_IDENTITY,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        process_alive: Callable[[int], bool] | None = None,
    ) -> None:
        self.identity = identity
        self._now = now
        self._sleep = sleep
        self._process_alive = process_alive

    # -- status ------------------------------------------------------------
    def status(self) -> ServiceStatus:
        _require_windows("查询服务状态")
        _sm, _we, win32service, win32serviceutil = _service_modules()
        try:
            raw = win32serviceutil.QueryServiceStatus(self.identity.name)
        except Exception as exc:  # noqa: BLE001
            code = _winerror(exc)
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return ServiceStatus(state=SERVICE_STOPPED)
            raise ServiceError(f"查询服务状态失败: {exc}", step="QueryServiceStatus", winerror=code) from exc
        state = int(raw[1])
        exit_code = int(raw[3]) if len(raw) > 3 else 0
        return ServiceStatus(state=state, pid=self._process_id(), win32_exit_code=exit_code)

    def _process_id(self) -> int:
        """尽力取服务 PID；取不到返回 0（状态本身仍然有效）。"""

        try:
            import win32service

            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        except Exception:  # noqa: BLE001 - 仅用于展示，失败不代表状态无效
            log.debug("open scm for pid failed", exc_info=True)
            return 0
        try:
            handle = win32service.OpenService(manager, self.identity.name, win32service.SERVICE_QUERY_STATUS)
        except Exception:  # noqa: BLE001 - 服务可能刚被删除
            log.debug("open service for pid failed", exc_info=True)
            return 0
        try:
            info = win32service.QueryServiceStatusEx(handle, win32service.SC_STATUS_PROCESS_INFO)
        except Exception:  # noqa: BLE001 - 老系统/老 pywin32 可能没有该导出
            log.debug("QueryServiceStatusEx failed", exc_info=True)
            return 0
        finally:
            win32service.CloseServiceHandle(handle)
            win32service.CloseServiceHandle(manager)
        if isinstance(info, dict):
            return int(info.get("ProcessId") or 0)
        return 0

    def is_running(self) -> bool:
        return self.status().is_running

    def wait_for_state(self, target: int, *, timeout_s: float, poll_s: float = 0.5) -> bool:
        deadline = self._now() + timeout_s
        while self._now() < deadline:
            if self.status().state == target:
                return True
            self._sleep(min(poll_s, max(0.0, deadline - self._now())))
        return self.status().state == target

    # -- stop / start ------------------------------------------------------
    def stop(self, *, timeout_s: float = DEFAULT_STOP_TIMEOUT_S, poll_s: float = 0.5) -> bool:
        """停止服务；已停止/不存在视为成功。返回 True 表示最终处于 STOPPED。"""

        _require_windows("停止服务")
        _sm, _we, _winservice, win32serviceutil = _service_modules()
        try:
            win32serviceutil.StopService(self.identity.name)
        except Exception as exc:  # noqa: BLE001
            code = _winerror(exc)
            if code in (ERROR_SERVICE_NOT_ACTIVE, ERROR_SERVICE_DOES_NOT_EXIST):
                log.info("service %s already stopped (%s)", self.identity.name, code)
                return True
            raise ServiceError(f"停止服务失败: {exc}", step="StopService", winerror=code) from exc
        if not self.wait_for_state(SERVICE_STOPPED, timeout_s=timeout_s, poll_s=poll_s):
            log.error("service %s did not reach STOPPED within %.1fs", self.identity.name, timeout_s)
            return False
        return True

    def start(
        self,
        *,
        attempts: int = DEFAULT_START_ATTEMPTS,
        settle_s: float = DEFAULT_START_SETTLE_S,
        confirm_s: float = DEFAULT_START_CONFIRM_S,
        backoff_s: float = DEFAULT_START_BACKOFF_S,
        poll_s: float = 0.5,
    ) -> bool:
        """启动服务并在 RUNNING 后额外确认 ``confirm_s`` 秒仍在运行。

        旧实现只调一次 ``StartService`` 且不复查，SCM 还在 settling 或杀软刚写完 exe
        时就会失败且没人重试（旧 ``updater.py:388-454`` 的注释记录了这个事故）。
        """

        _require_windows("启动服务")
        _sm, _we, winservice, win32serviceutil = _service_modules()
        for attempt in range(1, attempts + 1):
            status = self._safe_status()
            if status == SERVICE_RUNNING:
                return True
            if status not in (SERVICE_START_PENDING, SERVICE_CONTINUE_PENDING):
                try:
                    win32serviceutil.StartService(self.identity.name)
                    log.info("StartService issued (attempt %s)", attempt)
                except Exception as exc:  # noqa: BLE001
                    code = _winerror(exc)
                    if code != ERROR_SERVICE_ALREADY_RUNNING:
                        log.warning("StartService attempt %s failed: %s (winerror=%s)", attempt, exc, code)
            deadline = self._now() + settle_s
            while self._now() < deadline:
                self._sleep(min(poll_s, max(0.0, deadline - self._now())))
                status = self._safe_status()
                if status == SERVICE_RUNNING:
                    self._sleep(confirm_s)
                    if self._safe_status() == SERVICE_RUNNING:
                        log.info("service %s started and stayed up (attempt %s)", self.identity.name, attempt)
                        return True
                    log.warning("service %s came up then dropped (attempt %s)", self.identity.name, attempt)
                    break
                if status == SERVICE_STOPPED:
                    break
            log.warning("service %s not running after attempt %s, retrying", self.identity.name, attempt)
            self._sleep(backoff_s * attempt)
        log.error("service %s could not be started after %s attempts", self.identity.name, attempts)
        return False

    def _safe_status(self) -> int | None:
        try:
            return self.status().state
        except ServiceError:
            log.warning("query service status failed", exc_info=True)
            return None

    def remove(self) -> bool:
        """删除服务注册；不存在视为成功。"""

        _require_windows("删除服务")
        _sm, _we, win32service, _wsu = _service_modules()
        try:
            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(f"打开服务管理器失败: {exc}", step="OpenSCManager", winerror=_winerror(exc)) from exc
        try:
            try:
                handle = win32service.OpenService(manager, self.identity.name, win32service.SERVICE_ALL_ACCESS)
            except Exception as exc:  # noqa: BLE001
                code = _winerror(exc)
                if code == ERROR_SERVICE_DOES_NOT_EXIST:
                    log.info("service %s does not exist", self.identity.name)
                    return True
                raise ServiceError(
                    f"打开服务失败: {exc}", step="OpenService", winerror=code
                ) from exc
            try:
                win32service.DeleteService(handle)
                log.info("service %s deleted", self.identity.name)
                return True
            finally:
                win32service.CloseServiceHandle(handle)
        finally:
            win32service.CloseServiceHandle(manager)

    def configure_recovery(self, *, delays_ms: Sequence[int] = DEFAULT_RECOVERY_DELAYS_MS) -> bool:
        """把 failure actions 配成“立即重启”，这是 A2 的主机制。

        返回 False 表示配置失败（例如权限不足或服务不存在）；调用方会退到计划任务。
        """

        _require_windows("配置服务恢复动作")
        _sm, _we, win32service, _wsu = _service_modules()
        actions = [(win32service.SC_ACTION_RESTART, int(delay)) for delay in delays_ms]
        try:
            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        except Exception as exc:  # noqa: BLE001
            log.warning("open scm for recovery config failed: %s", exc)
            return False
        try:
            try:
                handle = win32service.OpenService(
                    manager, self.identity.name, win32service.SERVICE_CHANGE_CONFIG
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("open service for recovery config failed: %s", exc)
                return False
            try:
                win32service.ChangeServiceConfig2(
                    handle,
                    win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                    {
                        "ResetPeriod": RECOVERY_RESET_PERIOD_S,
                        "RebootMsg": "",
                        "Command": "",
                        "Actions": actions,
                    },
                )
                return True
            finally:
                win32service.CloseServiceHandle(handle)
        except Exception as exc:  # noqa: BLE001
            log.warning("configure recovery failed: %s", exc)
            return False
        finally:
            win32service.CloseServiceHandle(manager)

    # -- process wait ------------------------------------------------------
    def wait_for_process_exit(self, pid: int, *, timeout_s: float, poll_s: float = 0.25) -> bool:
        """等待进程退出；超时返回 False。``pid <= 0`` 视为已退出。"""

        if pid <= 0:
            return True
        if self._process_alive is not None:
            deadline = self._now() + timeout_s
            while self._now() < deadline:
                if not self._process_alive(pid):
                    return True
                self._sleep(min(poll_s, max(0.0, deadline - self._now())))
            return not self._process_alive(pid)
        _require_windows("等待进程退出")
        try:
            import win32api
            import win32event
            import win32process

            handle = win32api.OpenProcess(win32process.SYNCHRONIZE, False, pid)
        except Exception as exc:  # noqa: BLE001 - 进程可能已经不存在
            log.debug("open process %s failed (%s); treating as exited", pid, exc)
            return True
        try:
            result = win32event.WaitForSingleObject(handle, int(max(0.0, timeout_s) * 1000))
            return result == win32event.WAIT_OBJECT_0
        finally:
            win32api.CloseHandle(handle)


# --------------------------------------------------------------- install -----
def install_service(
    executable: str | Path,
    *,
    identity: ServiceIdentity = DEFAULT_IDENTITY,
) -> None:
    """注册（或更新）服务并写 failure actions / delayed auto start。

    旧 ``service.py`` 的 ``ChangeServiceConfig2`` SID 分支传的是裸 int，pywin32 期望
    dict，异常被 ``pass`` 吞掉，等于什么都没配；新实现**不配 SID**（默认就是无服务
    SID，语义等价），并把失败动作配成 A2 依赖的立即重启。
    """

    _require_windows("安装服务")
    _sm, _we, win32service, _wsu = _service_modules()
    binary_path = _quoted_binary_path(executable)
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(f"打开服务管理器失败（需要管理员权限）: {exc}", step="OpenSCManager", winerror=_winerror(exc)) from exc
    try:
        handle = None
        try:
            handle = win32service.CreateService(
                manager,
                identity.name,
                identity.display_name,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                binary_path,
                None,
                0,
                None,
                None,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            code = _winerror(exc)
            if code != 1073:  # ERROR_SERVICE_EXISTS
                raise ServiceError(f"创建服务失败: {exc}", step="CreateService", winerror=code) from exc
            try:
                handle = win32service.OpenService(manager, identity.name, win32service.SERVICE_ALL_ACCESS)
                win32service.ChangeServiceConfig(
                    handle,
                    win32service.SERVICE_NO_CHANGE,
                    win32service.SERVICE_AUTO_START,
                    win32service.SERVICE_NO_CHANGE,
                    binary_path,
                    None,
                    0,
                    None,
                    None,
                    None,
                    identity.display_name,
                )
            except Exception as inner:  # noqa: BLE001
                raise ServiceError(
                    f"更新已有服务失败: {inner}", step="ChangeServiceConfig", winerror=_winerror(inner)
                ) from inner
        if handle is None:  # pragma: no cover - CreateService 成功时句柄必非空
            raise ServiceError("创建服务后未拿到句柄", step="CreateService")
        try:
            _configure_service(handle, identity)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(manager)


def _quoted_binary_path(executable: str | Path) -> str:
    path = str(executable).strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if not path:
        raise ServiceError("可执行文件路径为空", step="install")
    return f'"{path}"'


def _configure_service(handle: Any, identity: ServiceIdentity) -> None:
    _sm, _we, win32service, _wsu = _service_modules()
    try:
        win32service.ChangeServiceConfig2(
            handle, win32service.SERVICE_CONFIG_DESCRIPTION, identity.description
        )
    except Exception as exc:  # noqa: BLE001 - 描述失败不影响服务可用性
        log.warning("set service description failed: %s", exc)
    try:
        win32service.ChangeServiceConfig2(
            handle,
            win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
            {"DelayedAutostart": True},
        )
    except Exception as exc:  # noqa: BLE001 - 老系统可能不支持延迟启动
        log.warning("set delayed auto start failed: %s", exc)
    try:
        win32service.ChangeServiceConfig2(
            handle,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": RECOVERY_RESET_PERIOD_S,
                "RebootMsg": "",
                "Command": "",
                "Actions": [(win32service.SC_ACTION_RESTART, ms) for ms in DEFAULT_RECOVERY_DELAYS_MS],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 恢复动作是重启的兜底，失败要显式记录
        log.error("set failure actions failed (A2 restart will rely on the scheduled task): %s", exc)


# --------------------------------------------------------------- restart -----
@dataclass(frozen=True)
class RestartPlan:
    """一次性重启任务的可测试描述（纯数据）。"""

    task_name: str
    start_boundary: str
    action: tuple[str, ...]
    description: str


def plan_restart(
    *,
    now: datetime,
    delay_s: int = 10,
    identity: ServiceIdentity = DEFAULT_IDENTITY,
) -> RestartPlan:
    """规划一个秒级的一次性重启任务。

    ``now`` 必须是**本地时间**（Task Scheduler 的 StartBoundary 不带时区）。
    ``delay_s`` 限制在 1..3600，保证“≤60s 重新上线”的默认值 10s 有意义。
    """

    if not 1 <= int(delay_s) <= 3_600:
        raise ValueError(f"delay_s 必须在 1..3600 之间，收到 {delay_s!r}")
    boundary = now.replace(microsecond=0) + timedelta(seconds=int(delay_s))
    return RestartPlan(
        task_name=f"{identity.name}-Restart",
        start_boundary=boundary.strftime("%Y-%m-%dT%H:%M:%S"),
        action=("sc.exe", "start", identity.name),
        description=f"One-shot restart of {identity.display_name}",
    )


def restart_task_xml(plan: RestartPlan) -> str:
    """生成 schtasks /Create /XML 所需的最小任务定义（纯字符串，可单测）。"""

    arguments = " ".join(_quote_arg(part) for part in plan.action[1:])
    command = plan.action[0]
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{_TASK_XMLNS}">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{_xml_escape(plan.description)}</Description>\n"
        f"    <URI>\\{_xml_escape(plan.task_name)}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <TimeTrigger>\n"
        f"      <StartBoundary>{plan.start_boundary}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <UserId>S-1-5-18</UserId>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        f"    <ExecutionTimeLimit>{RESTART_TASK_EXEC_LIMIT}</ExecutionTimeLimit>\n"
        f"    <DeleteExpiredTaskAfter>{RESTART_TASK_TTL}</DeleteExpiredTaskAfter>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(command)}</Command>\n"
        f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _quote_arg(value: str) -> str:
    if not value or any(ch in value for ch in ' \t"'):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def register_restart_task(
    plan: RestartPlan,
    *,
    tmp_dir: str | Path,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool:
    """用 ``schtasks.exe /Create /XML`` 注册任务；返回是否成功。

    失败只记录日志并返回 False（调用方还有 SCM recovery 这条主路径），不抛异常——
    但绝不静默：日志里带上 schtasks 的 stderr。
    """

    _require_windows("注册重启计划任务")
    directory = Path(tmp_dir)
    directory.mkdir(parents=True, exist_ok=True)
    xml_path = directory / f"{plan.task_name}.xml"
    try:
        xml_path.write_text(restart_task_xml(plan), encoding="utf-16")
        completed = run(
            ["schtasks.exe", "/Create", "/TN", plan.task_name, "/XML", str(xml_path), "/F"],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("could not register restart task %s: %s", plan.task_name, exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            xml_path.unlink()
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        log.error("schtasks rejected restart task %s (rc=%s): %s", plan.task_name, completed.returncode, stderr)
        return False
    log.info("restart task %s registered for %s", plan.task_name, plan.start_boundary)
    return True


def request_restart(
    controller: ServiceController,
    *,
    tmp_dir: str | Path,
    now: Callable[[], datetime] = datetime.now,
    delay_s: int = 10,
    register: Callable[..., bool] = register_restart_task,
    enable_recovery: Callable[..., bool] | None = None,
    exit_process: Callable[[int], None] = os._exit,
    shutdown_logging: Callable[[], None] = logging.shutdown,
) -> None:
    """A2 的唯一实现：ReloadA（SCM recovery）+ ReloadB（一次性计划任务）。

    只有**两个机制都不可用**时抛 :class:`ServiceError`，让上层上报 ``restart_failed``
    并继续运行——重启失败不能变成“把服务停掉”。
    """

    plan = plan_restart(now=now(), delay_s=delay_s, identity=controller.identity)
    registered = register(plan, tmp_dir=tmp_dir)
    recovery = (enable_recovery or controller.configure_recovery)()
    if not registered and not recovery:
        raise ServiceError(
            "无法注册任何重启机制（计划任务与 SCM recovery 都失败），拒绝停服",
            step="request_restart",
        )
    log.warning(
        "restarting via %s; exiting without reporting STOPPED so SCM treats this as a failure",
        " ".join(
            mechanism
            for mechanism, ok in (("scm-recovery", recovery), ("scheduled-task", registered))
            if ok
        ),
    )
    shutdown_logging()
    exit_process(1)


# ------------------------------------------------------------ uninstall svc ---
def remove_service(identity: ServiceIdentity = DEFAULT_IDENTITY) -> bool:
    """删除服务注册（uninstall helper 用；不存在视为成功）。"""

    return ServiceController(identity).remove()


__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_RECOVERY_DELAYS_MS",
    "NOT_STARTED_BY_SCM",
    "SERVICE_CONTINUE_PENDING",
    "SERVICE_PAUSED",
    "SERVICE_PAUSE_PENDING",
    "SERVICE_RUNNING",
    "SERVICE_START_PENDING",
    "SERVICE_STOPPED",
    "SERVICE_STOP_PENDING",
    "AppFactory",
    "RestartPlan",
    "ServiceApplication",
    "ServiceController",
    "ServiceError",
    "ServiceIdentity",
    "ServiceStatus",
    "install_service",
    "plan_restart",
    "register_restart_task",
    "remove_service",
    "request_restart",
    "restart_task_xml",
    "run_service",
]