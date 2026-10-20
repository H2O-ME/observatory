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
RECONNECT_BACKOFF_S = (2, 5, 10, 20, 30, 60, 120)
UPGRADE_PROBE_BASE_S = 120
FRAME_QUEUE_SIZE = 64
SPOOL_DRAIN_S = 20
MAX_COMMAND_CONCURRENCY = 4

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

        self._pipes: dict[int, PipeServer] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        self._launch_attempts: dict[int, tuple[int, float]] = {}
        self._sessions: dict[int, dict[str, Any]] = {}
        self._frame_queue: asyncio.Queue | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._control: dict[str, int] = {}
        self._updating = False
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

            level = min(self.level, LEVEL_POLL_SHORT)
            channel = build_channel(level, self.client, self._heartbeat_payload)
            self.channel = channel
            log.info("connecting with transport level=%s (%s)", level, level_name(level))

            run_task = asyncio.create_task(channel.run(self._handle_message, self.heartbeat_interval), name="channel")
            ready_task = asyncio.create_task(channel.ready.wait(), name="ready")
            try:
                done, pending = await asyncio.wait(
                    {run_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if ready_task in done and channel.ready.is_set():
                    self.level = level
                    self._connected = True
                    backoff = 0
                    log.info("connected via %s", level_name(level))
                    await self._report_state("connected")
                    if level != LEVEL_WS:
                        asyncio.create_task(self._upgrade_probe(level), name="upgrade")
                    await run_task
                    if not self._stopping.is_set():
                        log.warning("transport %s closed", level_name(level))
                        self.level = min(level + 1, LEVEL_POLL_SHORT)
                else:
                    error = None
                    if run_task.done():
                        try:
                            run_task.result()
                        except Exception as exc:  # noqa: BLE001
                            error = exc
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
                for task in (run_task, ready_task):
                    if not task.done():
                        task.cancel()
                try:
                    await channel.close()
                except Exception:  # noqa: BLE001
                    pass

            if self._stopping.is_set():
                break
            delay = RECONNECT_BACKOFF_S[min(backoff, len(RECONNECT_BACKOFF_S) - 1)]
            backoff += 1
            log.info("reconnecting in %.0fs", delay)
            await self._sleep_or_stop(delay)

    async def _upgrade_probe(self, current_level: int) -> None:
        """Periodically try to climb back to the WebSocket."""

        delay = UPGRADE_PROBE_BASE_S
        while not self._stopping.is_set() and self._connected and self.level != LEVEL_WS:
            await self._sleep_or_stop(delay)
            if self._stopping.is_set() or not self._connected:
                return
            if await self._probe_ws():
                log.info("websocket reachable again, upgrading from %s", level_name(current_level))
                self.level = LEVEL_WS
                channel = self.channel
                if channel is not None:
                    await channel.close()
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
            },
        }

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

    async def _handle_worker_result(self, session_id: int, header: dict) -> None:
        command_id = str(header.get("id") or "")
        if not command_id:
            return
        self._pending.pop(command_id, None)
        await self._send_result(
            command_id,
            bool(header.get("ok")),
            header.get("result") if isinstance(header.get("result"), dict) else None,
            header.get("error"),
        )

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
            await self._send_event("update_failed", {"version": version, "error": str(exc)})
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
            state.update(
                {
                    "username": item.get("username", ""),
                    "is_console": int(session_id) == console,
                }
            )
            self._ensure_worker(session_id)

        for session_id in list(self._sessions):
            if session_id not in active:
                log.info("session %s ended, stopping worker", session_id)
                self._stop_worker(session_id)

    def _ensure_worker(self, session_id: int) -> None:
        pipe = self._pipes.get(session_id)
        if pipe is None:
            pipe = PipeServer(session_id, lambda header, payload, sid=session_id: self._on_pipe_message(sid, header, payload))
            pipe.start()
            self._pipes[session_id] = pipe

        if pipe.connected.is_set():
            self._launch_attempts[session_id] = (0, time.monotonic())
            return

        process = self._processes.get(session_id)
        if process is not None and process.poll() is None:
            return

        attempts, last_attempt = self._launch_attempts.get(session_id, (0, 0.0))
        now = time.monotonic()
        if attempts and now - last_attempt < LAUNCH_COOLDOWN_S:
            return
        if attempts >= LAUNCH_RETRIES:
            log.error(
                "session %s: giving up after %s launch attempts (retrying after cooldown)",
                session_id,
                attempts,
            )
            self._launch_attempts[session_id] = (0, now)
            return

        log.info("launching worker for session %s (attempt %s)", session_id, attempts + 1)
        pid: int | None = None
        if self.service_mode:
            pid = launch_worker(session_id)
        if pid is None:
            process = spawn_local(worker_arguments(session_id))
            if process is not None:
                self._processes[session_id] = process
                self._launch_attempts[session_id] = (0, now)
                return
        self._launch_attempts[session_id] = (attempts + 1, now)
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
                asyncio.run_coroutine_threadsafe(self._handle_worker_result(session_id, header), loop)
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