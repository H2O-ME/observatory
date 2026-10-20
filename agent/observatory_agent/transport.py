"""Transport ladder: WebSocket -> HTTPS long-poll -> HTTPS short-poll.

Requirement: use the best protocol available and degrade step by step. The agent
starts at the top, remembers the best level that has worked, steps down when a
channel fails, and periodically probes upward again with exponential backoff so a
transient outage does not pin a machine to polling forever.

Only the WebSocket carries live control. When the agent is on a polling level the
server refuses control grants with an explicit reason instead of showing a black
rectangle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .client import DEVICE_TOKEN_HEADER, AgentClient, TransportError

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]

LEVEL_WS = 0
LEVEL_POLL_LONG = 1
LEVEL_POLL_SHORT = 2
LEVEL_NAMES = {LEVEL_WS: "ws", LEVEL_POLL_LONG: "long-poll", LEVEL_POLL_SHORT: "short-poll"}

WS_OPEN_TIMEOUT = 12
WS_MAX_MESSAGE = 16 * 1024 * 1024
PROBE_BACKOFF_MAX_S = 600


def _ws_connect(**kwargs):
    """Import the websockets connect function across 12/13/14 API shapes."""

    import websockets

    try:
        from websockets.asyncio.client import connect  # websockets >= 13

        return connect, "additional_headers"
    except Exception:  # noqa: BLE001 - older releases
        return websockets.connect, "extra_headers"


class Channel:
    kind = "none"
    level = 99

    def __init__(self) -> None:
        #: Set once the channel has genuinely carried a message; the runner uses
        #: it to distinguish "connection refused" from "connected then dropped".
        self.ready = asyncio.Event()

    async def run(self, handler: Handler, heartbeat_s: int) -> None:
        raise NotImplementedError

    async def send_json(self, payload: dict) -> bool:
        return False

    async def send_bytes(self, data: bytes) -> bool:
        return False

    async def close(self) -> None:
        return None


class WsChannel(Channel):
    kind = "ws"
    level = LEVEL_WS

    def __init__(self, client: AgentClient, payload_provider: Callable[[], dict]) -> None:
        super().__init__()
        self.client = client
        self.payload_provider = payload_provider
        self._ws: Any = None
        self._write_lock = asyncio.Lock()

    def _connect_kwargs(self, heartbeat_s: int) -> tuple[Any, dict[str, Any]]:
        connect, header_key = _ws_connect()
        kwargs: dict[str, Any] = {
            "ping_interval": max(10, heartbeat_s),
            "ping_timeout": max(10, heartbeat_s),
            "max_size": WS_MAX_MESSAGE,
            "open_timeout": WS_OPEN_TIMEOUT,
            header_key: {DEVICE_TOKEN_HEADER: self.client.token},
        }
        return connect, kwargs

    async def probe(self) -> None:
        """Cheap reachability test used by the upgrade probe."""

        connect, kwargs = self._connect_kwargs(20)
        async with connect(self.client.ws_url(), **kwargs) as socket:
            await socket.close()

    async def run(self, handler: Handler, heartbeat_s: int) -> None:
        connect, kwargs = self._connect_kwargs(heartbeat_s)
        url = self.client.ws_url()
        try:
            async with connect(url, **kwargs) as socket:
                self._ws = socket
                self.ready.set()
                log.info("websocket connected: %s", url)
                heartbeat = asyncio.create_task(self._heartbeat_loop(heartbeat_s))
                try:
                    while True:
                        raw = await socket.recv()
                        if isinstance(raw, (bytes, bytearray)):
                            await handler({"t": "_binary", "data": bytes(raw)})
                            continue
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(message, dict):
                            await handler(message)
                finally:
                    heartbeat.cancel()
                    self._ws = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any socket error means "step down"
            raise TransportError(f"websocket 中断: {exc}") from exc

    async def _heartbeat_loop(self, heartbeat_s: int) -> None:
        while True:
            await asyncio.sleep(max(5, heartbeat_s))
            if not await self.send_json({"t": "heartbeat", **self.payload_provider()}):
                raise TransportError("心跳发送失败")

    async def send_json(self, payload: dict) -> bool:
        socket = self._ws
        if socket is None:
            return False
        async with self._write_lock:
            try:
                await socket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                return True
            except Exception as exc:  # noqa: BLE001
                log.debug("ws send failed: %s", exc)
                return False

    async def send_bytes(self, data: bytes) -> bool:
        socket = self._ws
        if socket is None:
            return False
        async with self._write_lock:
            try:
                await socket.send(data)
                return True
            except Exception:  # noqa: BLE001
                return False

    async def close(self) -> None:
        socket = self._ws
        self._ws = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001
                pass


class PollChannel(Channel):
    """HTTPS polling fallback.

    ``long`` uses the server's 25s hold window; ``short`` polls every few seconds,
    which is what a restrictive proxy that kills idle connections leaves us with.
    """

    kind = "poll"

    def __init__(
        self,
        client: AgentClient,
        payload_provider: Callable[[], dict],
        *,
        level: int = LEVEL_POLL_LONG,
    ) -> None:
        super().__init__()
        self.client = client
        self.payload_provider = payload_provider
        self.level = level
        self.hold = 20 if level == LEVEL_POLL_LONG else 0
        self.interval = 5

    async def run(self, handler: Handler, heartbeat_s: int) -> None:
        last_heartbeat = 0.0
        first = True
        while True:
            now = time.monotonic()
            if first or now - last_heartbeat >= heartbeat_s:
                first = False
                last_heartbeat = now
                try:
                    payload = await self.client.heartbeat(self.payload_provider())
                except PermissionError as exc:
                    raise TransportError(str(exc)) from exc
                self.ready.set()
                await handler({"t": "pong", **payload})
                for command in payload.get("commands") or []:
                    await handler({"t": "command", "command": command})
                continue

            wait_s = self.hold
            if self.level == LEVEL_POLL_LONG:
                wait_s = int(max(2, min(self.hold, heartbeat_s - (now - last_heartbeat))))
            try:
                payload = await self.client.poll_commands(wait_s)
            except PermissionError as exc:
                raise TransportError(str(exc)) from exc
            await handler(
                {
                    "t": "pong",
                    "config": payload.get("config"),
                    "update": payload.get("update"),
                }
            )
            commands = payload.get("commands") or []
            for command in commands:
                await handler({"t": "command", "command": command})
            if not commands and self.level == LEVEL_POLL_SHORT:
                await asyncio.sleep(self.interval)


def build_channel(
    level: int,
    client: AgentClient,
    payload_provider: Callable[[], dict],
) -> Channel:
    if level == LEVEL_WS:
        return WsChannel(client, payload_provider)
    return PollChannel(client, payload_provider, level=LEVEL_POLL_LONG if level == LEVEL_POLL_LONG else LEVEL_POLL_SHORT)


def level_name(level: int) -> str:
    return LEVEL_NAMES.get(level, "unknown")


__all__ = [
    "Channel",
    "LEVEL_NAMES",
    "LEVEL_POLL_LONG",
    "LEVEL_POLL_SHORT",
    "LEVEL_WS",
    "PollChannel",
    "WsChannel",
    "build_channel",
    "level_name",
]