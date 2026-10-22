"""The service-side brain: network, sessions, commands, updates.

Runs an asyncio loop on its own thread so the Windows service control handler stays
responsive. Everything that touches the network lives here; everything that touches
a desktop lives in ``worker.py`` inside the user's session.

The previous version crashed the whole service with a ``NameError`` after five
consecutive worker-launch failures because a constant was referenced but never
defined. Every retry/backoff constant is therefore defined once at the top of this
module, and the supervisor is written so a failure can only ever skip one scan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from . import __version__, sysinfo
from .client import AgentClient, BaseRouter, TransportError
from .config import ConfigStore, SPOOL_DIR, STAGING_DIR, AgentConfig
from .pipe_proto import encode
from .pipe_server import PipeServer
from .session_launch import launch_worker, spawn_local, worker_arguments
from .spool import FrameSpool
from .transport import (
    LEVEL_POLL_LONG,
    LEVEL_POLL_SHORT,
    LEVEL_WS,
    Channel,
    build_channel,
    level_name,
)
from .version import is_newer

log = logging.getLogger(__name__)

# ---- tuning (single definition point; see the module docstring) --------------
SESSION_SCAN_S = 5.0
LAUNCH_RETRIES = 5
LAUNCH_COOLDOWN_S = 30.0
#: 连续拉起失败后的递进退避上限：30s → 60s → … → 30 分钟。没有递进时，一台没有
#: 真实用户令牌的残留会话会每 30 秒重试一次，日志刷屏且白耗 CPU（设备 1 实测）。
LAUNCH_BACKOFF_MAX_S = 1800.0
RECONNECT_BACKOFF_S = (2, 5, 10, 20, 30, 60, 120)
UPGRADE_PROBE_BASE_S = 120
FRAME_QUEUE_SIZE = 64
SPOOL_DRAIN_S = 20
MAX_COMMAND_CONCURRENCY = 4
#: Retry backoff for a failed update: 5, 10, 20, ... capped at 30 minutes. Without
#: it a broken helper re-downloaded the whole package on every heartbeat.
UPDATE_RETRY_BASE_S = 300.0
UPDATE_RETRY_MAX_S = 1800.0

#: Commands the service answers itself. Everything else is delegated to a worker
#: because it needs the interactive desktop.
SERVICE_COMMANDS = frozenset(
    {"update", "uninstall", "restart_agent", "reload_config", "sysinfo", "log_tail", "reboot", "poweroff"}
)
SERVICE_ONLY_IGNORE_WORKER = frozenset({"update", "uninstall", "restart_agent", "reload_config", "reboot", "poweroff"})


class AgentRunner:
    def __init__(self, *, service_mode: bool = True) -> None:
        self.service_mode = service_mode
        self.store = ConfigStore()
        config = self.store.config
        self.router = BaseRouter(config.base_urls)
        self.client = AgentClient(self.router, config.token)
        self.spool = FrameSpool(SPOOL_DIR)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._stop_event: asyncio.Event | None = None

        self.channel: Channel | None = None
        self.level = LEVEL_WS
        self._connected = False
        #: 升级到 WebSocket 的显式信号。探测成功后置位，``_network_loop`` 据此
        #: 主动结束当前轮询通道并以 ``LEVEL_WS`` 重连，而不是等轮询自己失败后再
        #: 被降级逻辑把级别覆盖回 short-poll。
        self._upgrade_requested = asyncio.Event()

        self._pipes: dict[int, PipeServer] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        #: ``session_id -> (连续拉起失败次数, 上次尝试的 monotonic 时间)``。失败
        #: 次数只增不清，冷却时间由 :meth:`_launch_backoff` 递进计算。
        self._launch_attempts: dict[int, tuple[int, float]] = {}
        self._sessions: dict[int, dict[str, Any]] = {}
        self._frame_queue: asyncio.Queue | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._control: dict[str, int] = {}
        self._updating = False
        #: ``version -> (failed_attempts, monotonic_time)``; drives the retry backoff
        #: so a broken update does not re-download the package every heartbeat.
        self._update_failures: dict[str, tuple[int, float]] = {}
        self._last_update_check = 0.0

        #: Set by ``service.py`` so the uninstaller/updater can stop the service.
        self.on_shutdown: Callable[[str], None] | None = None
        self.started_at_ms = int(time.time() * 1000)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="agent-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _wake(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            log.exception("runner crashed")
        finally:
            try:
                self._loop.run_until_complete(self._cleanup())
                self._loop.run_until_complete(self.client.close())
            except Exception:  # noqa: BLE001
                pass
            self._loop.close()

    async def _main(self) -> None:
        log.info("runner starting version=%s service_mode=%s", __version__, self.service_mode)
        self._stop_event = asyncio.Event()
        self._frame_queue = asyncio.Queue(maxsize=FRAME_QUEUE_SIZE)

        tasks = [
            asyncio.create_task(self._session_loop(), name="sessions"),
            asyncio.create_task(self._upload_loop(), name="uploader"),
            asyncio.create_task(self._spool_loop(), name="spooler"),
        ]
        try:
            await self._network_loop()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _cleanup(self) -> None:
        log.info("runner stopping")
        await self._stop_all_workers()
        channel = self.channel
        self.channel = None
        if channel is not None:
            try:
                await channel.close()
            except Exception:  # noqa: BLE001
                pass

    # -- network ladder ----------------------------------------------------
    async def _network_loop(self) -> None:
        backoff = 0
        while not self._stopping.is_set():
            if not await self._ensure_enrolled():
                await asyncio.sleep(15)
                continue

            if self._upgrade_requested.is_set():
                # 上一轮结束时探测恰好成功：不论那一轮做了怎样的降级判断，都按
                # 升级结果用 WebSocket 重连（信号在这里消费掉）。
                self.level = LEVEL_WS
                self._upgrade_requested.clear()

            level = min(self.level, LEVEL_POLL_SHORT)
            channel = build_channel(level, self.client, self._heartbeat_payload)
            self.channel = channel
            log.info("connecting with transport level=%s (%s)", level, level_name(level))

            run_task = asyncio.create_task(channel.run(self._handle_message, self.heartbeat_interval), name="channel")
            ready_task = asyncio.create_task(channel.ready.wait(), name="ready")
            probe_task: asyncio.Task | None = None
            upgraded = False
            try:
                done, _pending = await asyncio.wait(
                    {run_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if ready_task in done and channel.ready.is_set() and not run_task.done():
                    # The channel is live: it ends on its own when the peer closes.
                    # Do NOT cancel run_task here — that would tear the session down
                    # the moment it became usable.
                    self.level = level
                    self._connected = True
                    backoff = 0
                    log.info("connected via %s", level_name(level))
                    await self._report_state("connected")
                    if level == LEVEL_WS:
                        try:
                            await run_task
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            log.warning("transport %s ended: %s", level_name(level), exc)
                    else:
                        probe_task = asyncio.create_task(self._upgrade_probe(level), name="upgrade")
                        upgraded = await self._wait_channel_end(run_task, level)
                    if upgraded:
                        # 通道是被升级信号主动结束的：保留 LEVEL_WS，跳过降级步进。
                        log.info("switching transport to ws (upgrade confirmed)")
                    elif not self._stopping.is_set():
                        log.warning("transport %s closed", level_name(level))
                        if self.level == LEVEL_WS:
                            # 探测恰好在这一刻成功：保留升级结果，事件在下一轮被
                            # 消费，绝不能被下面的降级步进覆盖。
                            upgraded = True
                        else:
                            self.level = min(level + 1, LEVEL_POLL_SHORT)
                else:
                    # The channel died before it was usable.
                    error = None
                    if run_task.done():
                        try:
                            run_task.result()
                        except Exception as exc:  # noqa: BLE001
                            error = exc
                    else:
                        run_task.cancel()
                    log.warning("transport %s unavailable: %s", level_name(level), error or "closed")
                    self.level = min(level + 1, LEVEL_POLL_SHORT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("transport %s error: %s", level_name(level), exc)
                self.level = min(level + 1, LEVEL_POLL_SHORT)
            finally:
                self._connected = False
                self.channel = None
                pending = [task for task in (run_task, ready_task, probe_task) if task is not None]
                for task in pending:
                    if not task.done():
                        task.cancel()
                # 收尾被取消的 task，避免留下悬挂协程或 "Task was destroyed" 警告。
                try:
                    await asyncio.gather(*pending, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                try:
                    await channel.close()
                except Exception:  # noqa: BLE001
                    pass

            if self._stopping.is_set():
                break
            if upgraded:
                # 升级是主动切换，不该按断线重连的退避等待，下一轮直接建 WebSocket。
                continue
            delay = RECONNECT_BACKOFF_S[min(backoff, len(RECONNECT_BACKOFF_S) - 1)]
            backoff += 1
            log.info("reconnecting in %.0fs", delay)
            await self._sleep_or_stop(delay)

    async def _wait_channel_end(self, run_task: asyncio.Task, level: int) -> bool:
        """等待在线通道结束，或收到 WebSocket 升级信号。

        返回 ``True`` 表示通道是被升级信号主动结束的（调用方必须保留
        ``LEVEL_WS``，不能再降级）；``False`` 表示通道自己断了（调用方按阶梯降级）。
        """

        request_task = asyncio.create_task(self._upgrade_requested.wait(), name="upgrade-request")
        try:
            done, _pending = await asyncio.wait(
                {run_task, request_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            request_task.cancel()
            raise
        upgrade = request_task in done and not request_task.cancelled() and request_task.result()
        if not request_task.done():
            request_task.cancel()
            try:
                await request_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if upgrade:
            # 升级：显式结束当前轮询通道。PollChannel 没有关闭语义（继承的
            # ``Channel.close`` 是 no-op，``run`` 也不感知关闭），只能 cancel。
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return True
        # 通道自己结束：把异常写进日志，交由调用方降级。
        try:
            run_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("transport %s ended: %s", level_name(level), exc)
        return False

    async def _upgrade_probe(self, current_level: int) -> None:
        """Periodically try to climb back to the WebSocket."""

        delay = UPGRADE_PROBE_BASE_S
        while not self._stopping.is_set() and self._connected and self.level != LEVEL_WS:
            await self._sleep_or_stop(delay)
            if self._stopping.is_set() or not self._connected:
                return
            if await self._probe_ws():
                log.info("websocket reachable again, upgrading from %s", level_name(current_level))
                # 先落到 LEVEL_WS 再发信号：``_network_loop`` 收到信号后会 cancel
                # 当前轮询通道并跳过降级步进，下一轮直接用 WsChannel 重建连接。
                self.level = LEVEL_WS
                self._upgrade_requested.set()
                return
            delay = min(delay * 2, 1800)

    async def _probe_ws(self) -> bool:
        from .transport import WsChannel

        probe = WsChannel(self.client, self._heartbeat_payload)
        try:
            await probe.probe()
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            await probe.close()

    async def _sleep_or_stop(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self._stopping.is_set() and time.monotonic() < end:
            await asyncio.sleep(min(1.0, end - time.monotonic()))

    # -- enrollment --------------------------------------------------------
    async def _ensure_enrolled(self) -> bool:
        config = self.store.config
        if config.token:
            return True
        code = config.enroll_code or os.environ.get("OBS_ENROLL_CODE", "")
        if not code:
            log.error("设备尚未注册，且没有注册码（请用安装脚本安装）")
            return False
        try:
            info = await asyncio.to_thread(sysinfo.collect, __version__)
            response = await self.client.enroll(
                code=code,
                uid=config.device_uid,
                hostname=info.get("hostname", ""),
                os_name=info.get("os_name", ""),
                os_version=f"{info.get('os_release', '')} {info.get('os_version', '')}".strip(),
                arch=info.get("arch", ""),
                agent_version=__version__,
                sysinfo=info,
                sessions=info.get("sessions") or [],
            )
            self.store.update(
                token=response.get("token", ""),
                base_urls=response.get("base_urls") or config.base_urls,
                server_version=response.get("server_version", ""),
            )
            self.router.set_urls(self.store.config.base_urls)
            self.client.token = self.store.config.token
            self.store.apply_server_config(response.get("config") or {}, response.get("server_time_ms") or 0)
            log.info("enrolled as %s", self.store.config.device_uid)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment failed: %s", exc)
            return False

    async def _reregister(self) -> None:
        log.warning("device token rejected, re-enrolling")
        self.store.update(token="")
        self.client.token = ""

    # -- messages ----------------------------------------------------------
    @property
    def heartbeat_interval(self) -> int:
        return max(5, int(self.store.config.setting("heartbeat_interval_s", 20)))

    def _heartbeat_payload(self) -> dict[str, Any]:
        sessions = []
        for session_id, state in list(self._sessions.items()):
            sessions.append(
                {
                    "session_id": session_id,
                    "username": state.get("username", ""),
                    "state": "active" if state.get("connected") else "connecting",
                    "is_console": bool(state.get("is_console")),
                    "capturable": state.get("connectable", True),
                }
            )
        return {
            "agent_version": __version__,
            "sysinfo": sysinfo.quick(),
            "state": {
                "transport": level_name(self.level),
                "connected": self._connected,
                "sessions": sessions,
                "spool": self.spool.pending(),
                "control": sorted(self._control.keys()),
                "uptime_s": max(0, (int(time.time() * 1000) - self.started_at_ms) // 1000),
                "updating": self._updating,
                "capture": self._capture_health(),
            },
        }

    def _capture_health(self) -> dict[str, Any]:
        """Whether this host currently has a desktop the agent can capture.

        Without this the console only saw "device online, no frames", which is
        indistinguishable from a broken capture pipeline. The distinct cases are
        reported explicitly instead.
        """

        workers = [sid for sid, pipe in self._pipes.items() if pipe.connected.is_set()]
        errors = [
            str(state.get("launch_error"))
            for state in self._sessions.values()
            if state.get("launch_error")
        ]
        # 只有带真实登录用户名的会话才可能有可采集的桌面。残留会话（例如
        # session 1，WTSQueryUserToken 返回 1008）永远采不了屏，如实报
        # no_session，不能一直显示"正在启动"。
        has_user = any(str(state.get("username") or "").strip() for state in self._sessions.values())
        if workers:
            state, reason = "ok", ""
        elif errors:
            state, reason = "blocked", errors[0]
        elif not self._sessions or not has_user:
            state, reason = "no_session", "当前没有已登录的交互式桌面（控制台未登录或已注销）"
        else:
            state, reason = "starting", "正在启动采屏进程"
        return {
            "state": state,
            "reason": reason,
            "workers": sorted(workers),
            "sessions": len(self._sessions),
        }

    def _mark_capture_blocked(self, session_id: int, error: str) -> None:
        state = self._sessions.setdefault(session_id, {})
        state["connectable"] = False
        state["launch_error"] = error

    async def _handle_message(self, message: dict) -> None:
        kind = message.get("t")

        if kind in {"welcome", "pong", "config"}:
            config = message.get("config")
            if isinstance(config, dict):
                self.store.apply_server_config(config, int(message.get("server_time_ms") or 0))
                self._broadcast_worker_config()
            update = message.get("update")
            if isinstance(update, dict):
                await self._maybe_update(update)
            if kind == "welcome":
                log.info(
                    "welcome from server version=%s transport=%s",
                    message.get("server_version"),
                    level_name(self.level),
                )

        elif kind == "command":
            command = message.get("command") or {}
            await self._dispatch_command(command)

        elif kind == "ctrl_start":
            await self._forward_to_worker(
                {"t": "ctrl_start", "session": message.get("session"), "fps": message.get("fps"), "quality": message.get("quality")}
            )

        elif kind == "ctrl_in":
            await self._forward_to_worker(
                {"t": "ctrl_in", "session": message.get("session"), "ev": message.get("ev") or {}}
            )

        elif kind == "ctrl_stop":
            session_token = str(message.get("session") or "")
            self._control.pop(session_token, None)
            await self._forward_to_worker({"t": "ctrl_stop", "session": session_token})

        elif kind == "ping":
            await self._send_json({"t": "pong", "ts": int(time.time() * 1000)})

        elif kind == "_binary":
            log.debug("unexpected binary message from server")

        else:
            log.debug("unknown server message: %s", kind)

    # -- outbound helpers --------------------------------------------------
    async def _send_json(self, payload: dict) -> bool:
        channel = self.channel
        if channel is not None and await channel.send_json(payload):
            return True
        kind = payload.get("t")
        if kind == "result":
            return await self.client.send_result(
                str(payload.get("command_id") or ""),
                bool(payload.get("ok")),
                payload.get("result") if isinstance(payload.get("result"), dict) else None,
                payload.get("error"),
            )
        if kind == "event":
            # Events are advisory; polling clients get them on the next heartbeat.
            return False
        return False

    async def _send_result(self, command_id: str, ok: bool, result: dict | None, error: str | None) -> None:
        payload = {
            "t": "result",
            "command_id": command_id,
            "ok": ok,
            "result": result,
            "error": error,
        }
        channel = self.channel
        if channel is not None and await channel.send_json(payload):
            return
        if not await self.client.send_result(command_id, ok, result, error):
            log.warning("could not deliver result for %s", command_id)

    async def _send_event(self, name: str, data: dict | None = None) -> None:
        channel = self.channel
        if channel is not None:
            await channel.send_json({"t": "event", "event": name, "data": data or {}})

    # -- commands ----------------------------------------------------------
    async def _dispatch_command(self, command: dict) -> None:
        command_id = str(command.get("id") or "")
        command_type = str(command.get("type") or "")
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        if not command_id:
            return
        log.info("command %s type=%s", command_id, command_type)

        if command_type in SERVICE_COMMANDS:
            asyncio.create_task(self._run_service_command(command_id, command_type, payload))
        else:
            await self._delegate_to_worker(command_id, command_type, payload)

    async def _run_service_command(self, command_id: str, command_type: str, payload: dict) -> None:
        try:
            if command_type == "reload_config":
                async with self._config_refresh() as config:
                    await self._send_result(command_id, True, {"config": config}, None)
            elif command_type == "sysinfo":
                info = await asyncio.to_thread(sysinfo.collect, __version__)
                await self._send_result(command_id, True, {"sysinfo": info}, None)
            elif command_type == "log_tail":
                from .logging_setup import read_tail

                lines = int(payload.get("lines") or 200)
                await self._send_result(command_id, True, {"lines": await asyncio.to_thread(read_tail, lines, "agent")}, None)
            elif command_type == "update":
                version = str(payload.get("version") or "")
                await self._send_result(command_id, True, {"started": True, "version": version}, None)
                await self._maybe_update({"version": version, "_forced": True})
            elif command_type == "uninstall":
                await self._send_result(command_id, True, {"uninstalling": True}, None)
                await self._begin_uninstall()
            elif command_type == "restart_agent":
                await self._send_result(command_id, True, {"restarting": True}, None)
                self._request_shutdown("restart_agent")
            elif command_type in {"reboot", "poweroff"}:
                await self._send_result(command_id, True, {"ok": True}, None)
                await asyncio.to_thread(_shutdown_machine, command_type == "reboot")
            else:
                await self._send_result(command_id, False, None, f"未知命令: {command_type}")
        except Exception as exc:  # noqa: BLE001
            log.exception("service command %s failed", command_type)
            await self._send_result(command_id, False, None, str(exc))

    class _ConfigRefresh:
        def __init__(self, runner: "AgentRunner") -> None:
            self.runner = runner
            self.config: dict[str, Any] = {}

        async def __aenter__(self) -> dict[str, Any]:
            self.config = dict(self.runner.store.config.settings)
            return self.config

        async def __aexit__(self, *args) -> None:
            return None

    def _config_refresh(self) -> "AgentRunner._ConfigRefresh":
        return AgentRunner._ConfigRefresh(self)

    async def _delegate_to_worker(self, command_id: str, command_type: str, payload: dict) -> None:
        session_id = self._pick_session()
        pipe = self._pipes.get(session_id) if session_id is not None else None
        if session_id is None or pipe is None or not pipe.connected.is_set():
            await self._send_result(
                command_id,
                False,
                None,
                "没有可用的交互式会话，受控端当前无法执行该操作",
            )
            return
        self._pending[command_id] = {"session_id": session_id, "type": command_type}
        if not pipe.send({"t": "cmd", "id": command_id, "type": command_type, "payload": payload}):
            self._pending.pop(command_id, None)
            await self._send_result(command_id, False, None, "与用户会话的管道已断开")

    def _pick_session(self) -> int | None:
        console = sysinfo.console_session_id()
        if console in self._pipes and self._pipes[console].connected.is_set():
            return console
        for session_id, pipe in self._pipes.items():
            if pipe.connected.is_set():
                return session_id
        return None

    async def _handle_worker_result(self, session_id: int, header: dict, payload: bytes = b"") -> None:
        command_id = str(header.get("id") or "")
        if not command_id:
            return
        self._pending.pop(command_id, None)
        result = header.get("result") if isinstance(header.get("result"), dict) else None
        if result is not None and payload:
            # A large file read travels as the pipe payload to stay under the
            # header limit; put it back in the shape the console expects.
            import base64

            result = {**result, "data": base64.b64encode(payload).decode("ascii")}
            result.pop("data_encoding", None)
        await self._send_result(command_id, bool(header.get("ok")), result, header.get("error"))

    # -- updates -----------------------------------------------------------
    async def _maybe_update(self, info: dict) -> None:
        if self._updating:
            return
        version = str(info.get("version") or "")
        if not version:
            return
        forced = bool(info.get("_forced"))
        if not forced:
            config = self.store.config
            if not bool(config.setting("auto_update", True)):
                log.info("auto-update disabled, skipping %s", version)
                return
            if not is_newer(version, __version__):
                return

        # A failed attempt used to be retried on the very next heartbeat, which
        # re-downloaded the whole package every few seconds and hammered both the
        # device and the server. Back off, and repeat the same failure less and
        # less often while still eventually recovering.
        failure = self._update_failures.get(version)
        if failure is not None and not forced:
            attempts, last = failure
            wait_s = min(UPDATE_RETRY_MAX_S, UPDATE_RETRY_BASE_S * (2 ** (attempts - 1)))
            if time.monotonic() - last < wait_s:
                return
            log.info(
                "retrying update to %s after %ss (attempt %s failed before)",
                version,
                wait_s,
                attempts,
            )

        if not self._connected:
            log.info("deferring update to %s until connected", version)
            return

        self._updating = True
        log.info("starting update to %s (current %s)", version, __version__)
        await self._send_event("update_started", {"version": version})
        try:
            from . import updater

            await updater.apply_update(
                client=self.client,
                version=version,
                sha256=str(info.get("sha256") or ""),
                staging_dir=STAGING_DIR,
                on_event=self._send_event,
            )
            await self._send_event("update_ready", {"version": version})
            self._request_shutdown(f"update:{version}")
        except Exception as exc:  # noqa: BLE001
            log.exception("update failed")
            attempts = (failure[0] + 1) if failure else 1
            self._update_failures[version] = (attempts, time.monotonic())
            await self._send_event(
                "update_failed", {"version": version, "error": str(exc), "attempt": attempts}
            )
            self._updating = False

    # -- uninstall ---------------------------------------------------------
    async def _begin_uninstall(self) -> None:
        log.info("remote uninstall requested")
        await self._send_event("uninstall_started", {})
        try:
            await self.client.notify_uninstalled()
        except Exception:  # noqa: BLE001
            pass
        from . import uninstall

        try:
            await asyncio.to_thread(uninstall.spawn_purge_helper)
        except Exception as exc:  # noqa: BLE001
            log.error("could not start uninstaller: %s", exc)
        self._request_shutdown("uninstall")

    def _request_shutdown(self, reason: str) -> None:
        log.info("shutdown requested: %s", reason)
        callback = self.on_shutdown
        if callback is not None:
            try:
                callback(reason)
            except Exception:  # noqa: BLE001
                log.exception("shutdown callback failed")
        self._stopping.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake)
            except RuntimeError:
                pass

    # -- workers -----------------------------------------------------------
    async def _session_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._scan_sessions()
            except Exception:  # noqa: BLE001 - a failing scan must never stop the loop
                log.exception("session scan failed")
            await self._sleep_or_stop(SESSION_SCAN_S)

    async def _scan_sessions(self) -> None:
        sessions = await asyncio.to_thread(sysinfo.list_sessions)
        console = sysinfo.console_session_id()
        active: dict[int, dict] = {}
        for item in sessions:
            if item.get("state") in {"active", "connected"}:
                active[int(item["session_id"])] = item

        for session_id, item in active.items():
            state = self._sessions.setdefault(session_id, {})
            username = str(item.get("username") or "").strip()
            previous = str(state.get("username") or "")
            state.update(
                {
                    "username": username,
                    "is_console": int(session_id) == console,
                }
            )
            if not username:
                # 没有登录用户名的会话拿不到用户令牌（WTSQueryUserToken 返回
                # 1008），拉起 worker 必然失败；它本身也没有可采集的桌面，属
                # 环境限制。只记录状态供 heartbeat 的 capture 上报，不再拉起。
                state["connectable"] = False
                self._launch_attempts.pop(session_id, None)
                if previous:
                    log.info("session %s 没有登录用户，跳过采屏进程（无法采集屏幕）", session_id)
                continue
            state["connectable"] = True
            self._ensure_worker(session_id)

        # Only a session that this loop itself tracks may be retired here. Worker
        # hellos also report listener entries such as RDP-Tcp (session ids above
        # 65535, state ``listen``); treating those as managed sessions made every
        # scan "end" them and kill the worker that had just connected.
        for session_id in list(self._sessions):
            if session_id in active:
                continue
            if not self._manages_session(session_id):
                self._sessions.pop(session_id, None)
                continue
            log.info("session %s ended, stopping worker", session_id)
            self._stop_worker(session_id)

    def _manages_session(self, session_id: int) -> bool:
        """True when this process owns a worker (or pipe) for ``session_id``."""

        return session_id in self._pipes or session_id in self._processes

    def _launch_backoff(self, failures: int) -> float:
        """连续拉起失败 ``failures`` 次后的冷却秒数：30s → 60s → … → 30 分钟。

        没有递进时，一个永远拿不到用户令牌的残留会话会每 30 秒重试一次，日志
        每 30 秒刷一行、白耗 CPU（设备 1 实测持续数小时）。
        """

        if failures <= 0:
            return 0.0
        # 30 * 2**10 已远超上限，限制指数避免大整数运算。
        exponent = min(failures - 1, 10)
        return min(LAUNCH_COOLDOWN_S * (2**exponent), LAUNCH_BACKOFF_MAX_S)

    def _note_launch_failure(self, session_id: int, failures: int, now: float) -> None:
        """记录一次拉起失败；连续失败次数只增不清，供递进退避使用。"""

        self._launch_attempts[session_id] = (failures, now)

    def _ensure_worker(self, session_id: int) -> None:
        pipe = self._pipes.get(session_id)
        if pipe is None:
            pipe = PipeServer(session_id, lambda header, payload, sid=session_id: self._on_pipe_message(sid, header, payload))
            pipe.start()
            self._pipes[session_id] = pipe

        if pipe.connected.is_set():
            # 成功连上即重置退避与失败标记，下次断开从最短冷却重新开始。
            self._launch_attempts.pop(session_id, None)
            self._sessions.setdefault(session_id, {}).pop("launch_error", None)
            return

        process = self._processes.get(session_id)
        if process is not None and process.poll() is None:
            return

        failures, last_attempt = self._launch_attempts.get(session_id, (0, 0.0))
        now = time.monotonic()
        cooldown = self._launch_backoff(failures)
        if failures and now - last_attempt < cooldown:
            return
        if failures >= LAUNCH_RETRIES:
            # 快速重试阶段已经结束，只在退避到期真正重试时留下一行日志，
            # 不再每 30 秒重复 "launching/giving up"。
            log.warning(
                "session %s: 已连续 %s 次拉起失败，按递进退避重试（本次冷却 %.0fs）",
                session_id,
                failures,
                cooldown,
            )

        log.info("launching worker for session %s (attempt %s)", session_id, failures + 1)
        pid: int | None = None
        if self.service_mode:
            pid = launch_worker(session_id)
        if pid is None:
            if self.service_mode:
                # A service lives in session 0. Spawning the worker locally there
                # yields a process with no desktop: it connects to the pipe and
                # reports frames it cannot actually capture, which is worse than
                # no worker at all. Record the failure and retry with backoff
                # instead of silently degrading.
                self._note_launch_failure(session_id, failures + 1, now)
                self._mark_capture_blocked(session_id, "无法进入用户会话，屏幕采集不可用")
                log.error(
                    "session %s: create-process-as-user failed, capture unavailable "
                    "(attempt %s, next retry in %.0fs)",
                    session_id,
                    failures + 1,
                    self._launch_backoff(failures + 1),
                )
                return
            process = spawn_local(worker_arguments(session_id))
            if process is not None:
                self._processes[session_id] = process
                self._launch_attempts.pop(session_id, None)
                return
        self._note_launch_failure(session_id, failures + 1, now)
        self._sessions.setdefault(session_id, {})["launched_pid"] = pid

    def _stop_worker(self, session_id: int) -> None:
        pipe = self._pipes.pop(session_id, None)
        if pipe is not None:
            try:
                pipe.send({"t": "stop"})
            except Exception:  # noqa: BLE001
                pass
            pipe.stop()
        process = self._processes.pop(session_id, None)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.pop(session_id, None)
        self._launch_attempts.pop(session_id, None)

    async def _stop_all_workers(self) -> None:
        for session_id in list(self._pipes):
            self._stop_worker(session_id)

    def _broadcast_worker_config(self) -> None:
        config = dict(self.store.config.settings)
        for pipe in list(self._pipes.values()):
            if pipe.connected.is_set():
                pipe.send({"t": "config", "config": config})

    # -- worker -> service -------------------------------------------------
    def _on_pipe_message(self, session_id: int, header: dict, payload: bytes) -> None:
        """Called on the pipe thread: hop to the asyncio loop."""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        kind = header.get("t")
        try:
            if kind == "frame":
                if self._frame_queue is not None:
                    loop.call_soon_threadsafe(self._enqueue_frame, header, payload)
                else:
                    self.spool.put(header, payload)
            elif kind == "ctrl":
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._forward_ctrl(header, payload)))
            elif kind == "result":
                asyncio.run_coroutine_threadsafe(
                    self._handle_worker_result(session_id, header, payload), loop
                )
            elif kind == "hello":
                state = self._sessions.setdefault(session_id, {})
                state["connected"] = True
                state["agent_version"] = header.get("agent_version", "")
                state["pid"] = header.get("pid")
                log.info("worker hello session=%s version=%s", session_id, header.get("agent_version"))
                if header.get("sessions"):
                    loop.call_soon_threadsafe(self._store_sessions, header["sessions"])
            elif kind == "state":
                state = self._sessions.setdefault(session_id, {})
                state["connected"] = True
                state["control_active"] = bool(header.get("control_active"))
            elif kind == "pong":
                pass
        except Exception:  # noqa: BLE001
            log.exception("pipe message handling failed session=%s", session_id)

    def _enqueue_frame(self, header: dict, payload: bytes) -> None:
        queue_ref = self._frame_queue
        if queue_ref is None:
            self.spool.put(header, payload)
            return
        try:
            queue_ref.put_nowait((header, payload))
        except asyncio.QueueFull:
            self.spool.put(header, payload)

    def _store_sessions(self, sessions: list) -> None:
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session_id = int(item.get("session_id") or 0)
            if session_id:
                state = self._sessions.setdefault(session_id, {})
                state["username"] = item.get("username", "")
                state["is_console"] = bool(item.get("is_console"))

    async def _forward_ctrl(self, header: dict, payload: bytes) -> None:
        channel = self.channel
        if channel is None:
            return
        session_token = str(header.get("session") or "")
        if not session_token:
            return
        self._control[session_token] = int(header.get("session_id") or 0)
        outbound = {key: value for key, value in header.items() if key != "len"}
        try:
            await channel.send_bytes(encode(outbound, payload))
        except Exception as exc:  # noqa: BLE001
            log.debug("control frame forward failed: %s", exc)

    async def _forward_to_worker(self, header: dict) -> None:
        session_id = self._pick_session()
        pipe = self._pipes.get(session_id) if session_id is not None else None
        if pipe is None or not pipe.connected.is_set():
            log.debug("no worker to forward %s", header.get("t"))
            return
        pipe.send(header)

    # -- frame upload ------------------------------------------------------
    async def _upload_loop(self) -> None:
        assert self._frame_queue is not None
        while not self._stopping.is_set():
            try:
                header, payload = await self._frame_queue.get()
            except asyncio.CancelledError:
                raise
            ok = await self.client.upload_frame(
                payload,
                ts_ms=int(header.get("ts") or 0),
                session_id=int(header.get("session_id") or 0),
                kind=str(header.get("kind") or "screen"),
            )
            if not ok:
                self.spool.put(header, payload)
                log.info("frame upload failed, spooled (pending=%s)", self.spool.pending())

    async def _spool_loop(self) -> None:
        while not self._stopping.is_set():
            await self._sleep_or_stop(SPOOL_DRAIN_S)
            if self._stopping.is_set():
                return
            try:
                batch = await asyncio.to_thread(self.spool.pop_batch, 4)
            except Exception:  # noqa: BLE001
                continue
            for header, payload, path in batch:
                ok = await self.client.upload_frame(
                    payload,
                    ts_ms=int(header.get("ts") or 0),
                    session_id=int(header.get("session_id") or 0),
                    kind=str(header.get("kind") or "screen"),
                )
                if ok:
                    await asyncio.to_thread(self.spool.ack, path)
                else:
                    break
            await asyncio.to_thread(self.spool.cleanup)

    async def _report_state(self, stage: str) -> None:
        await self._send_event(
            "transport",
            {"stage": stage, "level": level_name(self.level), "connected": self._connected},
        )

    # -- introspection -----------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "transport": level_name(self.level),
            "connected": self._connected,
            "service_mode": self.service_mode,
            "device_uid": self.store.config.device_uid,
            "sessions": sorted(self._pipes.keys()),
            "spool": self.spool.pending(),
            "updating": self._updating,
        }


def _shutdown_machine(reboot: bool) -> None:
    """Reboot/shutdown through the documented API (SYSTEM has the privilege)."""

    try:
        import win32api
        import win32security
        import win32con

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY
        )
        win32security.AdjustTokenPrivileges(
            token, False, [(win32security.LookupPrivilegeValue(None, win32security.SE_SHUTDOWN_NAME), win32con.SE_PRIVILEGE_ENABLED)]
        )
        win32api.InitiateSystemShutdown(None, "Observatory 远程指令", 5, True, reboot)
    except Exception as exc:  # noqa: BLE001
        log.error("shutdown failed: %s", exc)


_ = (queue, AgentConfig)

__all__ = ["AgentRunner", "LAUNCH_COOLDOWN_S", "LAUNCH_RETRIES", "SERVICE_COMMANDS"]