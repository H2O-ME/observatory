"""Fan-out hubs: one for agent WebSockets, one for console WebSockets.

Kept deliberately thin. The agent hub is what makes the realtime path work: a
command created in an API handler is pushed to the device immediately instead of
waiting for its next poll, and the same object is what the control relay uses to
forward input events.

Sends are serialised per connection with a lock: uvicorn raises if two coroutines
write to the same WebSocket concurrently, and the control relay genuinely is
concurrent with command dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

log = logging.getLogger(__name__)


class AgentConnection:
    def __init__(self, *, device_id: int, uid: str, websocket: Any, remote: str = "") -> None:
        self.device_id = device_id
        self.uid = uid
        self.websocket = websocket
        self.remote = remote
        self.send_lock = asyncio.Lock()
        self.closing = False

    async def send_json(self, payload: dict[str, Any]) -> bool:
        if self.closing:
            return False
        async with self.send_lock:
            try:
                await self.websocket.send_json(payload)
                return True
            except Exception as exc:  # noqa: BLE001 - transport failures are expected
                log.debug("agent send failed uid=%s: %s", self.uid, exc)
                self.closing = True
                return False

    async def send_bytes(self, data: bytes) -> bool:
        if self.closing:
            return False
        async with self.send_lock:
            try:
                await self.websocket.send_bytes(data)
                return True
            except Exception as exc:  # noqa: BLE001
                log.debug("agent binary send failed uid=%s: %s", self.uid, exc)
                self.closing = True
                return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closing = True
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason)


class AgentHub:
    def __init__(self) -> None:
        self._connections: dict[int, AgentConnection] = {}
        # Wired by Runtime so a command pushed to an offline device still wakes
        # its pending long-poll instead of waiting for the next interval.
        self.notifier: Any = None

    def notify_command(self, device_id: int) -> None:
        if self.notifier is not None:
            self.notifier.notify(device_id)

    async def register(self, connection: AgentConnection) -> None:
        previous = self._connections.get(connection.device_id)
        self._connections[connection.device_id] = connection
        if previous is not None and previous is not connection:
            # A reconnecting agent replaces the stale socket; the old one is
            # closed with a normal code so the agent does not treat it as failover.
            log.info("agent reconnected uid=%s, closing previous socket", connection.uid)
            await previous.close(code=4000, reason="replaced by newer connection")

    async def unregister(self, connection: AgentConnection) -> None:
        current = self._connections.get(connection.device_id)
        if current is connection:
            self._connections.pop(connection.device_id, None)

    def get(self, device_id: int) -> AgentConnection | None:
        connection = self._connections.get(device_id)
        if connection is not None and connection.closing:
            self._connections.pop(device_id, None)
            return None
        return connection

    def is_live(self, device_id: int) -> bool:
        return self.get(device_id) is not None

    async def send_json(self, device_id: int, payload: dict[str, Any]) -> bool:
        connection = self.get(device_id)
        if connection is None:
            return False
        return await connection.send_json(payload)

    async def send_bytes(self, device_id: int, data: bytes) -> bool:
        connection = self.get(device_id)
        if connection is None:
            return False
        return await connection.send_bytes(data)

    async def close_device(self, device_id: int, code: int = 4001, reason: str = "") -> None:
        connection = self._connections.pop(device_id, None)
        if connection is not None:
            await connection.close(code=code, reason=reason)

    async def broadcast_json(self, payload: dict[str, Any], *, skip: set[int] | None = None) -> int:
        sent = 0
        skip = skip or set()
        for device_id, connection in list(self._connections.items()):
            if device_id in skip:
                continue
            if await connection.send_json(payload):
                sent += 1
        return sent

    def online_devices(self) -> list[int]:
        return [device_id for device_id in self._connections if self.get(device_id) is not None]

    def clear(self) -> None:
        self._connections.clear()


class ConsoleHub:
    """Broadcast channel for the console.

    Events are best-effort: a page that was asleep re-reads REST state on wake,
    so a dropped event is a latency problem, never a correctness problem.
    """

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def register(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.add(websocket)

    async def unregister(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict[str, Any]) -> int:
        targets = list(self._clients)
        if not targets:
            return 0
        sent = 0
        dead: list[Any] = []
        for websocket in targets:
            try:
                await websocket.send_json(event)
                sent += 1
            except Exception:  # noqa: BLE001
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self._clients.discard(websocket)
        return sent

    def clear(self) -> None:
        self._clients.clear()


__all__ = ["AgentConnection", "AgentHub", "ConsoleHub"]