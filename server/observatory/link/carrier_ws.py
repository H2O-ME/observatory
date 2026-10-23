"""WebSocket carrier for the v3 link (``WS /api/v1/agent/link``).

Identical semantics to the HTTP carrier: same ``AgentSession``, same outbox,
same envelopes. The only additions are (a) a downlink pump that pushes queued
envelopes immediately and (b) binary control frames forwarded to the session's
viewer queues.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.clock import SYSTEM_CLOCK
from ..core.config import Settings
from ..core.crypto import DEVICE_TOKEN_HEADER
from ..core.errors import AppError, ErrorCode
from ..core.log import log_event
from .agent_config import build_agent_config
from .envelope import (
    EnvelopeError,
    FramingError,
    PROTOCOL_VERSION,
    decode_binary,
    decode_envelope,
    new_envelope,
)
from .journal import CommandJournal
from .limits import client_ip, enforce
from .registry import DeviceDirectory, LinkRegistry
from .session import AgentSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["link"])

CLOSE_AUTH = 4401
CLOSE_BAD_PROTOCOL = 4400
CLOSE_REPLACED = 4000
CLOSE_IDLE = 4408
#: Uninstalled device: terminal, the agent must stop reconnecting (Lead ruling).
CLOSE_UNINSTALLED = 4410
CLOSE_RATE = 4429


def _settings(websocket: WebSocket) -> Settings:
    return websocket.app.state.settings


def _registry(websocket: WebSocket) -> LinkRegistry:
    return websocket.app.state.links


def _journal(websocket: WebSocket) -> CommandJournal:
    return websocket.app.state.journal


def _directory(websocket: WebSocket) -> DeviceDirectory:
    return websocket.app.state.device_directory


@router.websocket("/link")
async def link_ws(websocket: WebSocket) -> None:
    settings = _settings(websocket)
    registry = _registry(websocket)
    journal = _journal(websocket)
    directory = _directory(websocket)
    clock = SYSTEM_CLOCK

    await websocket.accept()
    token = websocket.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    try:
        enforce(
            getattr(websocket.app.state, "link_limiter", None),
            token,
            client_ip(websocket.headers, websocket.client, settings),
        )
    except AppError as exc:
        await websocket.close(code=CLOSE_RATE, reason=exc.message)
        return
    try:
        identity = await directory.authenticate(token) if token else None
    except AppError as exc:
        # GONE → dedicated 4410 so the agent stops retrying; anything else is auth.
        code = CLOSE_UNINSTALLED if exc.code == ErrorCode.GONE else CLOSE_AUTH
        await websocket.close(code=code, reason=exc.message)
        return
    if identity is None:
        await websocket.close(code=CLOSE_AUTH, reason="invalid device token")
        return

    session = AgentSession(
        device_id=identity.device_id,
        uid=identity.uid,
        journal=journal,
        settings=settings,
        clock=clock,
        carrier="ws",
        protocol=PROTOCOL_VERSION,
        agent_config=await build_agent_config(websocket.app, identity.settings),
    )
    await registry.register(identity.device_id, session)
    session.note_carrier("ws", PROTOCOL_VERSION)
    session.touch()
    session.set_close_hook(_close_hook(websocket))
    replayed = 0
    try:
        replayed = await session.replay_pending()
    except Exception as exc:  # noqa: BLE001 - live link must survive a journal read failure
        log_event(log, logging.ERROR, "link.ws.replay_failed", device_id=identity.device_id, error=str(exc))
    log_event(
        log,
        logging.INFO,
        "link.ws.connected",
        device_id=identity.device_id,
        uid=identity.uid,
        replayed=replayed,
    )

    send_lock = asyncio.Lock()
    #: The downlink pump waits for the first inbound message so the first
    #: downlink the agent sees is `welcome`, not a replayed command (§5.6).
    link_ready = asyncio.Event()

    async def send_envelope(envelope) -> bool:
        async with send_lock:
            try:
                await websocket.send_json(envelope.to_dict())
                return True
            except (WebSocketDisconnect, RuntimeError) as exc:
                log_event(
                    log,
                    logging.DEBUG,
                    "link.ws.send_failed",
                    device_id=identity.device_id,
                    error=str(exc),
                )
                return False

    async def pump_downlink() -> None:
        await link_ready.wait()
        while not session.closed:
            queued = session.outbox.pending()
            if queued:
                for envelope in queued:
                    if not await send_envelope(envelope):
                        return
                await session.outbox.wait_for_change(settings.heartbeat_interval_s)
                continue
            changed = await session.outbox.wait_for_change(settings.heartbeat_interval_s)
            if not changed and not session.closed and not session.outbox.pending():
                ping = new_envelope("ping", {"server_time_ms": clock.now_ms()}, clock=clock)
                if not await send_envelope(ping):
                    return

    async def handle_text(text: str) -> None:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            log_event(log, logging.WARNING, "link.ws.bad_json", device_id=identity.device_id, error=str(exc))
            return
        try:
            envelope = decode_envelope(raw)
        except EnvelopeError as exc:
            log_event(
                log,
                logging.WARNING,
                "link.ws.bad_envelope",
                device_id=identity.device_id,
                error=str(exc),
            )
            if exc.code.value == "UNSUPPORTED":
                await websocket.close(code=CLOSE_BAD_PROTOCOL, reason=exc.message)
            return
        if not session.accept_message(envelope.id):
            log_event(
                log,
                logging.DEBUG,
                "link.ws.duplicate",
                device_id=identity.device_id,
                msg_id=envelope.id,
            )
            return
        session.touch()
        for reply in await session.handle_uplink(envelope):
            await send_envelope(reply)
        # WS has no HTTP response body, so the uplink cursor travels in an
        # explicit `pong` envelope (ack_id = last processed uplink id).
        ack = session.ack_envelope()
        if ack is not None:
            await send_envelope(ack)
        link_ready.set()

    async def handle_binary(blob: bytes) -> None:
        try:
            header, body = decode_binary(blob)
        except FramingError as exc:
            log_event(log, logging.WARNING, "link.ws.bad_binary", device_id=identity.device_id, error=str(exc))
            return
        session.touch()
        if header.get("t") != "ctrl":
            log_event(
                log,
                logging.WARNING,
                "link.ws.unknown_binary",
                device_id=identity.device_id,
                header=header,
            )
            return
        await session.on_binary(header, body)
        link_ready.set()

    pump = asyncio.create_task(pump_downlink(), name=f"link-ws-pump-{identity.device_id}")
    idle_s = max(5.0, settings.ws_stale_ms / 1000.0)
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=idle_s)
            except asyncio.TimeoutError:
                log_event(log, logging.INFO, "link.ws.idle", device_id=identity.device_id)
                with contextlib.suppress(RuntimeError):
                    # Reason: the socket may already be closed by the peer; the
                    # close is best-effort and the finally block cleans up.
                    await websocket.close(code=CLOSE_IDLE, reason="no uplink for stale window")
                break
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            blob = message.get("bytes")
            if text is not None:
                await handle_text(text)
            elif blob is not None:
                await handle_binary(blob)
    except WebSocketDisconnect:
        log_event(log, logging.INFO, "link.ws.disconnected", device_id=identity.device_id)
    except Exception as exc:  # noqa: BLE001 - one bad message must not crash the app
        # Observable: log with stack trace, then close the link cleanly below.
        log_event(log, logging.ERROR, "link.ws.error", device_id=identity.device_id, error=str(exc))
        log.exception("websocket link failed")
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            # Reason: cancellation is the expected way to stop the downlink pump.
            await pump
        session.set_close_hook(None)
        await registry.unregister(identity.device_id, session)
        await session.close(code=CLOSE_REPLACED, reason="carrier closed")
        log_event(log, logging.INFO, "link.ws.closed", device_id=identity.device_id)


def _close_hook(websocket: WebSocket):
    async def _close(code: int, reason: str) -> None:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            # Reason: session shutdown may race the peer's own disconnect.
            await websocket.close(code=code, reason=reason)

    return _close


__all__ = [
    "CLOSE_AUTH",
    "CLOSE_BAD_PROTOCOL",
    "CLOSE_IDLE",
    "CLOSE_RATE",
    "CLOSE_REPLACED",
    "CLOSE_UNINSTALLED",
    "router",
]