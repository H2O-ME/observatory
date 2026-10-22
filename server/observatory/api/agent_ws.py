"""Agent WebSocket: the preferred transport.

Everything the device needs after enrollment rides this single socket: heartbeat,
command dispatch, command results, file-operation results, frame upload and the
live-control relay. When it cannot connect the agent falls back to HTTPS long
polling; the server side of that fallback is ``api/agent.py``.

Frame bytes arrive as binary messages using the shared framing envelope:

    uint32 header_len | JSON header | payload

``header["t"] == "frame"``  -> a captured screen, persisted like an HTTP upload
``header["t"] == "ctrl"``   -> a live-control frame, relayed to the watching browser
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..deps import client_ip, ws_device_token
from ..framing import FramingError, decode
from ..models import Device, now_ms
from ..security import device_token_hash
from ..services import audit, commands as command_service, events, ingest
from ..services.hub import AgentConnection
from ..services.packages import can_self_update
from ..services.runtime import Runtime
from ..version import is_newer

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent-ws"])

CLOSE_UNAUTHORIZED = 4401
CLOSE_GONE = 4002
CLOSE_IDLE = 4408


def _runtime(websocket: WebSocket) -> Runtime:
    return websocket.app.state.runtime


async def _load_device(runtime: Runtime, token: str) -> Device | None:
    if not token:
        return None
    digest = device_token_hash(token, runtime.settings.secret_key)
    async with runtime.db.sessionmaker() as session:
        result = await session.execute(select(Device).where(Device.token_hash == digest))
        return result.scalars().first()


@router.websocket("/ws")
async def agent_socket(websocket: WebSocket) -> None:
    runtime = _runtime(websocket)
    token = ws_device_token(websocket)
    device = await _load_device(runtime, token)
    if device is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid device token")
        return
    if device.uninstalled_at_ms:
        await websocket.accept()
        await websocket.close(code=CLOSE_GONE, reason="device uninstalled")
        return

    await websocket.accept()
    remote = client_ip(websocket) if hasattr(websocket, "client") else ""
    connection = AgentConnection(device_id=device.id, uid=device.uid, websocket=websocket, remote=remote)
    await runtime.agent_hub.register(connection)
    runtime.registry.attach(device.id, "ws", remote)

    effective = {}
    async with runtime.db.sessionmaker() as session:
        device_row = await session.get(Device, device.id)
        if device_row is not None:
            device_row.last_seen_ms = now_ms()
            await session.commit()
            effective = await runtime.store.effective(session, device_row.settings or {})
        update = None
        if runtime.packages is not None:
            package = await runtime.packages.latest(session, "agent")
            if (
                package is not None
                and can_self_update(device.agent_version)
                and is_newer(package.version, device.agent_version or "0.0.0")
            ):
                update = runtime.packages.update_payload(package)

    await connection.send_json(
        {
            "t": "welcome",
            "device_uid": device.uid,
            "server_version": runtime.version,
            "server_time_ms": now_ms(),
            "heartbeat_interval_s": int(effective.get("heartbeat_interval_s", 20)),
            "config": effective,
            "update": update,
            "poll_hold_s": runtime.settings.poll_hold_s,
        }
    )
    await events.emit(
        runtime.console_hub,
        events.EVENT_DEVICE_UPDATED,
        device_uid=device.uid,
        data={"transport": "ws"},
    )
    log.info("agent ws connected uid=%s remote=%s", device.uid, remote)

    idle = runtime.settings.ws_idle_timeout_s
    ping_sent = False
    last_ping = 0.0

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=idle)
            except asyncio.TimeoutError:
                if ping_sent:
                    log.info("agent ws idle timeout uid=%s", device.uid)
                    break
                ping_sent = True
                last_ping = time.monotonic()
                if not await connection.send_json({"t": "ping", "ts": now_ms()}):
                    break
                continue

            if message.get("type") == "websocket.disconnect":
                break

            ping_sent = False
            text = message.get("text")
            blob = message.get("bytes")
            if text is not None:
                await _handle_text(runtime, device, connection, text)
            elif blob is not None:
                await _handle_binary(runtime, device, connection, blob)
            runtime.registry.touch(device.id)

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - never let one bad message kill the loop silently
        log.exception("agent ws error uid=%s: %s", device.uid, exc)
    finally:
        runtime.registry.detach(device.id, "ws")
        await runtime.agent_hub.unregister(connection)
        async with runtime.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is not None:
                row.last_seen_ms = now_ms()
                await session.commit()
        await events.emit(runtime.console_hub, events.EVENT_DEVICE_UPDATED, device_uid=device.uid)
        log.info("agent ws disconnected uid=%s", device.uid)
        _ = last_ping


async def _handle_text(runtime: Runtime, device: Device, connection: AgentConnection, text: str) -> None:
    import json

    try:
        message = json.loads(text)
    except ValueError:
        log.warning("agent %s sent invalid JSON", device.uid)
        return
    kind = message.get("t")

    if kind == "hello":
        async with runtime.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is None:
                return
            row.agent_version = str(message.get("agent_version", row.agent_version))[:30]
            row.last_seen_ms = now_ms()
            if isinstance(message.get("sysinfo"), dict):
                row.sysinfo = message["sysinfo"]
            await session.commit()
            config = await runtime.store.effective(session, row.settings or {})
        await connection.send_json({"t": "config", "config": config, "server_time_ms": now_ms()})

    elif kind == "heartbeat":
        async with runtime.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is None:
                return
            row.last_seen_ms = now_ms()
            if message.get("agent_version"):
                row.agent_version = str(message["agent_version"])[:30]
            if isinstance(message.get("sysinfo"), dict):
                row.sysinfo = message["sysinfo"]
            await session.commit()
            config = await runtime.store.effective(session, row.settings or {})
            update = None
            if runtime.packages is not None and config.get("auto_update", True):
                package = await runtime.packages.latest(session, "agent")
                if (
                    package is not None
                    and can_self_update(row.agent_version)
                    and is_newer(package.version, row.agent_version or "0.0.0")
                ):
                    update = runtime.packages.update_payload(package)
        await connection.send_json(
            {
                "t": "pong",
                "server_time_ms": now_ms(),
                "config": config,
                "update": update,
                "heartbeat_interval_s": int(config.get("heartbeat_interval_s", 20)),
            }
        )

    elif kind == "result":
        command_id = str(message.get("command_id") or "")
        if not command_id:
            return
        async with runtime.db.sessionmaker() as session:
            await command_service.record_result(
                session,
                command_id=command_id,
                ok=bool(message.get("ok")),
                result=message.get("result") if isinstance(message.get("result"), dict) else None,
                error=str(message["error"])[:2000] if message.get("error") else None,
                bus=runtime.bus,
                console_hub=runtime.console_hub,
                device_uid=device.uid,
            )

    elif kind == "event":
        event_name = str(message.get("event") or "agent_event")[:64]
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        async with runtime.db.sessionmaker() as session:
            await audit.record(
                session,
                action=f"agent.{event_name}"[:64],
                actor="agent",
                device=device,
                detail=data,
            )
        await events.emit(runtime.console_hub, events.EVENT_NOTICE, device_uid=device.uid, data={"event": event_name, **data})

    elif kind == "uninstall_ack":
        async with runtime.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is not None:
                row.uninstalled_at_ms = now_ms()
                await session.commit()
        runtime.registry.detach(device.id)
        await events.emit(
            runtime.console_hub, events.EVENT_UNINSTALL_PROGRESS, device_uid=device.uid, data={"stage": "acked"}
        )

    elif kind == "pong":
        return

    else:
        log.debug("agent %s sent unknown message %r", device.uid, kind)


async def _handle_binary(runtime: Runtime, device: Device, connection: AgentConnection, blob: bytes) -> None:
    try:
        header, payload = decode(blob)
    except FramingError as exc:
        log.warning("agent %s sent malformed frame: %s", device.uid, exc)
        return

    kind = header.get("t")
    if kind == "ctrl":
        token = str(header.get("session") or "")
        if token and runtime.control.push_frame(token, payload):
            return
        return

    if kind != "frame":
        log.debug("agent %s sent unknown binary %r", device.uid, kind)
        return

    async with runtime.db.sessionmaker() as session:
        row = await session.get(Device, device.id)
        if row is None:
            return
        config = await runtime.store.effective(session, row.settings or {})
        await ingest.store_frame(
            runtime,
            session,
            row,
            payload,
            ts_ms=int(header.get("ts") or now_ms()),
            width=int(header.get("w") or 0),
            height=int(header.get("h") or 0),
            session_id=int(header.get("session_id") or 0),
            kind=str(header.get("kind") or "screen"),
            settings_values=config,
        )


__all__ = ["CLOSE_GONE", "CLOSE_IDLE", "CLOSE_UNAUTHORIZED", "router"]