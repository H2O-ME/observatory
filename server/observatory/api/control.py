"""Realtime remote control: grant + relay socket.

Flow:

1. ``POST /devices/{uid}/control`` mints a short-lived grant (audited).
2. The browser opens ``WS /devices/{uid}/control?token=<grant>``.
3. The server asks the agent (over its WebSocket) to start a control stream at the
   configured fps/quality.
4. Input events travel browser -> server -> agent (``ctrl_in``); frames travel
   agent -> server -> browser as raw WebP bytes.

Control is only possible on the WebSocket transport. If a device is on the polling
fallback the grant request is refused with a reason the UI shows verbatim, instead
of opening a black rectangle and pretending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_admin
from ..models import Device
from ..schemas import ControlGrantIn, ControlGrantOut, OkOut
from ..services import audit
from ..services.presence import STATE_ONLINE, snapshot
from ..services.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["control"])

CLOSE_NO_CHANNEL = 4409


async def _device(session: AsyncSession, uid: str) -> Device:
    result = await session.execute(select(Device).where(Device.uid == uid))
    device = result.scalars().first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device


@router.post("/{uid}/control", response_model=ControlGrantOut, dependencies=[Depends(require_admin)])
async def create_grant(
    uid: str,
    payload: ControlGrantIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> ControlGrantOut:
    device = await _device(session, uid)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要显式确认才能开始远程控制")

    effective = await runtime.store.effective(session, device.settings or {})
    if not effective.get("control_enabled", True):
        raise HTTPException(status_code=403, detail="该设备的实时控制已被禁用")

    presence = snapshot(device, runtime.registry)
    if presence["state"] != STATE_ONLINE:
        raise HTTPException(
            status_code=409,
            detail=f"实时控制需要 WebSocket 通道：{presence['reason']}",
        )

    grant = runtime.control.grant(
        device=device,
        fps=int(effective.get("control_fps", 8)),
        quality=int(effective.get("control_quality", 55)),
        ttl_s=payload.ttl_s,
        actor="console",
    )
    await audit.record(
        session,
        action=audit.ACTION_CONTROL_GRANT,
        device=device,
        ip=client_ip(request),
        detail={"ttl_s": payload.ttl_s, "fps": grant.fps},
    )
    return ControlGrantOut(
        token=grant.token,
        expires_at_ms=grant.expires_at_ms,
        fps=grant.fps,
        quality=grant.quality,
        ws_path=f"/api/v1/devices/{device.uid}/control",
        server_time_ms=int(time.time() * 1000),
    )


@router.delete("/{uid}/control/{token}", response_model=OkOut, dependencies=[Depends(require_admin)])
async def revoke_grant(uid: str, token: str) -> OkOut:
    runtime = get_runtime()
    runtime.control.revoke(token)
    return OkOut(message="已结束控制会话")


@router.websocket("/{uid}/control")
async def control_socket(websocket: WebSocket, uid: str) -> None:
    runtime: Runtime = websocket.app.state.runtime
    token = websocket.query_params.get("token", "")
    await websocket.accept()

    async with runtime.db.sessionmaker() as session:
        result = await session.execute(select(Device).where(Device.uid == uid))
        device = result.scalars().first()
    if device is None:
        await websocket.close(code=4404, reason="device not found")
        return

    grant = runtime.control.verify(token, device_id=device.id)
    if grant is None:
        await websocket.close(code=4401, reason="invalid grant")
        return
    if not runtime.agent_hub.is_live(device.id):
        await websocket.close(code=CLOSE_NO_CHANNEL, reason="no realtime channel")
        return

    queue = runtime.control.attach_viewer(token)
    if queue is None:
        await websocket.close(code=4401, reason="grant gone")
        return

    started = await runtime.agent_hub.send_json(
        device.id,
        {
            "t": "ctrl_start",
            "session": token,
            "fps": grant.fps,
            "quality": grant.quality,
            "ttl_s": max(30, (grant.expires_at_ms - int(time.time() * 1000)) // 1000),
        },
    )
    if not started:
        runtime.control.detach_viewer(token)
        await websocket.close(code=CLOSE_NO_CHANNEL, reason="agent unreachable")
        return

    await websocket.send_json(
        {"type": "ready", "fps": grant.fps, "quality": grant.quality, "expires_at_ms": grant.expires_at_ms}
    )
    log.info("control session started uid=%s", uid)

    async def pump_frames() -> None:
        while True:
            frame = await queue.get()
            await websocket.send_bytes(frame)

    async def pump_input() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            blob = message.get("bytes")
            if text:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if parsed.get("type") == "input" and isinstance(parsed.get("ev"), dict):
                    await runtime.agent_hub.send_json(
                        device.id, {"t": "ctrl_in", "session": token, "ev": parsed["ev"]}
                    )
                elif parsed.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            elif blob:
                await runtime.agent_hub.send_bytes(device.id, blob)

    tasks = [asyncio.create_task(pump_frames()), asyncio.create_task(pump_input())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None:
                log.debug("control pump ended: %s", exc)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("control session error uid=%s: %s", uid, exc)
    finally:
        for task in tasks:
            task.cancel()
        runtime.control.detach_viewer(token)
        await runtime.agent_hub.send_json(device.id, {"t": "ctrl_stop", "session": token})
        async with runtime.db.sessionmaker() as session:
            await audit.record(
                session,
                action=audit.ACTION_CONTROL_GRANT,
                device=device,
                detail={"stage": "stop"},
            )
        log.info("control session stopped uid=%s", uid)


__all__ = ["CLOSE_NO_CHANNEL", "router"]