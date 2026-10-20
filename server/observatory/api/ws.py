"""Console WebSocket: one socket per browser tab, server-pushed events.

The console is not allowed to infer liveness from its own clock. This socket
therefore carries a periodic ``tick`` with ``server_time_ms``; while it is down the
UI renders "状态未知" instead of guessing, which removes the class of bug where the
page claimed a device was online long after the server had lost contact.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..deps import ws_admin_token
from ..models import now_ms
from ..security import decode_session
from ..services import events
from ..services.runtime import Runtime

log = logging.getLogger(__name__)
router = APIRouter(tags=["ws"])

CLOSE_UNAUTHORIZED = 4401


@router.websocket("/ws")
async def console_socket(websocket: WebSocket) -> None:
    runtime: Runtime = websocket.app.state.runtime
    token = ws_admin_token(websocket)
    payload = decode_session(token, secret_key=runtime.settings.secret_key)
    if payload is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    await websocket.accept()
    await runtime.console_hub.register(websocket)
    await websocket.send_json(
        events.build(
            "hello",
            data={
                "version": runtime.version,
                "server_time_ms": now_ms(),
                "heartbeat_s": runtime.settings.ws_heartbeat_s,
                "channels": runtime.registry.counts(),
            },
        )
    )
    log.info("console ws connected (%d clients)", runtime.console_hub.client_count)

    heartbeat = max(5, runtime.settings.ws_heartbeat_s)
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=heartbeat)
            except asyncio.TimeoutError:
                await websocket.send_json(
                    events.build(
                        "tick",
                        data={
                            "server_time_ms": now_ms(),
                            "channels": runtime.registry.counts(),
                            "clients": runtime.console_hub.client_count,
                        },
                    )
                )
                continue

            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if not text:
                continue
            if '"ping"' in text:
                await websocket.send_json(events.build("pong", data={"server_time_ms": now_ms()}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("console ws closed: %s", exc)
    finally:
        await runtime.console_hub.unregister(websocket)
        log.info("console ws disconnected (%d clients)", runtime.console_hub.client_count)


__all__ = ["CLOSE_UNAUTHORIZED", "router"]