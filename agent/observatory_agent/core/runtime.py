"""受控端运行时装配：服务进程（链路 + 命令分发 + 会话/worker 管理）与 worker 进程。

为什么需要这个模块
------------------
规格 §4.2 要求"服务进程持有链路与命令分发；worker 只做采集/输入/文件；两者用
同一信封协议走管道"。``win/service.py`` 只提供 SCM 宿主（它接收一个
``ServiceApplication`` 工厂），``win/pipe.py`` 只提供管道，``tasks/*`` 只提供动作
处理器——把这三者装配成"一条链路"的地方就是本模块。0.2.10 的等价物是
``runner.py`` + ``worker.py`` 两个大文件，这里把它们拆成两个职责清晰的运行时：

- :class:`ServiceRuntime`（SYSTEM 会话）：唯一 ``Link``、唯一 ``CommandDispatcher``、
  会话扫描与 worker 拉起、worker 消息 → 链路信封的翻译、服务端命令执行。
- :class:`WorkerRuntime`（用户会话）：``PipeClient`` + 唯一 ``CaptureEngine`` +
  任务处理器；它**不持有链路**，只跟服务进程说管道信封。

内部管道协议（``win.pipe`` 的 payload 头；与 v3 信封同构）
----------------------------------------------------------
服务 → worker（v3 信封）::

    t=config   body={"config": {...}}
    t=command  body={"command_id","type","payload","deadline_ms"}
    t=control  body={"action":"start|input|stop","session","fps","quality","ev"}
    t=ping     body={}
    t=stop     body={"reason"}            # 仅管道内部使用（不是对外协议类型）

worker → 服务：

    t=hello     body={"agent_version","boot_id","session_id","capture",...}
    t=heartbeat body={"session_id","capture","control_active","device_time_ms"}
    t=result    body=CommandResult.to_body(...)      # 原样转发到 link.send("result")
    t=event     body={"event","data"}                # 原样转发到 link.send("event")
    t=pong      body={}
    二进制帧    头 {"t":"frame"|"ctrl", ...} + JPEG 载荷

时间与注入：服务运行时接受 ``store``/``link_factory``/``session_provider`` 等注入点，
单测可以不起真实 Win32/网络；否则全部走 ``win/`` 与 ``core/link`` 的真实实现。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tasks.input import InputState
from . import sysinfo
from .commands import ERROR_BAD_REQUEST, Command, CommandDispatcher, CommandResult, expired_result
from .config import (
    DEFAULT_SETTINGS,
    INSTALL_DIR,
    STAGING_DIR,
    ConfigStore,
    ensure_dirs,
    outbox_limits_from_env,
    outbox_path,
    resolve_enroll_key,
    sanitize_settings,
)
from .envelope import EnvelopeError, EnvelopeTooLarge, envelope_from_dict, new_envelope, new_id
from .link import PROBE_BASE_S, Link, LinkClosed
from .logging import read_tail, setup_logging
from .outbox import Outbox, OutboxFull, SqliteOutboxStore

log = logging.getLogger(__name__)

#: 会话扫描间隔（秒）：与 0.2.10 一致，5s 足够发现登录/注销。
SESSION_SCAN_S = 5.0
#: worker 心跳/采集健康上报间隔（秒）。
WORKER_STATE_INTERVAL_S = 10
#: 采集健康超过这个时间没更新就视为"没有活 worker"（规格 presence 的输入）。
CAPTURE_HEALTH_STALE_MS = 60_000
#: worker 拉起失败后的递进冷却：30s → 60s → … → 30 分钟。
LAUNCH_COOLDOWN_S = 30.0
LAUNCH_BACKOFF_MAX_S = 1800.0
#: 重启/更新/卸载前给上行信封留出的发送时间（之后进程可能立即退出）。
RESULT_FLUSH_S = 1.0
#: 控制会话 TTL 的兜底清理间隔（服务端会发 control stop，这里只防泄漏）。
CONTROL_REAP_S = 300.0
#: 控制会话租约的默认/上下限（秒）。服务端在 ``control`` 信封里下发 ``ttl_s``
#: （``console/control.py`` 的 grant TTL，默认 600s），受控端必须**本地**也执行
#: 到期——只靠服务端定时器/stop 报文时，一条丢掉的 stop 就能让画面与输入无限
#: 期地留在"控制中"（A3）。上限与 ``MAX_TTL_S`` 对齐，下限防止 0/负数。
CONTROL_TTL_DEFAULT_S = 600.0
CONTROL_TTL_MIN_S = 15.0
CONTROL_TTL_MAX_S = 3600.0
#: 租约到期检查的粒度（秒）：控制循环每一帧都查，空闲时也至少每这么多秒查一次。
CONTROL_LEASE_CHECK_S = 1.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _monotonic_ms() -> int:
    """单调毫秒时钟（A6：worker 侧命令预算用它，墙钟跳变不影响超时判定）。"""

    return int(time.monotonic() * 1000)


#: 更新包内版本文件（``agent/build/build.ps1`` 把仓库 VERSION 放在 zip 根）。
PACKAGE_VERSION_FILE = "VERSION"


def read_package_version(archive: Path) -> str:
    """读更新包根部的 ``VERSION``（纯函数，便于单测/拒绝错包）。

    返回去空白后的版本字符串；包损坏、缺文件、非 UTF-8 时返回空串，由调用方
    决定拒绝升级（不允许"猜一个版本继续装"）。
    """

    try:
        with zipfile.ZipFile(archive) as bundle:
            try:
                raw = bundle.read(PACKAGE_VERSION_FILE)
            except KeyError:
                return ""
    except (OSError, zipfile.BadZipFile):
        return ""
    return raw.decode("utf-8", "replace").strip()


def _session_dict(item: Any) -> dict[str, Any]:
    """把 ``win.session.SessionInfo``（或字典）统一成 JSON 形状。"""

    if isinstance(item, dict):
        data = dict(item)
    else:
        data = {
            "session_id": int(getattr(item, "session_id", 0) or 0),
            "username": str(getattr(item, "username", "") or ""),
            "state": str(getattr(item, "state", "") or ""),
            "station": str(getattr(item, "station", "") or ""),
            "is_console": bool(getattr(item, "is_console", False)),
        }
    data["capturable"] = bool(str(data.get("username") or "").strip())
    return data


def _launch_backoff(failures: int) -> float:
    """连续拉起失败 ``failures`` 次后的冷却秒数（与 0.2.10 同一函数）。"""

    if failures <= 0:
        return 0.0
    exponent = min(failures - 1, 16)
    return min(LAUNCH_COOLDOWN_S * (2**exponent), LAUNCH_BACKOFF_MAX_S)


def _pipe_failure(exc: BaseException) -> dict[str, Any]:
    """把管道写失败翻译成 ``CommandResult`` 的字段（A1）。

    超限（``EnvelopeTooLarge`` / ``PipePayloadTooLarge``）是**输入太大**，不是
    管道坏了：回一条 ``BAD_REQUEST`` + 可读原因，服务端/前端能据此提示"减小
    分片"，而不是看到一条笼统的断链错误或干脆等到超时。其余异常仍按断链处理。
    """

    if isinstance(exc, EnvelopeTooLarge):
        return {
            "error": f"命令数据超过管道单帧上限，请减小分片后重试：{exc}",
            "error_code": ERROR_BAD_REQUEST,
        }
    return {"error": f"与用户会话的管道已断开: {exc}"}


# ------------------------------------------------------------ 采集健康聚合 ---
class CaptureHealthAggregator:
    """把各 worker 上报的采集健康聚合成服务端看到一个 ``state.capture``。

    0.2.10 是服务进程自己根据管道连接数推断（``runner._capture_health``），
    worker 手里的真实采集错误根本没有出口。0.3.0 的单一来源是
    ``CaptureEngine.health()``（worker 进程），服务端只做聚合。
    """

    def __init__(self) -> None:
        self._by_session: dict[int, dict[str, Any]] = {}

    def update(
        self,
        session_id: int,
        health: dict[str, Any] | None,
        *,
        control_active: bool,
        now_ms: int,
    ) -> None:
        if not isinstance(health, dict):
            health = {"state": "starting", "reason": "worker 尚未上报采集健康"}
        self._by_session[int(session_id)] = {
            **health,
            "control_active": bool(control_active),
            "updated_ms": int(now_ms),
        }

    def remove(self, session_id: int) -> None:
        self._by_session.pop(int(session_id), None)

    def snapshot(self, now_ms: int) -> dict[str, Any]:
        fresh = {
            sid: value
            for sid, value in self._by_session.items()
            if now_ms - int(value.get("updated_ms") or 0) <= CAPTURE_HEALTH_STALE_MS
        }
        workers = sorted(fresh)
        # at_ms lets the server run its own capture_stale_s check (0.3.1 F3);
        # without it the server-side threshold was a no-op for v3 devices.
        at_ms = int(now_ms)
        if not fresh:
            return {
                "state": "no_session",
                "reason": "当前没有已登录的交互式桌面（控制台未登录或已注销）",
                "workers": [],
                "sessions": 0,
                "at_ms": at_ms,
            }
        blocked = [value for value in fresh.values() if value.get("state") == "blocked"]
        if blocked:
            return {
                "state": "blocked",
                "reason": str(blocked[0].get("reason") or "采集失败"),
                "workers": workers,
                "sessions": len(fresh),
                "at_ms": at_ms,
            }
        healthy = [value for value in fresh.values() if value.get("state") == "ok"]
        if healthy:
            return {"state": "ok", "reason": "", "workers": workers, "sessions": len(fresh), "at_ms": at_ms}
        return {
            "state": "starting",
            "reason": str(next(iter(fresh.values())).get("reason") or "正在启动采屏进程"),
            "workers": workers,
            "sessions": len(fresh),
            "at_ms": at_ms,
        }


# ------------------------------------------------------------ 控制租约 ---
@dataclass(frozen=True)
class ControlLease:
    """一个控制会话的**本地**租约（A3）。

    ``expires_at`` 用单调时钟（``ServiceRuntime._control_clock``）。服务端确实
    会下发 ``ttl_s`` 并在到期时发 stop + 关 WS，但受控端不能把"控制一定会被
    正确停止"寄托在一条可能丢失的报文上：丢一条 stop 就等于画面无限期在线、
    输入无限期可注入。本地租约是第二道闸门。
    """

    token: str
    session_id: int
    expires_at: float

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def remaining_ms(self, now: float) -> int:
        return max(0, int((self.expires_at - now) * 1000))


# ------------------------------------------------------------- 服务运行时 ---
class ServiceRuntime:
    """服务进程运行时；实现 ``win.service.ServiceApplication`` 协议。"""

    def __init__(
        self,
        *,
        request_shutdown: Any | None = None,
        store: ConfigStore | None = None,
        service_mode: bool = True,
        link_factory: Any | None = None,
    ) -> None:
        self._request_shutdown_cb = request_shutdown
        self._store = store or ConfigStore()
        self._service_mode = bool(service_mode)
        self._link_factory = link_factory
        self._link: Link | None = None
        self._outbox: Outbox | None = None
        self._dispatcher = CommandDispatcher()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_flag = threading.Event()
        self._pipes: dict[int, Any] = {}
        self._processes: dict[int, int] = {}
        self._local_workers: dict[int, Any] = {}
        self._launch_failures: dict[int, tuple[int, float]] = {}
        self._sessions: list[Any] = []
        self._session_state: dict[int, dict[str, Any]] = {}
        #: A3：控制会话租约表（token → 本地租约，单调时钟到期）。
        self._controls: dict[str, ControlLease] = {}
        self._health = CaptureHealthAggregator()
        self._updating = False
        self._update_running = False
        self._update_task: Any = None
        self._started_at_ms = _now_ms()
        self._sysinfo_cache: dict[str, Any] = {}
        self._last_launch_log_s: dict[int, float] = {}
        #: 0.3.1（F37）：运行线程存活标志，:meth:`is_alive` 供 win.service 探活轮询。
        self._thread_alive = False

    # -- ServiceApplication 协议 ----------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._thread_main, name="agent-runtime", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop_flag.set()
        self._wake()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))
            if thread.is_alive():
                log.warning("服务运行时在 %.0fs 内未退出", timeout_s)

    def is_alive(self) -> bool:
        """0.3.1（F37/F31）：``win.service`` 的探活契约——运行线程是否还活着。

        返回 ``False`` 时服务宿主以非零码退出，交给 SCM recovery 重启
        （僵尸服务检测，契约见 ``win/service.py`` 模块 docstring 的 F31 段）。
        """

        thread = self._thread
        return thread is not None and thread.is_alive() and self._thread_alive

    # -- 线程/事件循环 ---------------------------------------------------
    def _thread_main(self) -> None:
        self._thread_alive = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception:  # noqa: BLE001 - 服务进程必须留下崩溃证据
            log.exception("服务运行时崩溃")
        finally:
            self._thread_alive = False
            with contextlib.suppress(Exception):  # 关闭线程内事件循环失败无补救手段
                self._loop.close()

    def _wake(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # 循环已关闭时唤醒失败是正常竞态
            loop.call_soon_threadsafe(self._set_stop)

    def _set_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def _sleep_or_stop(self, seconds: float) -> None:
        assert self._stop_event is not None
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, float(seconds)))

    def _request_shutdown(self, reason: str) -> None:
        log.info("请求停止服务：%s", reason)
        callback = self._request_shutdown_cb
        if callback is not None:
            try:
                callback(reason)
            except Exception:  # noqa: BLE001 - 停止回调失败不能阻止内部退出
                log.exception("停止回调抛异常")
        self._stop_flag.set()
        self._wake()

    # -- 主流程 ----------------------------------------------------------
    async def _run(self) -> None:
        setup_logging("agent")
        ensure_dirs()
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_flag.is_set():
            self._stop_event.set()
        log.info(
            "服务运行时启动 version=%s device=%s service_mode=%s",
            self._safe_version(),
            self._store.config.device_uid,
            self._service_mode,
        )
        try:
            self._sysinfo_cache = await asyncio.to_thread(sysinfo.collect, self._safe_version())
            self._link = self._build_link()
            self._register_handlers()
            await self._ensure_enrolled()
            await self._link.start()
            tasks = [
                asyncio.create_task(self._dispatch_loop(), name="agent-dispatch"),
                asyncio.create_task(self._session_loop(), name="agent-sessions"),
                asyncio.create_task(self._sysinfo_loop(), name="agent-sysinfo"),
                asyncio.create_task(self._maintenance_loop(), name="agent-maintenance"),
            ]
            try:
                await self._stop_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            link = self._link
            if link is not None:
                with contextlib.suppress(Exception):  # 关闭链路失败只能记录
                    await link.close()
            outbox = self._outbox
            if outbox is not None:
                outbox.close()
            self._shutdown_workers()
            log.info("服务运行时已停止")

    def _safe_version(self) -> str:
        try:
            from .. import __version__

            return __version__
        except Exception:  # noqa: BLE001 - 版本解析失败降级为空串，不阻断启动
            log.debug("读取版本失败", exc_info=True)
            return ""

    # -- 注册 ------------------------------------------------------------
    async def _ensure_enrolled(self) -> None:
        assert self._link is not None
        if self._store.config.token:
            return
        # 开放注册：没有本地令牌就直接注册，不需要注册码/安装令牌。
        try:
            await self._enroll(self._link)
        except Exception as exc:  # noqa: BLE001 - 注册失败由链路的 401 重注册路径继续重试
            log.warning("首次注册失败（链路会继续重试）：%s", exc)

    async def _enroll(self, link: Link) -> str:
        from .. import __version__

        config = self._store.config
        info = await asyncio.to_thread(sysinfo.collect, __version__, sessions=self._sessions_payload())
        # A4：配置了部署准入密钥才带 code；空串时 enroll() 不写该字段（零凭据）。
        enroll_key = resolve_enroll_key(config.enroll_key)
        payload = await link.enroll(
            device_uid=config.device_uid,
            hostname=str(info.get("hostname") or ""),
            os_name=str(info.get("os_name") or ""),
            os_version=f"{info.get('os_release', '')} {info.get('os_version', '')}".strip(),
            arch=str(info.get("arch") or ""),
            agent_version=__version__,
            sysinfo=info,
            sessions=list(info.get("sessions") or []),
            code=enroll_key,
            boot_id=config.boot_id,
        )
        token = str(payload.get("token") or "")
        if not token:
            raise RuntimeError("注册响应没有 token")
        self._store.update(
            token=token,
            base_urls=[str(url) for url in (payload.get("base_urls") or config.base_urls)],
            server_version=str(payload.get("server_version") or ""),
        )
        self._store.apply_server_config(
            payload.get("config") or {}, server_time_ms=int(payload.get("server_time_ms") or 0)
        )
        link.set_base_urls(self._store.config.base_urls)
        log.info("设备已注册 uid=%s", self._store.config.device_uid)
        return token

    async def _reauth(self) -> str | None:
        """401 之后重新注册（A17 受控端侧）；返回新令牌。

        开放注册下没有"能不能注册"的前置开关，401 的唯一原因是本地令牌失效，
        因此直接重注册即可；服务端拒绝时由链路退避后继续重试。
        """

        link = self._link
        if link is None:
            log.error("设备令牌失效，但链路不存在，无法重新注册")
            return None
        try:
            return await self._enroll(link)
        except Exception as exc:  # noqa: BLE001 - 失败由链路退避后重试
            log.warning("重新注册失败：%s", exc)
            return None

    # -- 链路 ------------------------------------------------------------
    def _open_outbox(self) -> Outbox:
        """持久化 outbox（W1）：磁盘不可用时降级为内存，但必须 loud 失败。"""

        limits = outbox_limits_from_env()
        try:
            store = SqliteOutboxStore(outbox_path())
        except Exception as exc:  # noqa: BLE001 - 磁盘损坏不能阻止受控端上线
            log.exception(
                "打开持久化 outbox 失败（%s），本次运行降级为内存队列：进程重启会丢未确认上行",
                exc,
            )
            return Outbox(limits)
        log.info("持久化 outbox 已打开 path=%s", outbox_path())
        return Outbox(limits, store=store)

    def _build_link(self) -> Link:
        config = self._store.config
        if self._link_factory is not None:
            return self._link_factory(config, self)
        # 同 Link：Outbox 有 __len__，不能写 ``self._outbox or ...``（空队列是假值）。
        outbox = self._outbox if self._outbox is not None else self._open_outbox()
        self._outbox = outbox
        probe_base_s = PROBE_BASE_S
        raw_probe = os.environ.get("OBS_PROBE_BASE_S", "").strip()
        if raw_probe:
            try:
                probe_base_s = max(1.0, float(raw_probe))
            except ValueError:
                log.warning("OBS_PROBE_BASE_S=%r 不是数字，使用默认 %ss", raw_probe, PROBE_BASE_S)
        else:
            # A15：transport_reprobe_s 是 CarrierPolicy 向上探测的起始间隔。
            try:
                probe_base_s = max(1.0, float(config.setting("transport_reprobe_s", PROBE_BASE_S)))
            except (TypeError, ValueError):
                log.warning(
                    "transport_reprobe_s=%r 不是数字，使用默认 %ss",
                    config.setting("transport_reprobe_s", PROBE_BASE_S),
                    PROBE_BASE_S,
                )
        try:
            poll_interval_s = max(1.0, float(config.setting("poll_interval_s", 5)))
        except (TypeError, ValueError):
            log.warning("poll_interval_s 配置非法，使用默认 5s")
            poll_interval_s = 5.0
        return Link(
            token=config.token,
            base_urls=config.base_urls,
            device_uid=config.device_uid,
            agent_version=self._safe_version(),
            boot_id=config.boot_id,
            clock=self._store.clock,
            outbox=outbox,
            outbox_limits=outbox_limits_from_env(),
            heartbeat_payload=self._heartbeat_payload,
            on_carrier_change=self._on_carrier_change,
            on_unauthorized=self._reauth,
            heartbeat_interval_s=int(config.setting("heartbeat_interval_s", 20)),
            probe_base_s=probe_base_s,
            http_poll_interval_s=poll_interval_s,
        )

    def _heartbeat_payload(self) -> dict[str, Any]:
        return {
            "agent_version": self._safe_version(),
            "sysinfo": sysinfo.quick(),
            "state": self._state_payload(),
        }

    def _state_payload(self) -> dict[str, Any]:
        link = self._link
        return {
            "transport": link.carrier if link is not None else None,
            "connected": bool(link.connected) if link is not None else False,
            "sessions": self._sessions_payload(),
            "spool": link.pending_uplink if link is not None else 0,
            "control": sorted(self._controls),
            "updating": self._updating,
            "uptime_s": max(0, (_now_ms() - self._started_at_ms) // 1000),
            "capture": self._health.snapshot(_now_ms()),
        }

    def _on_carrier_change(self, change: Any) -> None:
        log.info("载体切换：%s -> %s（%s）", change.previous, change.current, change.reason)

    # -- 会话/worker -----------------------------------------------------
    def _sessions_payload(self) -> list[dict[str, Any]]:
        return [_session_dict(item) for item in self._sessions]

    def _list_sessions(self) -> list[Any]:
        try:
            from ..win.session import list_sessions
        except ImportError as exc:  # pragma: no cover - win 包缺失属打包错误
            log.warning("win.session 不可用，会话枚举禁用：%s", exc)
            return []
        try:
            return list(list_sessions())
        except Exception as exc:  # noqa: BLE001 - 非 Windows/无权限时降级为空列表
            log.warning("会话枚举失败：%s", exc)
            return []

    async def _session_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                sessions = await asyncio.to_thread(self._list_sessions)
                self._sessions = sessions
                self._reconcile_workers()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一次扫描失败不能终止扫描循环
                log.exception("会话扫描失败")
            await self._sleep_or_stop(SESSION_SCAN_S)

    def _reconcile_workers(self) -> None:
        active = {int(_session_dict(item).get("session_id") or 0) for item in self._sessions}
        active.discard(0)
        for session_id in list(self._pipes):
            if session_id not in active:
                log.info("会话 %s 已结束，停止 worker 管道", session_id)
                self._stop_worker(session_id)
        if not self._service_mode and os.environ.get("OBS_WORKER_IN_CONSOLE", "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            # 前台模式默认不拉起用户会话 worker（0.2.10 行为，避免调试时冒出采集进程）；
            # E2E/现场排障可用 OBS_WORKER_IN_CONSOLE=1 打开完整链路（命令→worker→帧）。
            return
        target = self._select_target()
        if target is None:
            return
        self._ensure_worker(target)

    def _select_target(self) -> int | None:
        """按 ``capture_scope`` 选择要采集的会话（A15 的消费者）。

        ``session``（默认）：优先当前交互用户会话（非 console 会话），没有则退回
        console 会话；``console``：优先 console 会话。可采集状态以
        :data:`win.session.CAPTURABLE_STATES` 为唯一权威（F36：active/disconnected
        ——RDP 关窗后的 disconnected 会话仍可采集，无人值守不失联；connected 是
        CONNECTQUERY 桌面未就绪，重试白耗）。此前这里硬编码旧集合，与
        ``select_capturable`` 双实现漂移（P1，2026-10 审查）。
        """

        try:
            from ..win.session import CAPTURABLE_STATES
        except Exception:  # noqa: BLE001 — 非 Windows 测试环境回退
            CAPTURABLE_STATES = frozenset({"active", "disconnected"})

        scope = str(self._store.setting("capture_scope", "session")).strip().lower()
        entries = [_session_dict(item) for item in self._sessions]
        candidates = [
            entry
            for entry in entries
            if str(entry.get("username") or "").strip()
            and str(entry.get("state") or "").lower() in CAPTURABLE_STATES
        ]
        if not candidates:
            return None
        console = [entry for entry in candidates if entry.get("is_console")]
        if scope == "console":
            chosen = (console or candidates)[0]
        else:
            interactive = [entry for entry in candidates if not entry.get("is_console")]
            chosen = (interactive or console or candidates)[0]
        return int(chosen.get("session_id") or 0) or None

    def _ensure_worker(self, session_id: int) -> None:
        pipe = self._pipes.get(session_id)
        if pipe is None:
            pipe = self._create_pipe_server(session_id)
            if pipe is None:
                return
        if pipe.connected.is_set():
            self._launch_failures.pop(session_id, None)
            return
        pid = self._processes.get(session_id)
        if pid and self._process_alive(pid):
            return
        failures, last_attempt = self._launch_failures.get(session_id, (0, 0.0))
        now = time.monotonic()
        if failures and now - last_attempt < _launch_backoff(failures):
            return
        if failures >= 5 and now - self._last_launch_log_s.get(session_id, 0.0) >= 300:
            self._last_launch_log_s[session_id] = now
            log.warning(
                "会话 %s 已连续 %s 次拉起失败，按递进退避重试（冷却 %.0fs）",
                session_id,
                failures,
                _launch_backoff(failures),
            )
        self._launch_failures[session_id] = (failures + 1, now)
        self._launch_worker(session_id)

    def _create_pipe_server(self, session_id: int) -> Any | None:
        try:
            from ..win.pipe import PipeServer
            from ..win.session import executable_path, session_user_sid
        except ImportError as exc:  # pragma: no cover
            log.error("win.pipe 不可用：%s", exc)
            return None
        try:
            allowed_sid = session_user_sid(session_id)
        except Exception as exc:  # noqa: BLE001 - 没有用户令牌的残留会话无法建管道
            if self._service_mode:
                log.info("会话 %s 无法解析用户 SID，跳过 worker：%s", session_id, exc)
                return None
            # 前台/调试模式与服务进程同用户：WTSQueryUserToken 需要 SE_TCB，
            # 普通管理员也没有；此时用当前进程令牌的 SID 仍然满足"只允许本用户"。
            allowed_sid = self._current_user_sid()
            if not allowed_sid:
                log.error("会话 %s 无法确定允许的用户 SID，跳过 worker", session_id)
                return None
            log.info("前台模式：会话 %s 使用当前进程 SID 校验 worker 管道", session_id)
        # 生产（service）模式严格要求 worker 镜像=本安装目录 exe；前台调试模式下
# 用户可能用 venv python / 不同解释器路径启动，本机进程身份已由 SID+会话校验，
# 不再卡镜像路径（否则 A17 在调试场景会把合法 worker 拒之门外）。
        expected_image = executable_path() if self._service_mode else None
        server = PipeServer(
            session_id,
            allowed_user_sid=allowed_sid,
            on_message=lambda header, payload, sid=session_id: self._on_pipe_message(sid, header, payload),
            expected_image_path=expected_image,
        )
        server.start()
        self._pipes[session_id] = server
        log.info("会话 %s 的 worker 管道已开始监听", session_id)
        return server

    @staticmethod
    def _current_user_sid() -> str | None:
        """当前进程用户的 SID（前台模式用来替代 WTSQueryUserToken）。"""

        try:
            import win32api
            import win32security
        except ImportError:  # pragma: no cover - 非 Windows
            return None
        try:
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            try:
                sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                win32api.CloseHandle(token)
            return str(win32security.ConvertSidToStringSid(sid))
        except Exception as exc:  # noqa: BLE001 - 取不到 SID 时由调用方拒绝建管道
            log.warning("读取当前进程用户 SID 失败：%s", exc)
            return None

    def _launch_worker(self, session_id: int) -> None:
        # A2：worker 不读配置，boot_id 只能经命令行传给子进程。
        boot_id = self._store.config.boot_id
        if not self._service_mode:
            # 前台/调试模式：直接在当前会话 spawn worker（无需 CreateProcessAsUser，
            # 普通用户也能跑完整链路；service 模式仍走跨会话拉起）。
            try:
                from ..win.session import spawn_local, worker_arguments

                process = spawn_local(worker_arguments(session_id, boot_id=boot_id))
            except Exception as exc:  # noqa: BLE001 - 拉起失败要可观测并按退避重试
                log.warning("会话 %s 前台模式拉起 worker 失败：%s", session_id, exc)
                return
            self._local_workers[session_id] = process
            self._processes[session_id] = int(process.pid)
            log.info("会话 %s 的 worker 已拉起（前台模式）pid=%s", session_id, process.pid)
            return
        try:
            from ..win.session import launch_worker
        except ImportError as exc:  # pragma: no cover
            log.error("win.session 不可用，无法拉起 worker：%s", exc)
            return
        try:
            pid = int(launch_worker(session_id, boot_id=boot_id))
        except Exception as exc:  # noqa: BLE001 - CreateProcessAsUser 失败要可观测并按退避重试
            log.warning("会话 %s 拉起 worker 失败：%s", session_id, exc)
            return
        self._processes[session_id] = pid
        log.info("会话 %s 的 worker 已拉起 pid=%s", session_id, pid)

    def _process_alive(self, pid: int) -> bool:
        try:
            import win32api
            import win32con
            import win32process

            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except ImportError:  # pragma: no cover - 非 Windows
            return False
        except Exception:  # noqa: BLE001 - 进程不存在/无权限都视为已退出
            return False
        try:
            return int(win32process.GetExitCodeProcess(handle)) == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001 - 查询失败按已退出处理，触发重新拉起
            return False
        finally:
            with contextlib.suppress(Exception):  # 关闭句柄失败无害
                win32api.CloseHandle(handle)

    def _stop_worker(self, session_id: int) -> None:
        server = self._pipes.pop(session_id, None)
        if server is not None:
            try:
                server.send(new_envelope("stop", {"reason": "session_end"}, ts_ms=_now_ms()))
            except Exception as exc:  # noqa: BLE001 - 连接已断是常态，记录后继续收尾
                log.debug("向会话 %s 发送 stop 失败：%s", session_id, exc)
            server.stop()
        self._processes.pop(session_id, None)
        worker = self._local_workers.pop(session_id, None)
        if worker is not None and worker.poll() is None:
            try:
                worker.terminate()
                worker.wait(timeout=5)
            except Exception as exc:  # noqa: BLE001 - worker 已退出/无权限时只记录
                log.debug("终止前台模式 worker 失败：%s", exc)
        self._launch_failures.pop(session_id, None)
        self._last_launch_log_s.pop(session_id, None)
        self._session_state.pop(session_id, None)
        self._health.remove(session_id)

    def _shutdown_workers(self) -> None:
        for session_id in list(self._pipes):
            self._stop_worker(session_id)
        for session_id, worker in list(self._local_workers.items()):
            if worker.poll() is None:
                try:
                    worker.terminate()
                except Exception as exc:  # noqa: BLE001 - 退出阶段只记录
                    log.debug("终止本地 worker %s 失败：%s", session_id, exc)
            self._local_workers.pop(session_id, None)

    def _on_pipe_message(self, session_id: int, header: dict[str, Any], payload: bytes) -> None:
        """管道线程回调：把消息投递到 asyncio 循环（不在这里做 IO/等待）。"""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._handle_worker_message(session_id, header, payload), loop)
        except RuntimeError as exc:  # 循环正在关闭
            log.debug("管道消息丢弃（运行时已关闭）: %s", exc)

    async def _handle_worker_message(self, session_id: int, header: dict[str, Any], payload: bytes) -> None:
        kind = header.get("t")
        try:
            if kind == "frame" and payload:
                await self._on_worker_frame(session_id, header, payload)
                return
            if kind == "ctrl" and payload:
                await self._on_worker_ctrl(header, payload)
                return
            if header.get("v") == 3:
                envelope = envelope_from_dict(header)
                await self._on_worker_envelope(session_id, envelope)
                return
            log.warning("会话 %s 发来未知管道消息 t=%r（忽略）", session_id, kind)
        except EnvelopeError as exc:
            log.warning("会话 %s 的管道帧非法（忽略）：%s", session_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单条消息失败不能影响会话
            log.exception("处理会话 %s 的管道消息失败 t=%r", session_id, kind)

    async def _on_worker_frame(self, session_id: int, header: dict[str, Any], payload: bytes) -> None:
        link = self._link
        if link is None:
            return
        captured_ms = int(header.get("device_ts_ms") or header.get("ts") or _now_ms())
        try:
            link.enqueue_frame(
                payload,
                device_ts_ms=captured_ms,
                session_id=session_id,
                kind=str(header.get("kind") or "screen"),
                width=int(header.get("w") or 0),
                height=int(header.get("h") or 0),
                score=int(header.get("score") or 0),
            )
        except OutboxFull as exc:
            log.error("帧入队失败（outbox 满，会话 %s）：%s", session_id, exc)

    async def _on_worker_ctrl(self, header: dict[str, Any], payload: bytes) -> None:
        link = self._link
        session = str(header.get("session") or "")
        if link is None or not session:
            return
        sent = await link.send_control_frame(session, payload, ts_ms=int(header.get("ts_ms") or 0) or None)
        if not sent:
            log.debug("控制帧未发送（当前载体不支持或已断开）session=%s", session)

    async def _on_worker_envelope(self, session_id: int, envelope: Any) -> None:
        link = self._link
        if link is None:
            return
        body = envelope.body_dict
        if envelope.type == "hello":
            state = self._session_state.setdefault(session_id, {})
            state.update(
                {
                    "connected": True,
                    "agent_version": body.get("agent_version", ""),
                    "boot_id": body.get("boot_id", ""),
                }
            )
            self._health.update(
                session_id, body.get("capture"), control_active=bool(body.get("control_active")), now_ms=_now_ms()
            )
            log.info("worker hello session=%s version=%s", session_id, body.get("agent_version"))
            if isinstance(body.get("sessions"), list):
                # 0.3.1（F37）：worker 上报的会话列表只留排障日志，不再覆盖
                # ``self._sessions``——会话表的唯一来源是 _session_loop 的 WTS 枚举，
                # 用上报覆盖会引入第二个真相源（worker 看到的列表更陈旧）。
                log.debug("worker hello sessions=%s", body["sessions"])
            # A2：worker 不再读 config.json（SYSTEM 独占），所以**必须**在 hello
            # 之后立刻把非敏感设置推给它。此前这条同步只在配置变化时发生，
            # worker 靠自读文件兜底；现在兜底没有了，首帧推送就是唯一来源。
            self._send_config_to(session_id)
            return
        if envelope.type == "heartbeat":
            self._health.update(
                session_id,
                body.get("capture"),
                control_active=bool(body.get("control_active")),
                now_ms=_now_ms(),
            )
            self._session_state.setdefault(session_id, {})["connected"] = True
            return
        if envelope.type == "result":
            try:
                await link.send("result", body)
            except (LinkClosed, OutboxFull) as exc:
                log.warning("回执发送失败 command=%s：%s", body.get("command_id"), exc)
            return
        if envelope.type == "event":
            try:
                await link.send("event", body)
            except (LinkClosed, OutboxFull) as exc:
                log.warning("事件发送失败 event=%s：%s", body.get("event"), exc)
            return
        if envelope.type == "pong":
            return
        log.debug("忽略 worker 上行类型 %s（session=%s）", envelope.type, session_id)

    # -- 下行分发 --------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        assert self._link is not None
        while not self._stop_flag.is_set():
            try:
                envelope = await self._link.recv()
            except LinkClosed:
                log.info("链路已关闭，命令分发退出")
                return
            try:
                await self._handle_downlink(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条下行失败不能终止分发
                log.exception("处理下行信封失败 type=%s", envelope.type)

    async def _handle_downlink(self, envelope: Any) -> None:
        kind = envelope.type
        body = envelope.body_dict
        if kind == "command":
            # 0.3.1（F34g）：更新进行中拒绝重启——重启会把后台更新任务与 helper
            # 状态一起带走，swap 做到一半的服务进程消失 = 设备下线。
            if str(body.get("type") or "") == "restart_agent" and self._updating:
                log.warning("更新进行中，拒绝 restart_agent 命令")
                command_id = str(body.get("command_id") or "")
                if command_id:
                    await self._send_result(
                        command_id,
                        CommandResult(ok=False, error="更新进行中，拒绝重启", error_code="BAD_REQUEST"),
                    )
                return
            await self._handle_command(body)
            return
        if kind == "config":
            values = body.get("config")
            changed = self._store.apply_server_config(
                values if isinstance(values, dict) else {}, server_time_ms=int(body.get("server_time_ms") or 0)
            )
            if changed:
                log.info("服务端配置已更新：%s", sorted(values or {}))
            self._broadcast_config()
            return
        if kind == "welcome":
            values = body.get("config")
            if isinstance(values, dict):
                self._store.apply_server_config(
                    values, server_time_ms=int(body.get("server_time_ms") or 0)
                )
                self._broadcast_config()
            offer = body.get("update_offer")
            if isinstance(offer, dict):
                await self._dispatch_internal("update", offer)
            return
        if kind == "control":
            await self._forward_control(body)
            return
        if kind == "uninstall":
            log.warning("收到卸载指令（uninstall 信封），交给唯一卸载处理器")
            await self._dispatch_internal("uninstall", {})
            return
        if kind == "update_offer":
            await self._dispatch_internal("update", body)
            return
        log.debug("忽略下行信封类型 %s", kind)

    async def _handle_command(self, body: dict[str, Any]) -> None:
        """下行命令的**唯一**入口：本进程有处理器就本地执行，否则转发 worker。

        服务命令与 worker 命令不再各有一条分发路径：处理器注册在同一个
        ``CommandDispatcher`` 上，命令 id 去重、异常回执、超时语义全部一份实现。

        A6：命令预算的"接收时刻"一律取**本进程单调时钟**，不接受信封里的
        ``ts_ms``（那是服务端时钟，与本地 ``_monotonic_ms()`` 不同源，做差会把
        预算算成负数或几天）。
        """

        try:
            command = Command.from_body(body, ts_ms=_monotonic_ms())
        except ValueError as exc:
            log.warning("下行命令格式非法（忽略）：%s", exc)
            command_id = str(body.get("command_id") or "")
            if command_id:
                await self._send_result(
                    command_id,
                    CommandResult(ok=False, error=f"命令格式非法: {exc}", error_code="BAD_REQUEST"),
                )
            return
        # A6：执行前先校验预算。已经超时的命令绝不执行——服务端/用户已收到
        # 超时，此时再执行删除/写入/重启是"看不见的破坏性动作"。
        if command.is_expired(_monotonic_ms()):
            log.warning(
                "命令 %s 超出执行预算（type=%s deadline_ms=%s），拒绝执行",
                command.id,
                command.type,
                command.deadline_ms,
            )
            await self._send_result(command.id, expired_result(command))
            return
        if command.type in self._dispatcher.handlers:
            result = await self._dispatcher.dispatch(command)
            await self._send_result(command.id, result)
            return
        await self._forward_to_worker(command)

    async def _dispatch_internal(self, command_type: str, payload: dict[str, Any]) -> CommandResult:
        """协议级下行（update_offer / uninstall 信封）走同一套处理器，但不回执。

        这类下行没有服务端创建的 command id，结果通过事件/``uninstall_ack`` 表达；
        仍然复用 dispatcher 的处理器与幂等，避免第二条更新/卸载实现。
        """

        command = Command(id=new_id(), type=command_type, payload=dict(payload))
        result = await self._dispatcher.dispatch(command)
        log.info(
            "内部派发 %s 完成 ok=%s error=%s", command_type, result.ok, result.error or "-"
        )
        return result

    # -- 服务命令处理器 --------------------------------------------------
    def _register_handlers(self) -> None:
        """把服务进程要执行的动作注册到唯一 dispatcher。"""

        self._dispatcher.register("sysinfo", self._cmd_sysinfo)
        self._dispatcher.register("log_tail", self._cmd_log_tail)
        self._dispatcher.register("reload_config", self._cmd_reload_config)
        self._dispatcher.register("restart_agent", self._cmd_restart_agent)
        self._dispatcher.register("reboot", lambda command: self._cmd_power(command, reboot=True))
        self._dispatcher.register("poweroff", lambda command: self._cmd_power(command, reboot=False))
        try:
            from ..tasks import update as update_task

            # 复用 agent-sys 的更新处理器（校验/事件/后台任务/停服），
            # 但 fetch 必须绑定"本次命令的 version"：做一个薄包装，避免从
            # staging 文件名反解版本，也避免共享可变的版本状态。
            self._update_task = update_task
            self._dispatcher.register("update", self._cmd_update)
        except Exception:  # noqa: BLE001 - 处理器缺失必须显式告警（更新命令会回执失败）
            log.exception("注册 update 处理器失败，更新命令将不可用")
        try:
            from ..tasks import uninstall as uninstall_task

            uninstall_task.register_handlers(
                self._dispatcher,
                install_dir=INSTALL_DIR,
                ack=self._uninstall_ack,
                report_failure=self._uninstall_report_failure,
                request_shutdown=self._request_shutdown,
                spawn_task=self._spawn,
                current_pid=os.getpid(),
            )
        except Exception:  # noqa: BLE001 - 注册失败时卸载命令会回执失败，服务保持在线
            log.exception("注册 uninstall 处理器失败，卸载命令将不可用")

    def _cmd_update(self, command: Command) -> CommandResult:
        """更新命令的唯一入口：并发保护 + 绑定本次版本的 fetch。

        ``tasks.update.make_update_handler`` 的 ``PackageFetcher`` 只接收目标路径，
        不接收版本；这里**按命令**构造处理器，把 version 闭包进 fetch，这样既不改
        agent-sys 的文件，也不靠 staging 文件名反解版本。
        """

        version = str(command.payload.get("version") or "").strip()
        if self._update_running:
            return CommandResult(
                ok=False,
                result={"version": version},
                error="已有更新在进行中",
                error_code="BAD_REQUEST",
            )
        # P1（2026-10 审查）：更新门槛。此前 offer/handler 都不比较版本，服务端
        # 可把设备降到任意旧版本（含回退安全修复）；`auto_update` 配置项定义了
        # 却无任何消费者（安慰剂，违反自家 C8 纪律）。规则：
        #   * forced=True（payload 显式声明）→ 跳过版本比较，仍受 auto_update 管辖；
        #   * 目标版本不高于当前 → 拒绝降级；
        #   * auto_update 关闭（设置非 true）→ 拒绝一切自动更新。
        try:
            from ..version import is_newer
            from .. import __version__ as current_version
        except Exception:  # noqa: BLE001 — 版本模块缺失时退回不比较
            is_newer = None  # type: ignore[assignment]
            current_version = ""  # type: ignore[assignment]
        forced = bool(command.payload.get("forced"))
        if is_newer is not None and not forced and not is_newer(version, current_version):
            return CommandResult(
                ok=False,
                result={"version": version},
                error=f"拒绝降级到 {version}（当前 {current_version}；强制降级需 forced=true）",
                error_code="BAD_REQUEST",
            )
        auto_update = str(self._store.setting("auto_update", True)).strip().lower()
        if auto_update not in ("1", "true", "yes", "on"):
            return CommandResult(
                ok=False,
                result={"version": version},
                error="auto_update 已关闭，拒绝自动更新（forced=true 亦受此开关管辖）",
                error_code="BAD_REQUEST",
            )
        task = self._update_task
        if task is None:
            return CommandResult(
                ok=False,
                result={"version": version},
                error="更新组件不可用",
                error_code="INTERNAL",
            )
        handler = task.make_update_handler(
            fetch=lambda destination: self._fetch_package(version, destination),
            staging_dir=STAGING_DIR,
            install_dir=INSTALL_DIR,
            request_shutdown=self._request_shutdown,
            on_event=self._update_event,
            on_finished=self._update_finished,
            spawn_task=self._spawn,
            current_pid=os.getpid(),
        )
        result = handler(command)
        if result.ok:
            self._update_running = True
            self._updating = True
        return result

    def _spawn(self, coroutine: Any) -> Any:
        """把后台协程挂到运行时事件循环（处理器的 ``spawn_task`` 注入点）。"""

        return asyncio.get_running_loop().create_task(coroutine)

    async def _fetch_package(self, version: str, destination: Path) -> None:
        """``PackageFetcher``：下载安装包，并**读包内 VERSION** 校验与 offer 一致。

        版本来源只有服务端下发的 version（闭包注入），不再从 staging 文件名反解；
        包内版本不符/缺失时删除下载文件并拒绝升级（错包绝不能进 swap 阶段）。
        """

        from ..tasks.update import UpdateFailed

        if not version:
            raise UpdateFailed("更新请求缺少 version", step="fetch")
        link = self._link
        if link is None:
            raise UpdateFailed("链路未建立，无法下载安装包", step="fetch")
        target = Path(destination)
        await link.download_package(version, target)
        actual = read_package_version(target)
        if not actual:
            target.unlink(missing_ok=True)
            raise UpdateFailed("安装包缺少根目录 VERSION，拒绝升级", step="fetch")
        if actual != version:
            target.unlink(missing_ok=True)
            raise UpdateFailed(
                f"安装包自称版本 {actual!r} 与目标 {version!r} 不一致，拒绝升级", step="fetch"
            )
        log.info("安装包版本校验通过 version=%s bytes=%s", version, target.stat().st_size)

    async def _cmd_sysinfo(self, _command: Command) -> CommandResult:
        info = await asyncio.to_thread(
            sysinfo.collect, self._safe_version(), sessions=self._sessions_payload()
        )
        self._sysinfo_cache = dict(info)
        return CommandResult(ok=True, result={"sysinfo": info})

    def _cmd_log_tail(self, command: Command) -> CommandResult:
        lines = int(command.payload.get("lines") or 200)
        return CommandResult(ok=True, result={"lines": read_tail(lines, "agent")})

    def _cmd_reload_config(self, _command: Command) -> CommandResult:
        return CommandResult(ok=True, result={"config": self._store.settings_snapshot()})

    def _cmd_restart_agent(self, command: Command) -> CommandResult:
        """重启：先**提交**（注册重启机制）再回执，然后才退出进程（A6）。

        旧实现立刻回 ``ok``，后台再申请重启；两个机制都失败时用户已经看到
        "已重启"而机器毫无变化（后台失败只写日志）。现在把"提交"拉进回执窗口：
        SCM recovery 与计划任务都成功登记才算成功，否则回可读错误并**保持运行**。
        ��交成功后必须等回执真的发出去再退出（RESULT_FLUSH_S），所以退出动作
        仍然放在后台任务里。
        """

        try:
            from ..win.service import ServiceController, ServiceError, commit_restart
        except ImportError as exc:  # pragma: no cover - 打包缺模块
            return CommandResult(ok=False, error=f"重启组件不可用: {exc}", error_code="INTERNAL")
        try:
            outcome = commit_restart(ServiceController(), tmp_dir=STAGING_DIR)
        except ServiceError as exc:
            log.warning("重启提交失败：%s", exc)
            return CommandResult(ok=False, error=f"重启提交失败: {exc}", error_code="INTERNAL")
        except Exception as exc:  # noqa: BLE001 - 非 Windows/权限不足都要变成可读回执
            log.warning("重启提交异常：%s", exc, exc_info=True)
            return CommandResult(ok=False, error=f"重启提交异常: {exc}", error_code="INTERNAL")
        if not outcome["registered"] and not outcome["recovery"]:
            return CommandResult(
                ok=False,
                error="无法注册任何重启机制（计划任务与 SCM recovery 都失败），未重启",
                error_code="INTERNAL",
            )
        # 提交已确认：剩余预算不够就别再扛着——直接把决定权交回调用方。
        self._spawn(self._restart_agent(committed=True))
        return CommandResult(
            ok=True,
            result={
                "restarting": True,
                "mechanisms": sorted(
                    name for name, ok in (("scm-recovery", outcome["recovery"]), ("scheduled-task", outcome["registered"])) if ok
                ),
                "commit": True,
            },
        )

    def _cmd_power(self, command: Command, *, reboot: bool) -> CommandResult:
        """重启/关机：以 ``shutdown.exe`` 的**真实退出码**回执（A6）。

        旧实现把 ``asyncio.to_thread(_shutdown_machine)`` 一扔就回 ``ok``，失败
        只写日志——用户看到"已关机"而机器还开着。现在同步等提交结果（受同一
        命令预算约束），失败回可读错误并补一条事件。
        """

        budget_s = float(command.remaining_ms(_monotonic_ms()) or 0) / 1000.0
        ok, detail = _shutdown_machine(reboot, timeout_s=budget_s if budget_s > 0 else 30.0)
        if ok:
            return CommandResult(ok=True, result={"ok": True, "reboot": bool(reboot), "commit": True})
        log.error("重启/关机提交失败 reboot=%s: %s", reboot, detail)
        # 破坏性动作失败必须可观测：除了回执，再补一条事件（后台/前端都能看到）。
        on_event = self._send_event
        self._spawn(on_event("power_failed", {"reboot": bool(reboot), "error": detail}))
        return CommandResult(
            ok=False,
            error=f"{'重启' if reboot else '关机'}提交失败: {detail}",
            error_code="INTERNAL",
        )

    async def _uninstall_ack(self) -> None:
        """helper 写回 ok=true 之后才 ack（A1：先确认再报卸载）。

        P1（2026-10 审查）：``link.send`` 在 HTTP 载体下只入队，真正发送要等
        下一轮长轮询；ack 后调用方立即 ``request_shutdown`` 会取消载体任务，
        随后 purge helper 又会删掉整个数据目录（含 outbox.sqlite3）——ack
        大概率永远发不出去，服务端留下永久幽灵设备。这里等 outbox 排空
        （上限 10s）再返回，给最后一轮轮询一个真实的机会。
        """

        link = self._link
        if link is None:
            return
        try:
            await link.send("uninstall_ack", {"ok": True, "device_time_ms": _now_ms()})
        except (LinkClosed, OutboxFull) as exc:
            log.warning("卸载回执发送失败：%s", exc)
            return
        deadline = time.monotonic() + 10.0
        pending = -1
        while time.monotonic() < deadline:
            try:
                pending = int(link.outbox_stats().get("pending", 1))
            except Exception:  # noqa: BLE001 — 统计不可得时按仍待发送处理
                pending = 1
            if pending <= 0:
                log.info("卸载回执已排空 outbox")
                return
            await asyncio.sleep(0.25)
        log.warning("卸载回执在 10s 内未能排空（outbox pending=%s），继续停服", pending)

    async def _uninstall_report_failure(self, reason: str) -> None:
        await self._send_event("uninstall_failed", {"reason": reason})

    async def _send_result(self, command_id: str, result: CommandResult) -> None:
        link = self._link
        if link is None:
            return
        body = result.to_body(command_id, device_time_ms=_now_ms())
        try:
            await link.send("result", body)
        except (LinkClosed, OutboxFull) as exc:
            log.warning("命令 %s 的回执发送失败：%s", command_id, exc)

    async def _send_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        link = self._link
        if link is None:
            return
        try:
            await link.send("event", {"event": name, "data": dict(data or {})})
        except (LinkClosed, OutboxFull) as exc:
            log.warning("事件 %s 发送失败：%s", name, exc)

    def _update_finished(self) -> None:
        """0.3.1（F34a）：handler 后台任务结束（无论成败）时复位 running 标志。

        不能只靠 ``update_failed`` 事件——事件回调链路上任何异常（或旧版本里
        BadZipFile/OSError 逃逸导致 asyncio task 静默死亡）都会把 ``_update_running``
        永久卡死，之后所有更新命令都被"已有更新在进行中"拒绝。
        """

        self._update_running = False
        self._updating = False

    async def _update_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        """更新流程事件回调：维护 ``state.updating`` 并原样上报事件。"""

        if name == "update_started":
            self._update_running = True
            self._updating = True
        elif name in {"update_downloading", "update_extracting", "update_precheck", "update_helper_started"}:
            self._updating = True
        elif name == "update_failed":
            self._update_running = False
            self._updating = False
        elif name == "update_ready":
            # 0.3.1（F34b）：update_ready 后进程即将 shutdown，``_update_running``
            # 由 handler 完成回调/退出路径自然复位；这里提前清掉反而制造
            # "已 ready 却又接受新更新命令"的窗口。
            self._updating = False
        await self._send_event(name, data)

    async def _forward_to_worker(self, command: Command) -> None:
        session_id = self._pick_session()
        pipe = self._pipes.get(session_id) if session_id is not None else None
        if session_id is None or pipe is None or not pipe.connected.is_set():
            await self._send_result(
                command.id,
                CommandResult(
                    ok=False,
                    error="没有可用的交互式会话，受控端当前无法执行该操作",
                    error_code="UNSUPPORTED",
                ),
            )
            return
        try:
            pipe.send(
                new_envelope(
                    "command",
                    {
                        "command_id": command.id,
                        "type": command.type,
                        # A6：转交的是**剩余**预算，不是原始 deadline_ms。worker 用自己的
                        # 单调时钟起算，排队时间因此也被算进预算——旧实现把原始值转发
                        # 过去，worker 每次重新拿到满额预算，排队多久都能执行。
                        "deadline_ms": (
                            command.remaining_ms(_monotonic_ms())
                            if command.deadline_ms
                            else 0
                        )
                        or 0,
                        "payload": command.payload,
                    },
                    ts_ms=_now_ms(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 管道断开要变成可观测失败回执
            log.warning("会话 %s 的命令转发失败：%s", session_id, exc)
            await self._send_result(command.id, CommandResult(ok=False, **_pipe_failure(exc)))

    def _pick_session(self) -> int | None:
        connected = [session_id for session_id, pipe in self._pipes.items() if pipe.connected.is_set()]
        if not connected:
            return None
        target = self._select_target()
        if target in connected:
            return target
        fallback = min(connected)
        # 0.3.1（F37）：回退到非目标会话是可观测的异常路由（命令可能落在错误桌面上）。
        log.warning("目标会话 %s 没有已连接的 worker，回退到会话 %s", target, fallback)
        return fallback

    async def _forward_control(self, body: dict[str, Any]) -> None:
        # 规格 §5.6（W1 定稿）：{session, action: start|input|stop, fps?, quality?,
        # deadline_ms?, ev?}；输入事件不再有独立信封类型，统一走 action="input"。
        # 兼容读取旧字段 "op"（W0 草稿），新服务端只发 action。
        action = str(body.get("action") or body.get("op") or "")
        session_token = str(body.get("session") or "")
        if not action:
            if body.get("ev"):
                action = "input"
            elif body.get("fps"):
                action = "start"
            else:
                action = "stop"
        now = self._control_clock()
        # A3：先把过期的租约统统清掉（每条控制报文都顺手做一次，成本可忽略）。
        self._expire_control_leases(now)

        if session_token and action == "stop":
            # stop 只清**这一个** token：另一个并发观看者（另一台控制台）不能
            # 因为别人 stop 而被误清。
            self._drop_control_lease(session_token, reason="stop")
        elif session_token and action != "stop":
            # A3（严格 session 匹配）：start 建租约；input 只有**当前**租约才能
            # 通过，非当前 session 的输入直接丢弃，且绝不影响现有流（旧实现对
            # 任意 stop 都会停掉现有流，任意 input 都会注入到当前桌面）。
            lease = self._controls.get(session_token)
            if action == "start":
                self._controls[session_token] = ControlLease(
                    token=session_token,
                    session_id=int(self._pick_session() or 0),
                    expires_at=now + self._lease_seconds(body.get("ttl_s")),
                )
            elif lease is None:
                log.warning(
                    "丢弃非当前控制会话的输入（session=%s，当前=%s）",
                    session_token,
                    sorted(self._controls),
                )
                return
        # A3：控制流一旦建立就**钉在那个会话上**——会话表每次扫描都可能变化
        # （用户切换登录/RDP 重连），重新 ``_pick_session`` 会把同一个控制流
        # 的帧与输入拆到两个不同的桌面上（画面在 A、输入进 B）。
        lease = self._controls.get(session_token) if session_token else None
        session_id = lease.session_id if lease is not None and lease.session_id else self._pick_session()
        pipe = self._pipes.get(session_id) if session_id is not None else None
        if session_id is None or pipe is None or not pipe.connected.is_set():
            log.warning("没有可用会话，控制指令 %s 丢弃 (session=%s)", action, session_token)
            await self._send_event("control_failed", {"reason": "没有可用的交互式会话", "session": session_token})
            return
        try:
            pipe.send(
                new_envelope(
                    "control",
                    {
                        "action": action,
                        "session": session_token,
                        "fps": int(body.get("fps") or 0),
                        "quality": int(body.get("quality") or 0),
                        "ev": body.get("ev") or {},
                        # A3：把本地租约的剩余毫秒数下发给 worker——由真正的执行者
                        # （按住键的那一端）执行到期检查，断链时也能自清。
                        "lease_ms": lease.remaining_ms(now) if lease is not None else 0,
                        "ttl_s": int(body.get("ttl_s") or 0),
                    },
                    ts_ms=_now_ms(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 管道断开要可观测
            log.warning("控制指令转发失败 session=%s：%s", session_token, exc)
            if isinstance(exc, EnvelopeTooLarge):
                # 控制报文头不会超限；真超限说明输入事件异常大，明确拒绝。
                await self._send_event(
                    "control_failed", {"reason": f"控制数据超过管道上限: {exc}", "session": session_token}
                )
            else:
                await self._send_event("control_failed", {"reason": str(exc), "session": session_token})

    # -- 控制租约（A3） ---------------------------------------------------
    @staticmethod
    def _control_clock() -> float:
        """控制租约用的**单调**时钟。

        必须是单调源而不是墙钟：改系统时间 / NTP 跳变既不能让租约无限延长，
        也不能让它瞬间到期。事件循环在跑时用 ``loop.time()``（它本身就是
        ``time.monotonic``，且与 asyncio 内部计时同源）；没有事件循环的同步
        上下文（维护线程/单测）退回 ``time.monotonic``——两者是同一个时钟。
        """

        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return time.monotonic()

    @staticmethod
    def _lease_seconds(raw: Any) -> float:
        """把服务端下发的 ``ttl_s`` 折算成本地租约秒数（带上下限）。"""

        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0:
            return CONTROL_TTL_DEFAULT_S
        return max(CONTROL_TTL_MIN_S, min(CONTROL_TTL_MAX_S, value))

    def _drop_control_lease(self, token: str, *, reason: str) -> None:
        """移除一个控制租约（不向 worker 发包，释放按键由 worker 的清理统一做）。"""

        lease = self._controls.pop(token, None)
        if lease is not None:
            log.info("控制租约结束 session=%s reason=%s", token, reason)

    def _expire_control_leases(self, now: float) -> list[str]:
        """收割到期租约，返回被收割的 token（A3 的本地 TTL）。

        返回 token 让调用方可以给每个过期会话补一条 ``control_failed`` 事件，
        使"控制过期"对前端可观测（而不是画面忽然停住）。
        """

        expired = [token for token, lease in self._controls.items() if lease.expired(now)]
        for token in expired:
            log.warning("控制租约到期（本地 TTL），结束 session=%s", token)
            self._drop_control_lease(token, reason="ttl")
        return expired

    def _broadcast_config(self) -> None:
        config = self._store.settings_snapshot()
        for session_id, pipe in list(self._pipes.items()):
            if not pipe.connected.is_set():
                continue
            try:
                pipe.send(new_envelope("config", {"config": config}, ts_ms=_now_ms()))
            except Exception as exc:  # noqa: BLE001 - 未连接/断开的会话下次 hello 会重新同步
                log.debug("向会话 %s 广播配置失败：%s", session_id, exc)

    def _send_config_to(self, session_id: int) -> None:
        """把当前设置推给**指定**会话（A2：worker 的唯一设置来源）。

        worker 用登录用户令牌运行，读不了 SYSTEM 独占的 config.json；它启动时
        只有 ``DEFAULT_SETTINGS``，必须由服务进程在 hello 之后把服务端下发的
        设置补上，否则"服务端改配置"在真机上永远不会生效。
        """

        pipe = self._pipes.get(session_id)
        if pipe is None or not pipe.connected.is_set():
            return
        config = self._store.settings_snapshot()
        try:
            pipe.send(new_envelope("config", {"config": config}, ts_ms=_now_ms()))
            log.info("已向会话 %s 下发配置（%s 项）", session_id, len(config))
        except Exception as exc:  # noqa: BLE001 - 管道断开时记日志，下次 hello 再同步
            log.warning("向会话 %s 下发配置失败：%s", session_id, exc)

    # -- 重启 ------------------------------------------------------------
    async def _restart_agent(self, *, committed: bool = False) -> None:
        """退出进程以触发重启。

        ``committed=True`` 表示 :meth:`_cmd_restart_agent` 已经成功登记了重启机制
        （A6 的真实提交结果），这里只需等回执出链路再退出。``committed=False``
        是兼容路径（内部/旧调用方）：自己走完整的 ``request_restart``。
        """

        if committed:
            await asyncio.sleep(RESULT_FLUSH_S)  # 让刚发的 result 先出链路
            try:
                from ..win.service import exit_for_restart

                await asyncio.to_thread(exit_for_restart)
            except Exception as exc:  # noqa: BLE001 - 退出失败等于服务仍活着，必须可观测
                log.exception("退出进程失败，服务保持运行")
                await self._send_event("restart_failed", {"error": str(exc)})
            return
        try:
            from ..win.service import ServiceController, request_restart
        except ImportError as exc:
            log.error("无法重启：win.service 不可用（%s）", exc)
            await self._send_event("restart_failed", {"error": str(exc)})
            return
        await asyncio.sleep(RESULT_FLUSH_S)  # 让刚发的 result 先出链路
        try:
            controller = ServiceController()
            await asyncio.to_thread(request_restart, controller, tmp_dir=STAGING_DIR)
        except Exception as exc:  # noqa: BLE001 - 重启失败不能变成停服（A2）
            log.exception("重启失败，服务保持运行")
            await self._send_event("restart_failed", {"error": str(exc)})

    # -- 周期任务 --------------------------------------------------------
    async def _sysinfo_loop(self) -> None:
        while not self._stop_flag.is_set():
            await self._sleep_or_stop(300.0)
            if self._stop_flag.is_set():
                return
            try:
                self._sysinfo_cache = await asyncio.to_thread(sysinfo.collect, self._safe_version())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 采样失败保留旧快照
                log.warning("刷新 sysinfo 失败", exc_info=True)

    async def _maintenance_loop(self) -> None:
        """清理过期控制会话（服务端 TTL 是主机制，这里只防内存泄漏）。"""

        while not self._stop_flag.is_set():
            await self._sleep_or_stop(CONTROL_REAP_S)
            if self._stop_flag.is_set():
                return
            expired = self._expire_control_leases(self._control_clock())
            for token in expired:
                await self._send_event("control_failed", {"reason": "控制会话已到期", "session": token})
            if len(self._controls) > 32:
                keep = dict(list(self._controls.items())[-16:])
                log.warning("控制会话表异常增长（%s），保留最近 16 个", len(self._controls))
                self._controls = keep


# ------------------------------------------------------------------ worker ---
class WorkerRuntime:
    """用户会话内的 worker：管道 + 采集引擎 + 任务处理器。

    **A2（2026-10 审查）：worker 不碰 ``%ProgramData%``**。它用登录用户令牌
    启动，而数据目录被收紧到 SYSTEM+Administrators——旧实现里的
    ``ConfigStore()``（读 config.json 拿令牌）、``ensure_dirs()``（写日志目录）
    在真机上会直接 PermissionError，worker 起不来。
    现在 worker 只有两样东西来自服务进程，全部经管道：
      * 非敏感设置：``config`` 信封（``_apply_remote_config``）；
      * 令牌/uid 之类**根本不需要**——worker 不做任何链路 IO。
    日志写 :func:`core.config.worker_log_dir`（``%LOCALAPPDATA%``，用户可写、
    SYSTEM 可读，``log_tail`` 照常工作）。
    """

    def __init__(self, session_id: int, *, boot_id: str = "") -> None:
        self.session_id = int(session_id)
        self._pipe: Any = None
        #: A2：worker 的设置**只**来自管道；默认值与 AgentConfig 同源
        #: （``DEFAULT_SETTINGS``），因此首帧 config 到达之前的采集行为不变。
        self._config_values: dict[str, Any] = dict(DEFAULT_SETTINGS)
        #: boot_id 由服务进程通过管道下发（worker 从不读配置）。
        self._boot_id = str(boot_id or "")
        self._engine: Any = None
        self._dispatcher = CommandDispatcher()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="worker-cmd")
        self._stop = threading.Event()
        self._control_stop = threading.Event()
        self._control_session = ""
        #: A3：控制租约的单调截止时刻（``time.monotonic``）。
        self._control_deadline = 0.0
        #: A3：已按下键/鼠标按钮的账本（控制结束时统一抬起）。
        self._input_state = InputState()
        self._control_thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    # -- 生命周期 --------------------------------------------------------
    def run(self) -> int:
        from .. import __version__
        from ..tasks.capture import CaptureEngine
        from ..win.pipe import PipeClient

        # A2：worker 日志写用户可写目录（不调用 ensure_dirs，不碰 %ProgramData%）。
        setup_logging(f"worker-{self.session_id}")
        # 0.3.1（F29）：必须在做任何窗口/GDI 调用之前切 PerMonitorV2 DPI 感知——
        # DPI-unaware 进程在非 100% 缩放机器上 GetSystemMetrics 会被虚拟化，
        # 控制坐标整体漂移（input.py 的 A16 换算修复要在这里才真正生效）。
        try:
            from ..tasks.input import enable_dpi_awareness

            log.info("DPI 感知已启用：%s", enable_dpi_awareness())
        except Exception as exc:  # noqa: BLE001 - 非 Windows/设置失败不能阻止 worker 启动
            log.warning("DPI 感知设置失败（高缩放下控制坐标可能漂移）：%s", exc)
        log.info("worker 启动 session=%s version=%s", self.session_id, __version__)
        self._pipe = PipeClient(self.session_id)
        try:
            self._pipe.connect(timeout_s=90)
        except Exception as exc:  # noqa: BLE001 - 连不上服务管道就没有存在意义
            log.error("无法连接服务管道，退出：%s", exc)
            return 2
        self._engine = CaptureEngine(settings=self._settings_snapshot())
        self._register_handlers()
        self._send_envelope(
            "hello",
            {
                "agent_version": __version__,
                "boot_id": self._boot_id,
                "session_id": self.session_id,
                "capture": self._engine.health_dict(),
                "control_active": False,
            },
        )
        self._start_capture()
        self._start_heartbeat()
        try:
            self._main_loop()
        finally:
            self._shutdown()
        return 0

    def _main_loop(self) -> None:
        while not self._stop.is_set():
            packet = self._pipe.recv()
            if packet is None:
                log.info("服务管道已关闭，worker 退出")
                return
            header, payload = packet
            try:
                self._dispatch(header, payload)
            except Exception:  # noqa: BLE001 - 单条消息失败不能杀死会话
                log.exception("worker 处理消息失败 t=%r", header.get("t"))

    def _shutdown(self) -> None:
        self._stop.set()
        # A3：worker 退出（管道断开/会话注销/服务重拉）也必须抬起按键，
        # 否则用户会话里的 Ctrl/鼠标左键会保持按下直到重启。
        self._cleanup_control(reason="worker_shutdown", notify=False)
        self._executor.shutdown(wait=False, cancel_futures=True)
        pipe = self._pipe
        if pipe is not None:
            with contextlib.suppress(Exception):  # 关闭失败由系统回收句柄
                pipe.close()
        log.info("worker 已停止 session=%s", self.session_id)

    # -- 发送 ------------------------------------------------------------
    def _send_envelope(self, kind: str, body: dict[str, Any], payload: bytes = b"", *, fatal: bool = True) -> bool:
        """发一条管道信封；返回是否成功。

        A1：``PipePayloadTooLarge``（这条消息装不进一帧）**不**终止 worker——
        旧实现把所有发送异常都当成断链并 ``self._stop.set()``，于是一条超限的
        ``files`` 回执就能让整个会话的 worker 退出。超限交给调用方回明确失败
        回执；只有真正的管道错误才停 worker（等下次拉起）。
        """

        pipe = self._pipe
        if pipe is None:
            return False
        try:
            pipe.send(new_envelope(kind, body, ts_ms=_now_ms()), payload)
            return True
        except EnvelopeTooLarge as exc:
            log.error("发送 %s 到服务失败（单帧超限，worker 继续运行）：%s", kind, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - 管道断开时 worker 只能退出，由服务重拉
            log.warning("发送 %s 到服务失败：%s", kind, exc)
            if fatal:
                self._stop.set()
            return False

    def _send_frame(self, frame: Any) -> None:
        if self._pipe is None:
            return
        header = {
            "t": "frame",
            "device_ts_ms": int(frame.device_ts_ms),
            "session_id": self.session_id,
            "kind": frame.kind,
            "w": int(frame.width),
            "h": int(frame.height),
            "score": int(frame.score),
        }
        try:
            self._pipe.send(header, frame.payload)
        except Exception as exc:  # noqa: BLE001 - 帧发送失败由下一帧/链路 outbox 弥补
            log.warning("发送采集帧失败：%s", exc)

    def _send_control_frame(self, frame: Any) -> None:
        if self._pipe is None:
            return
        header = {
            "t": "ctrl",
            "session": self._control_session,
            "ts_ms": int(frame.device_ts_ms),
            "w": int(frame.width),
            "h": int(frame.height),
        }
        try:
            self._pipe.send(header, frame.payload)
        except Exception as exc:  # noqa: BLE001 - 控制帧丢失只影响实时画面
            log.debug("发送控制帧失败：%s", exc)

    # -- 处理器 ----------------------------------------------------------
    def _register_handlers(self) -> None:
        from ..tasks import fs

        def screenshot(command: Command) -> CommandResult:
            try:
                frame = self._engine.capture_now(session_id=self.session_id)
            except Exception as exc:  # noqa: BLE001 - 采集失败要变成可观测回执
                return CommandResult(ok=False, error=f"截图失败: {exc}")
            self._send_frame(frame)
            return CommandResult(
                ok=True,
                result={
                    "frame_ts": int(frame.device_ts_ms),
                    "width": int(frame.width),
                    "height": int(frame.height),
                    "bytes": len(frame.payload),
                },
            )

        self._dispatcher.register("screenshot", screenshot)
        self._dispatcher.register("collect_now", screenshot)
        fs.register_handlers(
            self._dispatcher,
            settings_provider=self._settings,
            now_ms=_now_ms,
        )
        self._dispatcher.register("log_tail", self._log_tail)

    def _log_tail(self, command: Command) -> CommandResult:
        lines = int(command.payload.get("lines") or 200)
        return CommandResult(ok=True, result={"lines": read_tail(lines, f"worker-{self.session_id}")})

    def _settings_snapshot(self) -> dict[str, Any]:
        """worker 当前生效的设置快照（只来自管道 + 本地默认值）。"""

        return dict(self._config_values)

    def _apply_remote_config(self, values: dict[str, Any]) -> None:
        """应用服务进程经管道下发的**非敏感**设置（A2）。

        剥敏感键复用 ``core.config.sanitize_settings``（与服务进程同一份实现），
        所以即使服务端配置视图写错，令牌也不会出现在 worker 内存里。worker
        **不落盘**：它没有 config.json，进程结束设置就没了，下次 hello 之后
        服务进程会重新广播。
        """

        cleaned = sanitize_settings(values)
        self._config_values = cleaned
        if self._engine is not None:
            self._engine.apply_settings(self._settings_snapshot())
        log.info("worker 已应用新配置：%s", sorted(cleaned))

    def _settings(self) -> dict[str, Any]:
        return self._settings_snapshot()

    # -- 分发 ------------------------------------------------------------
    def _dispatch(self, header: dict[str, Any], payload: bytes) -> None:
        kind = header.get("t")
        if header.get("v") == 3:
            try:
                envelope = envelope_from_dict(header)
            except EnvelopeError as exc:
                log.warning("收到非法管道信封（忽略）：%s", exc)
                return
            self._dispatch_envelope(envelope)
            return
        log.debug("忽略未知管道消息 t=%r payload=%s", kind, len(payload))

    def _dispatch_envelope(self, envelope: Any) -> None:
        kind = envelope.type
        body = envelope.body_dict
        if kind == "config":
            values = body.get("config")
            if isinstance(values, dict):
                self._apply_remote_config(values)
            return
        if kind == "command":
            self._submit_command(body)
            return
        if kind == "control":
            self._handle_control(body)
            return
        if kind == "ping":
            self._send_envelope("pong", {})
            return
        if kind in {"stop", "uninstall"}:
            log.info("收到停止指令 t=%s", kind)
            self._stop.set()
            return
        log.debug("忽略服务下行类型 %s", kind)

    def _submit_command(self, body: dict[str, Any]) -> None:
        try:
            # A6：worker 用**单调**时钟做预算（进程内自洽即可；跨进程的剩余量
            # 已由服务进程折算好）。
            command = Command.from_body(body, ts_ms=_monotonic_ms())
        except ValueError as exc:
            command_id = str(body.get("command_id") or "")
            log.warning("服务下发命令格式非法：%s", exc)
            if command_id:
                self._executor.submit(
                    self._send_result,
                    command_id,
                    CommandResult(ok=False, error=f"命令格式非法: {exc}", error_code="BAD_REQUEST"),
                )
            return
        if command.is_expired(_monotonic_ms()):
            log.warning("命令 %s 在入队前已超预算，直接回超时", command.id)
            self._executor.submit(self._send_result, command.id, expired_result(command))
            return
        self._executor.submit(self._execute_command, command)

    def _execute_command(self, command: Command) -> None:
        # A6：线程池排队期间可能早已超出预算（3 个 worker 线程被慢操作占满时
        # 最坏能等几十秒）。**执行前**再校验一次：超时的破坏性动作绝不落地。
        if command.is_expired(_monotonic_ms()):
            log.warning(
                "命令 %s 排队后已超预算（type=%s deadline_ms=%s），拒绝执行",
                command.id,
                command.type,
                command.deadline_ms,
            )
            self._send_result(command.id, expired_result(command))
            return
        started = time.monotonic()
        try:
            result = asyncio.run(self._dispatcher.dispatch(command))
        except Exception as exc:  # noqa: BLE001 - 任何执行异常都必须回执
            log.exception("命令 %s 执行失败", command.id)
            result = CommandResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._send_result(command.id, result, elapsed_ms=elapsed_ms)

    def _send_result(self, command_id: str, result: CommandResult, *, elapsed_ms: int = 0) -> None:
        """回执必须送达或变成明确的失败回执（A1）。

        一条超大回执（例如 ``files`` 读出的分片）装不进管道帧时，旧实现会让
        worker 退出、服务端只能等到命令超时。现在补发一条**最小**的失败回执，
        把"数据太大"变成服务端/前端立刻可见的结果。
        """

        body = result.to_body(command_id, device_time_ms=_now_ms(), elapsed_ms=elapsed_ms)
        if self._send_envelope("result", body):
            return
        if self._pipe is None:
            return
        fallback = CommandResult(
            ok=False,
            error="回执数据超过管道单帧上限（A1），请减小分片后重试",
            error_code=ERROR_BAD_REQUEST,
        )
        self._send_envelope(
            "result",
            fallback.to_body(command_id, device_time_ms=_now_ms(), elapsed_ms=elapsed_ms),
            fatal=False,
        )

    # -- 采集 ------------------------------------------------------------
    def _start_capture(self) -> None:
        self._capture_thread = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self._capture_thread.start()

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            self._engine.set_control_active(bool(self._control_session))
            try:
                frame = self._engine.poll()
            except Exception:  # noqa: BLE001 - 引擎内部已降级，这里兜住线程
                log.exception("采集循环异常")
                # 0.3.1（F36）：异常路径也要计入失败退避，否则下一轮
                # next_due_in_ms() 不知道刚失败过，会以 500ms 节奏空转。
                self._engine.record_failure()
                self._stop.wait(5.0)
                continue
            if frame is not None:
                self._send_frame(frame)
            delay_ms = max(500, int(self._engine.next_due_in_ms()))
            self._stop.wait(min(delay_ms, 60_000) / 1000.0)

    def _start_heartbeat(self) -> None:
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(WORKER_STATE_INTERVAL_S)
            if self._stop.is_set():
                return
            health = self._engine.health_dict()
            self._send_envelope(
                "heartbeat",
                {
                    "session_id": self.session_id,
                    "capture": health,
                    "control_active": bool(self._control_session),
                    "device_time_ms": _now_ms(),
                },
            )

    # -- 实时控制 --------------------------------------------------------
    def _handle_control(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "stop")
        session = str(body.get("session") or "")
        if action == "start":
            self._start_control(
                session=session,
                fps=int(body.get("fps") or 0),
                quality=int(body.get("quality") or 0),
                lease_ms=int(body.get("lease_ms") or 0),
            )
        elif action == "input":
            # A3（严格 session 匹配）：只接受**当前**控制会话的输入。旧实现
            # 不校验就注入，于是过期的控制台（或另一台机器）仍能把输入打到
            # 当前桌面——而 stop 又会把别人的流停掉。这里两条都堵住：
            #   * 非当前 session 的输入丢弃（记日志，不影响现有流）；
            #   * 租约到期先自清（含释放按键）再丢弃。
            if not self._control_session or session != self._control_session:
                log.warning(
                    "丢弃非当前控制会话的输入（收到 %s，当前 %s）",
                    session or "<空>",
                    self._control_session or "<无>",
                )
                return
            if self._control_expired():
                self._cleanup_control(reason="lease_expired", notify=True)
                return
            event = body.get("ev") or {}
            try:
                from ..tasks.input import handle_event

                if not handle_event(event, state=self._input_state):
                    log.debug("未识别的输入事件：%s", event.get("kind"))
            except ImportError as exc:  # pragma: no cover - 打包缺模块
                log.error("输入注入不可用：%s", exc)
            return
        else:
            # stop 只结束**匹配的**那个会话：别人的 stop（或缺少 session 的畸形
            # stop）不能停我们的流。空闲时（没有控制会话）cleanup 本身是幂等的。
            # 万一真的漏掉一条合法 stop，租约到期会兜底清理——这是 A3 的分层。
            if self._control_session and session != self._control_session:
                log.warning(
                    "忽略非当前控制会话的 stop（收到 %s，当前 %s）",
                    session or "<空>",
                    self._control_session,
                )
                return
            self._cleanup_control(reason=f"stop:{action}", notify=False)

    def _start_control(self, *, session: str, fps: int, quality: int, lease_ms: int = 0) -> None:
        self._cleanup_control(reason="restart", notify=False)
        if not session:
            log.warning("控制启动缺少 session 令牌，忽略")
            return
        self._control_session = session
        # A3：本地租约用单调时钟；lease_ms 由服务进程按它自己的 loop.time()
        # 下发剩余量，这里重新起算，避免两个进程的时钟基准不同。
        seconds = (lease_ms / 1000.0) if lease_ms > 0 else CONTROL_TTL_DEFAULT_S
        self._control_deadline = time.monotonic() + max(CONTROL_TTL_MIN_S, min(CONTROL_TTL_MAX_S, seconds))
        settings = self._settings_snapshot()
        control_fps = max(1, min(15, fps or int(settings.get("control_fps", 8) or 8)))
        control_quality = max(20, min(95, quality or int(settings.get("control_quality", 55) or 55)))
        self._control_stop.clear()
        engine = self._engine
        self._control_thread = threading.Thread(
            target=self._control_loop,
            args=(control_fps, control_quality),
            name="control",
            daemon=True,
        )
        engine.set_control_active(True)
        self._control_thread.start()
        log.info(
            "实时控制流开始 fps=%s quality=%s session=%s 租约=%.0fs",
            control_fps,
            control_quality,
            session,
            self._control_deadline - time.monotonic(),
        )

    def _control_expired(self) -> bool:
        deadline = self._control_deadline
        return bool(self._control_session) and deadline > 0 and time.monotonic() >= deadline

    def _cleanup_control(self, *, reason: str, notify: bool) -> None:
        """结束实时控制并释放**所有**按住的键/鼠标按钮（A3 的统一清理函数）。

        stop 报文、本地 TTL 到期、管道断开、worker 退出全部走这里——旧实现只有
        stop 分支会停线程，且从不释放按键，实测会卡住 Ctrl/鼠标左键。释放动作
        放在最后（线程先停），避免"释放后控制循环又按下"的竞态。
        """

        had_session = bool(self._control_session)
        session = self._control_session
        self._control_stop.set()
        thread = self._control_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._control_thread = None
        self._control_session = ""
        self._control_deadline = 0.0
        if self._engine is not None:
            self._engine.set_control_active(False)
        released = 0
        try:
            released = self._input_state.release_all()
        except Exception:  # noqa: BLE001 - 释放失败也必须继续收尾（记日志）
            log.exception("释放按住的按键失败")
        if had_session or released:
            log.info(
                "实时控制结束 reason=%s session=%s 已释放=%s",
                reason,
                session or "<无>",
                released,
            )
        if notify and had_session:
            # TTL 到期要上报，前端才能从"画面冻住"变成"控制已过期"。
            self._send_envelope(
                "event",
                {"event": "control_ended", "data": {"reason": reason, "session": session}},
                fatal=False,
            )

    def _control_loop(self, fps: int, quality: int) -> None:
        interval = 1.0 / max(1, fps)
        while not self._control_stop.is_set() and not self._stop.is_set():
            # A3：租约到期由**执行端**自己判定——即使服务进程的 stop 丢了、
            # 管道断了，这条循环也一定会停下来并释放按键。
            if self._control_expired():
                self._cleanup_control(reason="lease_expired", notify=True)
                return
            started = time.monotonic()
            try:
                frame = self._engine.capture_control_frame(session_id=self.session_id, quality=quality)
            except Exception as exc:  # noqa: BLE001 - 控制流失败不能终止 worker
                log.warning("控制帧采集失败：%s", exc)
                self._control_stop.wait(0.5)
                continue
            self._send_control_frame(frame)
            elapsed = time.monotonic() - started
            self._control_stop.wait(max(0.01, interval - elapsed))


# --------------------------------------------------------------- 工厂/入口 ---
def create_service_app(request_shutdown: Any) -> ServiceRuntime:
    """``win.service.AppFactory`` 的实现：创建服务应用（SYSTEM 模式）。"""

    return ServiceRuntime(request_shutdown=request_shutdown, service_mode=True)


def run_console() -> int:
    """前台运行服务应用（调试用，不经过 SCM）；不会跨会话拉起 worker。"""

    setup_logging("agent", console=True)
    runtime = ServiceRuntime(service_mode=False)
    try:
        asyncio.run(runtime._run())  # noqa: SLF001 - console 模式复用同一运行时
    except KeyboardInterrupt:
        log.info("前台运行被中断")
    finally:
        runtime._shutdown_workers()  # noqa: SLF001 - 确保 worker 不残留
    return 0


def run_worker(session_id: int, *, boot_id: str = "") -> int:
    return WorkerRuntime(session_id, boot_id=boot_id).run()


def _shutdown_machine(reboot: bool, *, timeout_s: float = 30.0) -> tuple[bool, str]:
    """执行重启/关机，返回 ``(是否真实提交, 失败原因)``（A6）。

    旧签名返回 ``None`` 且只记日志：调用方在 ``shutdown.exe`` 真正跑之前就回了
    ``ok``，失败也不会有人知道。现在把 ``shutdown.exe`` 的退出码带回回执——
    SYSTEM 权限下这一步就是最真实的"已受理关机"证据。
    """

    flag = "/r" if reboot else "/s"
    try:
        completed = subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
            ["shutdown.exe", flag, "/t", "5"],
            capture_output=True,
            timeout=max(1.0, float(timeout_s)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"shutdown.exe 在 {timeout_s:.0f}s 内未返回"
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("执行关机/重启失败: %s", exc)
        return False, str(exc)
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        log.error("关机/重启命令失败 rc=%s: %s", completed.returncode, stderr)
        return False, f"rc={completed.returncode} {stderr}".strip()
    return True, ""


__all__ = [
    "CAPTURE_HEALTH_STALE_MS",
    "CaptureHealthAggregator",
    "SESSION_SCAN_S",
    "ServiceRuntime",
    "WorkerRuntime",
    "create_service_app",
    "run_console",
    "run_worker",
]