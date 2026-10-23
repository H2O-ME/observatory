"""Realtime control: short-lived grants, a relay socket and a hard TTL.

A5 is enforced in three independent places, so a lost timer or a lost DELETE
cannot leave input flowing:

1. every input frame re-checks the grant and ``now < expires_at_ms`` before it is
   translated into ``session.send("control", action="input")``;
2. a per-grant timer sends ``ctrl_stop`` and closes the browser socket at TTL;
3. ``DELETE /devices/{uid}/control/{token}`` marks the grant revoked, sends
   ``ctrl_stop`` and closes the socket immediately.

Only devices whose presence says ``capabilities.control`` may be granted a
session (A13), and only a WebSocket carrier can carry it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device
from . import audit
from .deps import client_ip, get_links, require_admin, store
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/devices",
    tags=["control"],
    route_class=ConsoleRoute,
)

DEFAULT_TTL_S = 600
MIN_TTL_S = 30
MAX_TTL_S = 3600
CONTROL_SEND_DEADLINE_MS = 5_000
CONTROL_DEADLINE_MS = 5_000
CLOSE_NO_CHANNEL = 4409
CLOSE_REVOKED = 4410


@dataclass
class Grant:
    token: str
    device_id: int
    device_uid: str
    fps: int
    quality: int
    expires_at_ms: int
    actor: str
    revoked: bool = False
    viewer: Any = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def expired(self) -> bool:
        return now_ms() >= self.expires_at_ms


class ControlSessions:
    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def grant(
        self,
        *,
        device: Device,
        fps: int,
        quality: int,
        ttl_s: int,
        actor: str,
    ) -> Grant:
        token = secrets.token_urlsafe(24)
        grant = Grant(
            token=token,
            device_id=device.id,
            device_uid=device.uid,
            fps=fps,
            quality=quality,
            expires_at_ms=now_ms() + ttl_s * 1000,
            actor=actor,
        )
        with self._lock:
            self._grants[token] = grant
        return grant

    def get(self, token: str, *, device_id: int | None = None) -> Grant | None:
        with self._lock:
            grant = self._grants.get(token)
        if grant is None:
            return None
        if grant.expired or grant.revoked:
            return None
        if device_id is not None and grant.device_id != device_id:
            return None
        return grant

    def remove(self, token: str) -> Grant | None:
        with self._lock:
            return self._grants.pop(token, None)

    def revoke_device(self, device_id: int) -> list[str]:
        with self._lock:
            tokens = [token for token, grant in self._grants.items() if grant.device_id == device_id]
        return [token for token in tokens if self.remove(token) is not None]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for grant in self._grants.values() if not grant.expired and not grant.revoked)

    def tokens_for(self, device_id: int) -> list[str]:
        with self._lock:
            return [token for token, grant in self._grants.items() if grant.device_id == device_id]


SESSIONS = ControlSessions()


async def _send_to_agent(request: Request, grant: Grant, body: dict[str, Any]) -> bool:
    agent_session = get_links(request).get(grant.device_id)
    if agent_session is None:
        return False
    try:
        await agent_session.send("control", body, deadline_ms=CONTROL_SEND_DEADLINE_MS)
        return True
    except Exception as exc:  # noqa: BLE001 - carrier errors are logged, not ignored
        log.warning("control send failed device=%s token=%s: %s", grant.device_id, grant.token, exc)
        return False


async def _close_viewer(grant: Grant, code: int, reason: str) -> None:
    viewer = grant.viewer
    grant.viewer = None
    if viewer is None:
        return
    async with grant.send_lock:
        with contextlib.suppress(Exception):
            await viewer.close(code=code, reason=reason)


async def stop_grant(request: Request, grant: Grant, *, code: int = CLOSE_REVOKED, reason: str = "revoked") -> None:
    """Send ``ctrl_stop`` and close the browser socket (A5)."""

    if grant.revoked:
        return
    grant.revoked = True
    SESSIONS.remove(grant.token)
    await _send_to_agent(request, grant, {"session": grant.token, "action": "stop"})
    await _close_viewer(grant, code, reason)
    for task in list(grant.tasks):
        if task is not asyncio.current_task():
            task.cancel()
    SESSIONS.remove(grant.token)


async def _expire_after(request: Request, grant: Grant) -> None:
    delay_s = max(0.0, (grant.expires_at_ms - now_ms()) / 1000)
    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:
        raise
    if grant.revoked:
        return
    log.info("control TTL expired device=%s token=%s", grant.device_id, grant.token)
    await stop_grant(request, grant, reason="ttl expired")


async def _load_device(session: AsyncSession, uid: str) -> Device:
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        raise AppError.gone("设备不存在")
    return device


@router.post("/{uid}/control")
async def create_control_grant(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    device = await _load_device(session, uid)
    if not payload.get("confirm"):
        raise AppError.bad_request("需要显式确认才能开始远程控制")
    try:
        ttl_s = int(payload.get("ttl_s", DEFAULT_TTL_S))
    except (TypeError, ValueError) as exc:
        raise AppError.bad_request("ttl_s 必须是整数") from exc
    if ttl_s < MIN_TTL_S or ttl_s > MAX_TTL_S:
        raise AppError.bad_request(f"ttl_s 必须在 {MIN_TTL_S}..{MAX_TTL_S} 秒之间")

    effective = await store().effective(session, device.settings or {})
    if not effective.get("control_enabled", True):
        raise AppError.unsupported("该设备的实时控制已被禁用")

    # A13: capability only, never a locally re-derived condition.
    from .devices import device_state_for

    _effective, snapshot = await device_state_for(session, device)
    if not snapshot.get("capabilities", {}).get("control"):
        raise AppError.unsupported(str(snapshot.get("reason") or "设备当前不可实时控制"))
    agent_session = get_links(request).get(device.id)
    if agent_session is None:
        raise AppError.unsupported("设备没有活动链路")

    grant = SESSIONS.grant(
        device=device,
        fps=int(effective.get("control_fps", 8)),
        quality=int(effective.get("control_quality", 55)),
        ttl_s=ttl_s,
        actor="console",
    )
    await audit.record(
        session,
        action=audit.ACTION_CONTROL_GRANT,
        device=device,
        ip=client_ip(request),
        detail={"ttl_s": ttl_s, "fps": grant.fps, "quality": grant.quality, "token": grant.token},
    )
    return stamp(
        {
            "token": grant.token,
            "expires_at_ms": grant.expires_at_ms,
            "fps": grant.fps,
            "quality": grant.quality,
            "ws_path": f"/api/v1/devices/{device.uid}/control",
        },
        deadline=CONTROL_DEADLINE_MS,
    )


@router.delete("/{uid}/control/{token}")
async def revoke_control_grant(
    uid: str,
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    device = await _load_device(session, uid)
    grant = SESSIONS.get(token, device_id=device.id)
    if grant is None:
        raise AppError.gone("控制会话不存在或已过期")
    await stop_grant(request, grant)
    await audit.record(
        session,
        action=audit.ACTION_CONTROL_GRANT,
        device=device,
        ip=client_ip(request),
        detail={"stage": "revoked", "token": token},
    )
    return stamp({"ok": True, "message": "已结束控制会话"})


@router.websocket("/{uid}/control")
async def control_socket(websocket: WebSocket, uid: str) -> None:
    runtime_links = websocket.app.state.links
    grant_token = websocket.query_params.get("token", "")
    if not grant_token:
        await websocket.close(code=4401, reason="control grant required")
        return
    await websocket.accept()

    async with websocket.app.state.db.sessionmaker() as session:
        device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        await websocket.close(code=4404, reason="device not found")
        return

    grant = SESSIONS.get(grant_token, device_id=device.id)
    if grant is None:
        await websocket.close(code=4401, reason="invalid grant")
        return
    agent_session = runtime_links.get(device.id)
    if agent_session is None:
        await websocket.close(code=CLOSE_NO_CHANNEL, reason="no realtime channel")
        return
    open_control = getattr(agent_session, "open_control", None)
    if not callable(open_control):
        await websocket.close(code=CLOSE_NO_CHANNEL, reason="agent cannot stream control")
        return
    stream = open_control(grant.token)
    if stream is None:
        await websocket.close(code=CLOSE_NO_CHANNEL, reason="agent cannot stream control")
        return

    grant.viewer = websocket
    await websocket.send_json(
        {
            "type": "ready",
            "fps": grant.fps,
            "quality": grant.quality,
            "expires_at_ms": grant.expires_at_ms,
            "server_time_ms": now_ms(),
        }
    )
    sent = await _send_to_agent(
        websocket,
        grant,
        {
            "session": grant.token,
            "action": "start",
            "fps": grant.fps,
            "quality": grant.quality,
            "ttl_s": max(1, (grant.expires_at_ms - now_ms()) // 1000),
        },
    )
    if not sent:
        await stop_grant(websocket, grant, code=CLOSE_NO_CHANNEL, reason="agent unreachable")
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_NO_CHANNEL, reason="agent unreachable")
        return

    async def pump_frames() -> None:
        queue = getattr(stream, "queue", None)
        if queue is None:
            return
        while not grant.revoked:
            frame = await queue.get()
            async with grant.send_lock:
                if grant.viewer is not websocket:
                    return
                await websocket.send_bytes(frame)

    async def pump_input() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            # A5: the grant is re-checked for every single input event.
            live = SESSIONS.get(grant.token, device_id=device.id)
            if live is None or live.expires_at_ms <= now_ms():
                await stop_grant(websocket, grant, reason="grant expired")
                return
            text = message.get("text")
            if text:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                kind = parsed.get("type")
                if kind == "input" and isinstance(parsed.get("ev"), dict):
                    await agent_session.send(
                        "control",
                        {"session": grant.token, "action": "input", "ev": parsed["ev"]},
                        deadline_ms=CONTROL_SEND_DEADLINE_MS,
                    )
                elif kind == "ping":
                    async with grant.send_lock:
                        await websocket.send_json({"type": "pong", "server_time_ms": now_ms()})
            elif message.get("bytes"):
                # Old browsers forwarded raw binary control frames; the legacy
                # bridge must pass them through unchanged, the v3 session carries
                # them as a base64 control envelope.
                forward_binary = getattr(agent_session, "forward_binary", None)
                if callable(forward_binary):
                    await forward_binary(message["bytes"])
                else:
                    await agent_session.send(
                        "control",
                        {
                            "session": grant.token,
                            "action": "input",
                            "data_b64": base64.b64encode(message["bytes"]).decode("ascii"),
                        },
                        deadline_ms=CONTROL_SEND_DEADLINE_MS,
                    )

    timer = asyncio.create_task(_expire_after(websocket, grant), name=f"control-ttl-{grant.token}")
    grant.tasks.add(timer)
    frames_task = asyncio.create_task(pump_frames(), name=f"control-frames-{grant.token}")
    grant.tasks.add(frames_task)
    try:
        await pump_input()
    except WebSocketDisconnect:
        pass
    except AppError as exc:
        log.info("control closed: %s", exc.message)
    finally:
        for task in (frames_task, timer):
            task.cancel()
        close_control = getattr(agent_session, "close_control", None)
        if callable(close_control):
            close_control(grant.token)
        else:
            log.warning("session has no close_control(); legacy stream may leak token=%s", grant.token)
        grant.viewer = None
        if not grant.revoked:
            await _send_to_agent(websocket, grant, {"session": grant.token, "action": "stop"})
        grant.revoked = True
        SESSIONS.remove(grant.token)
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_REVOKED, reason="control closed")


__all__ = [
    "CLOSE_NO_CHANNEL",
    "CLOSE_REVOKED",
    "ControlSessions",
    "Grant",
    "SESSIONS",
    "router",
    "stop_grant",
]