"""Named-pipe transport between the SYSTEM service and per-session workers.

Why a pipe at all: screen capture and input injection only see a desktop when they
run *inside that user's session*. The service runs in session 0 and owns the
network connection, so it spawns one worker per interactive session and talks to it
over a named pipe.

The reader polls with ``PeekNamedPipe`` and only issues ``ReadFile`` once the bytes
are known to be there. A plain blocking read on a synchronous handle deadlocks when
the peer is killed mid-write — which is exactly what happens during logoff, and
what used to leave orphaned workers behind.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .config import PIPE_PREFIX
from .pipe_proto import FramingError, HEADER_STRUCT, decode, encode

log = logging.getLogger(__name__)

BUFFER_SIZE = 1024 * 1024
POLL_INTERVAL = 0.02
CONNECT_TIMEOUT_S = 30.0


def pipe_name(session_id: int) -> str:
    return f"{PIPE_PREFIX}-{session_id}"


class PipeServer:
    """Service side: accepts one worker per session and exchanges messages."""

    def __init__(self, session_id: int, on_message: Callable[[dict, bytes], None]) -> None:
        self.session_id = session_id
        self.on_message = on_message
        self._handle: Any = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.connected = threading.Event()
        self._connected_once = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"pipe-srv-{self.session_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            import win32file

            if self._handle is not None:
                win32file.CloseHandle(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None
        if self._thread is not None:
            self._thread.join(timeout=3)

    def send(self, header: dict, payload: bytes = b"") -> bool:
        with self._write_lock:
            handle = self._handle
            if handle is None or not self.connected.is_set():
                return False
            try:
                import win32file

                win32file.WriteFile(handle, encode(header, payload))
                return True
            except Exception as exc:  # noqa: BLE001
                log.debug("pipe write failed session=%s: %s", self.session_id, exc)
                self.connected.clear()
                return False

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        try:
            import pywintypes
            import win32file
            import win32pipe
        except Exception as exc:  # noqa: BLE001
            log.error("pywin32 unavailable, cannot create pipe: %s", exc)
            return

        while not self._stop.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    pipe_name(self.session_id),
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    1,
                    BUFFER_SIZE,
                    BUFFER_SIZE,
                    0,
                    None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("CreateNamedPipe failed session=%s: %s", self.session_id, exc)
                time.sleep(2)
                continue

            self._handle = handle
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                # ERROR_PIPE_CONNECTED means a client beat us to it; anything else
                # means the worker never showed up.
                if getattr(exc, "winerror", None) not in (535, 0):
                    log.debug("ConnectNamedPipe session=%s: %s", self.session_id, exc)
            if self._stop.is_set():
                break

            self.connected.set()
            self._connected_once = True
            log.info("worker pipe connected session=%s", self.session_id)
            try:
                self._read_loop(handle)
            except Exception as exc:  # noqa: BLE001
                log.debug("pipe read loop ended session=%s: %s", self.session_id, exc)
            self.connected.clear()
            try:
                win32file.DisconnectNamedPipe(handle)
                win32file.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def _read_loop(self, handle) -> None:
        import win32file

        while not self._stop.is_set():
            try:
                _flags, available, _left = win32file.PeekNamedPipe(handle, 0)
            except Exception:
                return
            if available < HEADER_STRUCT.size:
                time.sleep(POLL_INTERVAL)
                continue

            header_size = _read_bytes(handle, HEADER_STRUCT.size)
            if header_size is None:
                return
            import struct

            (length,) = struct.unpack(">I", header_size)
            if length <= 0 or length > 64 * 1024:
                log.warning("bad pipe header length %s", length)
                return
            rest = _read_bytes(handle, length)
            if rest is None:
                return
            import json

            try:
                header = json.loads(rest.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(header, dict):
                continue
            payload = b""
            payload_size = int(header.get("len") or 0)
            if payload_size:
                data = _read_bytes(handle, payload_size)
                if data is None:
                    return
                payload = data
            try:
                self.on_message(header, payload)
            except Exception:  # noqa: BLE001
                log.exception("pipe message handler failed session=%s", self.session_id)


def _read_bytes(handle, size: int) -> bytes | None:
    import win32file

    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            _flags, available, _left = win32file.PeekNamedPipe(handle, 0)
        except Exception:
            return None
        if available <= 0:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            _err, chunk = win32file.ReadFile(handle, min(remaining, available))
        except Exception:
            return None
        if not chunk:
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class PipeClient:
    """Worker side: connects back to the service and exchanges messages."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._handle: Any = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    def connect(self, timeout_s: float = CONNECT_TIMEOUT_S) -> bool:
        try:
            import pywintypes
            import win32file
        except Exception as exc:  # noqa: BLE001
            log.error("pywin32 unavailable: %s", exc)
            return False

        deadline = time.time() + timeout_s
        while time.time() < deadline and not self._stop.is_set():
            try:
                handle = win32file.CreateFile(
                    pipe_name(self.session_id),
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                self._handle = handle
                log.info("connected to service pipe session=%s", self.session_id)
                return True
            except pywintypes.error:
                time.sleep(0.5)
        return False

    def stop(self) -> None:
        self._stop.set()
        try:
            import win32file

            if self._handle is not None:
                win32file.CloseHandle(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None

    def send(self, header: dict, payload: bytes = b"") -> bool:
        with self._write_lock:
            handle = self._handle
            if handle is None:
                return False
            try:
                import win32file

                win32file.WriteFile(handle, encode(header, payload))
                return True
            except Exception as exc:  # noqa: BLE001
                log.debug("pipe client write failed: %s", exc)
                return False

    def recv(self) -> tuple[dict, bytes] | None:
        """Blocking-ish receive used by the worker's dispatch loop."""

        import struct

        handle = self._handle
        if handle is None:
            return None
        header_size = _read_bytes(handle, HEADER_STRUCT.size)
        if header_size is None:
            return None
        (length,) = struct.unpack(">I", header_size)
        if length <= 0 or length > 64 * 1024:
            return None
        raw = _read_bytes(handle, length)
        if raw is None:
            return None
        import json

        try:
            header = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        payload = b""
        payload_size = int(header.get("len") or 0)
        if payload_size:
            data = _read_bytes(handle, payload_size)
            if data is None:
                return None
            payload = data
        if not isinstance(header, dict):
            return None
        return header, payload


__all__ = ["BUFFER_SIZE", "PipeClient", "PipeServer", "pipe_name"]
_ = (FramingError, decode, encode)