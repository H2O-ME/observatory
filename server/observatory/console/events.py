"""Console event socket and the process-local fan-out hub.

Events are best-effort: a browser that was asleep re-reads REST state on wake.
Two rules still matter:

* the socket carries ``server_time_ms`` in every frame so the console can
  calibrate its clock without consulting ``Date.now()`` (A12);
* a slow client must never stall the request that emits an event, so each send
  has a timeout and failures are logged (not swallowed).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import decode_session
from .deps import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

CLOSE_UNAUTHORIZED = 4401
SEND_TIMEOUT_S = 2.0

EVENT_DEVICE_UPDATED = "device_updated"
EVENT_DEVICE_REMOVED = "device_removed"
EVENT_COMMAND_CREATED = "command_created"
EVENT_COMMAND_UPDATED = "command_updated"
EVENT_FRAME_ADDED = "frame_added"
EVENT_UNINSTALL_PROGRESS = "uninstall_progress"
EVENT_CONTROL_FRAME = "control_frame"
EVENT_VERSION = "version"
EVENT_NOTICE = "notice"


def build(event_type: str, *, device_uid: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": event_type,
        "ts_ms": now_ms(),
        "server_time_ms": now_ms(),
        "device_uid": device_uid,
        "data": data or {},
    }


class EventHub:
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

    async def send(self, websocket: Any, event: dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(websocket.send_json(event), timeout=SEND_TIMEOUT_S)
            return True
        except Exception as exc:  # noqa: BLE001 - a dead browser is a normal event
            log.debug("console event send failed: %s", exc)
            return False

    async def broadcast(self, event: dict[str, Any]) -> int:
        targets = list(self._clients)
        if not targets:
            return 0
        sent = 0
        dead: list[Any] = []
        for websocket in targets:
            if await self.send(websocket, event):
                sent += 1
            else:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self._clients.discard(websocket)
        return sent

    def clear(self) -> None:
        self._clients.clear()


HUB = EventHub()


async def emit(event_type: str, *, device_uid: str = "", data: dict[str, Any] | None = None) -> int:
    return await HUB.broadcast(build(event_type, device_uid=device_uid, data=data))


@router.websocket("/ws")
async def console_socket(websocket: WebSocket) -> None:
    settings = get_settings(websocket)
    token = (
        websocket.query_params.get("token", "")
        or websocket.cookies.get("obs_session", "")
        or _bearer(websocket)
    )
    payload = decode_session(token, secret_key=settings.secret_key)
    if payload is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    await websocket.accept()
    await HUB.register(websocket)
    await websocket.send_json(
        build(
            "hello",
            data={
                "version": resolve_version(),
                "heartbeat_s": settings.heartbeat_interval_s,
                "channels": get_links_counts(websocket),
            },
        )
    )
    heartbeat = max(5, int(settings.heartbeat_interval_s))
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=heartbeat)
            except asyncio.TimeoutError:
                if not await HUB.send(
                    websocket,
                    build(
                        "tick",
                        data={
                            "server_time_ms": now_ms(),
                            "channels": get_links_counts(websocket),
                            "clients": HUB.client_count,
                        },
                    ),
                ):
                    break
                continue
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text and '"ping"' in text:
                with contextlib.suppress(Exception):
                    await websocket.send_json(build("pong"))
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.unregister(websocket)
        log.info("console ws disconnected (%d clients)", HUB.client_count)


def get_links_counts(websocket: WebSocket) -> dict[str, int]:
    from ..link.registry import registry as links

    return links.counts()


def _bearer(websocket: WebSocket) -> str:
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


__all__ = [
    "EVENT_COMMAND_CREATED",
    "EVENT_CONTROL_FRAME",
    "EVENT_DEVICE_REMOVED",
    "EVENT_DEVICE_UPDATED",
    "EVENT_FRAME_ADDED",
    "EVENT_NOTICE",
    "EVENT_UNINSTALL_PROGRESS",
    "EVENT_VERSION",
    "HUB",
    "build",
    "emit",
    "router",
]