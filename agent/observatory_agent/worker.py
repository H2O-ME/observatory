"""Per-session worker: capture, input injection and file operations.

This process runs **inside the interactive user session** (spawned by the service
with ``CreateProcessAsUser``). That is the only way to see the desktop with ``mss``
and to inject input with ``SendInput``; the service itself lives in session 0 and
can do neither.

Protocol with the service (framed over the named pipe):

service -> worker   ``config``, ``cmd``, ``ctrl_start``, ``ctrl_in``, ``ctrl_stop``, ``ping``, ``stop``
worker  -> service  ``hello``, ``state``, ``result``, ``frame``, ``ctrl``, ``pong``

Binary payloads always set ``header["len"]`` so the reader knows how many bytes follow.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import __version__, capture, fs_ops, input as input_inject, logging_setup, sysinfo
from .config import ConfigStore
from .pipe_server import PipeClient

log = logging.getLogger(__name__)

STATE_INTERVAL_S = 10
MAX_PARALLEL_COMMANDS = 3
#: Base64 file chunks above this size travel as the pipe payload instead of
#: inside the JSON header, whose hard limit is 64 KiB.
RESULT_INLINE_LIMIT = 16 * 1024


class Worker:
    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.store = ConfigStore()
        self.pipe = PipeClient(session_id)
        self._stop = threading.Event()
        self._settings: dict[str, Any] = dict(self.store.config.settings or {})
        self._config_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_COMMANDS, thread_name_prefix="cmd")
        self._capture_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._control_stop = threading.Event()
        self._control_session = ""
        self._control_fps = 8
        self._control_quality = 55
        self._last_state_ms = 0

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> int:
        logging_setup.setup_logging(f"worker-{self.session_id}")
        log.info("worker starting session=%s version=%s", self.session_id, __version__)
        if not self.pipe.connect(timeout_s=90):
            log.error("could not reach the service pipe, exiting")
            return 2

        self.pipe.send(
            {
                "t": "hello",
                "session_id": self.session_id,
                "agent_version": __version__,
                "sessions": sysinfo.list_sessions(),
                "pid": _pid(),
            }
        )
        self._start_capture_loop()
        self._send_state(force=True)

        try:
            while not self._stop.is_set():
                message = self.pipe.recv()
                if message is None:
                    log.info("pipe closed, worker exiting")
                    break
                header, payload = message
                try:
                    self._dispatch(header, payload)
                except Exception:  # noqa: BLE001 - one bad command must not kill the session
                    log.exception("dispatch failed for %s", header.get("t"))
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        self._stop.set()
        self._control_stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.pipe.stop()
        log.info("worker stopped session=%s", self.session_id)

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, header: dict, payload: bytes) -> None:
        kind = header.get("t")

        if kind == "config":
            with self._config_lock:
                self._settings = dict(header.get("config") or {})
            log.info("config updated: interval=%ss enabled=%s", self._settings.get("capture_interval_s"), self._settings.get("capture_enabled"))
            capture.reset_signature()

        elif kind == "cmd":
            self._executor.submit(self._run_command, str(header.get("id")), str(header.get("type")), header.get("payload") or {})

        elif kind == "ctrl_start":
            self._start_control(str(header.get("session") or ""), int(header.get("fps") or 8), int(header.get("quality") or 55))

        elif kind == "ctrl_in":
            event = header.get("ev") or {}
            if not input_inject.handle_event(event):
                log.debug("unsupported input event: %s", event.get("kind"))

        elif kind == "ctrl_stop":
            self._stop_control()

        elif kind == "ping":
            self.pipe.send({"t": "pong", "ts": _now_ms()})

        elif kind == "stop":
            self._stop.set()

        elif kind == "uninstall":
            self._stop_control()
            self._stop.set()

        else:
            log.debug("unknown message from service: %s", kind)

    # -- commands ----------------------------------------------------------
    def _run_command(self, command_id: str, command_type: str, payload: dict) -> None:
        started = _now_ms()
        try:
            if command_type in {"screenshot", "collect_now"}:
                result = self._capture_now(force=True)
            elif command_type == "files":
                result = self._run_files(payload)
            elif command_type == "log_tail":
                lines = int(payload.get("lines") or 200)
                result = {"ok": True, "lines": logging_setup.read_tail(lines, f"worker-{self.session_id}")}
            else:
                result = {"ok": False, "error": f"worker 不处理该命令: {command_type}"}
            ok = bool(result.get("ok", True))
            error = result.get("error")
            # A file read carries up to 1 MiB of base64 in ``data``. That does not
            # fit in the 64 KiB JSON header, and the old code ignored the failed
            # send, so the command stayed ``dispatched`` forever. Move the bulky
            # field to the binary payload and keep the header small.
            payload_out = b""
            header_out = {
                "t": "result",
                "id": command_id,
                "ok": ok,
                "result": result,
                "error": error,
                "elapsed_ms": _now_ms() - started,
            }
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, str) and len(data) > RESULT_INLINE_LIMIT:
                import base64

                try:
                    payload_out = base64.b64decode(data)
                except Exception:  # noqa: BLE001 - keep the inline value on failure
                    payload_out = b""
                if payload_out:
                    slim = {key: value for key, value in result.items() if key != "data"}
                    slim["data_encoding"] = "binary"
                    header_out["result"] = slim
            if not self.pipe.send(header_out, payload_out) and payload_out:
                # The binary path failed (pipe down or oversized); fall back to
                # reporting the failure instead of leaving the command pending.
                log.warning("could not return %s bytes for %s", len(payload_out), command_id)
                self.pipe.send(
                    {
                        "t": "result",
                        "id": command_id,
                        "ok": False,
                        "result": None,
                        "error": "结果过大且管道已断开，请重试",
                        "elapsed_ms": _now_ms() - started,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed", command_type)
            self.pipe.send(
                {
                    "t": "result",
                    "id": command_id,
                    "ok": False,
                    "result": None,
                    "error": f"{exc}",
                    "elapsed_ms": _now_ms() - started,
                }
            )

    def _run_files(self, payload: dict) -> dict:
        with self._config_lock:
            allow_write = bool(self._settings.get("file_write_enabled", True))
        enriched = dict(payload)
        enriched["allow_write"] = allow_write
        try:
            return fs_ops.perform(str(payload.get("op") or ""), enriched)
        except fs_ops.FileOpError as exc:
            return {"ok": False, "op": payload.get("op"), "data": {}, "error": str(exc), "elapsed_ms": 0}

    # -- capture -----------------------------------------------------------
    def _start_capture_loop(self) -> None:
        self._capture_thread = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        self._capture_thread.start()

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            with self._config_lock:
                enabled = bool(self._settings.get("capture_enabled", True))
                interval = max(5, int(self._settings.get("capture_interval_s", 30)))
                on_change_only = bool(self._settings.get("capture_on_change_only", False))
                threshold = max(1, int(self._settings.get("capture_change_threshold", 8)))
                max_width = int(self._settings.get("max_width", 1600))
                quality = int(self._settings.get("jpeg_quality", 72))
            if not enabled or self._control_session:
                # While a live-control stream is running the periodic capture
                # pauses: two DXGI grabs per second would halve the control frame
                # rate for no benefit.
                self._stop.wait(2)
                continue
            try:
                payload, width, height, score = capture.grab(
                    max_width=max_width, quality=quality, signature=on_change_only
                )
                if not on_change_only or score >= threshold:
                    self.pipe.send(
                        {
                            "t": "frame",
                            "ts": _now_ms(),
                            "w": width,
                            "h": height,
                            "session_id": self.session_id,
                            "kind": "screen",
                            "score": score,
                            "len": len(payload),
                        },
                        payload,
                    )
            except capture.CaptureError as exc:
                log.warning("capture failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("capture loop error: %s", exc)
            self._send_state()
            self._stop.wait(interval)

    def _capture_now(self, *, force: bool = False) -> dict:
        with self._config_lock:
            max_width = int(self._settings.get("max_width", 1600))
            quality = int(self._settings.get("jpeg_quality", 72))
        payload, width, height, _score = capture.grab(max_width=max_width, quality=quality, signature=False)
        ts = _now_ms()
        sent = self.pipe.send(
            {
                "t": "frame",
                "ts": ts,
                "w": width,
                "h": height,
                "session_id": self.session_id,
                "kind": "screen",
                "score": 255,
                "len": len(payload),
            },
            payload,
        )
        return {"ok": sent, "frame_ts": ts, "width": width, "height": height, "bytes": len(payload)}

    # -- live control ------------------------------------------------------
    def _start_control(self, session_token: str, fps: int, quality: int) -> None:
        self._stop_control()
        self._control_session = session_token
        self._control_fps = max(1, min(15, fps))
        self._control_quality = max(20, min(85, quality))
        self._control_stop.clear()
        self._control_thread = threading.Thread(target=self._control_loop, name="control", daemon=True)
        self._control_thread.start()
        log.info("control stream started fps=%s quality=%s", self._control_fps, self._control_quality)

    def _stop_control(self) -> None:
        self._control_stop.set()
        self._control_session = ""
        thread = self._control_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._control_thread = None

    def _control_loop(self) -> None:
        interval = 1.0 / max(1, self._control_fps)
        with self._config_lock:
            max_width = int(self._settings.get("max_width", 1600))
        capture.reset_signature()
        while not self._control_stop.is_set() and not self._stop.is_set():
            started = time.monotonic()
            try:
                payload, width, height, _score = capture.grab(
                    max_width=max_width, quality=self._control_quality, signature=False
                )
                self.pipe.send(
                    {
                        "t": "ctrl",
                        "session": self._control_session,
                        "ts": _now_ms(),
                        "w": width,
                        "h": height,
                        "len": len(payload),
                    },
                    payload,
                )
            except capture.CaptureError as exc:
                log.debug("control capture failed: %s", exc)
                self._control_stop.wait(0.5)
            except Exception as exc:  # noqa: BLE001
                log.debug("control loop error: %s", exc)
                self._control_stop.wait(0.5)
            elapsed = time.monotonic() - started
            self._control_stop.wait(max(0.01, interval - elapsed))

    # -- state -------------------------------------------------------------
    def _send_state(self, *, force: bool = False) -> None:
        now = _now_ms()
        if not force and now - self._last_state_ms < STATE_INTERVAL_S * 1000:
            return
        self._last_state_ms = now
        self.pipe.send(
            {
                "t": "state",
                "session_id": self.session_id,
                "control_active": bool(self._control_session),
                "ts": now,
            }
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pid() -> int:
    import os

    return os.getpid()


def main(session_id: int) -> int:
    return Worker(session_id).run()


__all__ = ["Worker", "main"]