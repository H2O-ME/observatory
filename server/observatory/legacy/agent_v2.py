"""0.2.x agent protocol, translated onto the 0.3.0 core.

This is the *only* place in the server allowed to speak the old protocol (C7).
It is a translator, not a second implementation: commands are created and
settled by ``link.journal``, presence is computed by ``observatory.presence``,
frames go through ``media.ingest``, configuration is stripped by
``console.dto.device_safe_config``.  New code must never import this module
(``grep -r "legacy" server/observatory`` only hits ``main.py``).

It exists so a 0.2.10 agent can enrol, heartbeat, poll, receive commands, report
results, upload frames and upgrade without reinstalling (N2).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import presence
from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import (
    DEVICE_TOKEN_HEADER,
    RateLimiter,
    constant_time_compare,
    device_token_hash,
    new_device_token,
)
from ..core.db import get_session
from ..core.errors import AppError, ErrorCode
from ..core.models import Device
from ..console import audit
from ..console.deps import client_ip, store
from ..console.dto import device_safe_config
from ..console.install import consume_install_token, validate_install_token
from ..console.updates import PackageService
from ..link.agent_state import apply_agent_report
from ..link.registry import registry as links
from ..link.session import AgentSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-legacy"])
legacy_router = router

POLL_HOLD_MAX_S = 20
POLL_WAIT_STEP_S = 0.1

_ENROLL_LIMITER: RateLimiter | None = None
#: The old long-poll carrier keeps one AgentSession per device, exactly like the
#: v3 HTTP carrier. Registry values are always `link.session.AgentSession` (C1).
_HTTP_SESSIONS: dict[int, AgentSession] = {}


def _limiter(settings: Any) -> RateLimiter:
    global _ENROLL_LIMITER
    if _ENROLL_LIMITER is None:
        _ENROLL_LIMITER = RateLimiter(settings.enroll_rate_limit, settings.enroll_rate_window_s)
    return _ENROLL_LIMITER


def _journal(request: Request) -> Any:
    journal = getattr(request.app.state, "journal", None)
    if journal is None:
        raise RuntimeError("app.state.journal is not initialised")
    return journal


def _effective(session: AsyncSession, device: Device) -> Any:
    return store().effective(session, device.settings or {})


def _merge_report(device: Device, *, sysinfo: Any, state: Any) -> None:
    """The old heartbeat merged sysinfo shallowly and persisted capture health."""

    if isinstance(sysinfo, dict) and sysinfo:
        device.sysinfo = {**(device.sysinfo or {}), **sysinfo}
    capture = state.get("capture") if isinstance(state, dict) else None
    if isinstance(capture, dict):
        device.sysinfo = {
            **(device.sysinfo or {}),
            "capture": {
                "state": str(capture.get("state") or "")[:30],
                "reason": str(capture.get("reason") or "")[:200],
                "workers": capture.get("workers") if isinstance(capture.get("workers"), list) else [],
                "sessions": int(capture.get("sessions") or 0),
                "at_ms": now_ms(),
            },
        }


def _device_state(device: Device, effective: dict[str, Any], *, uninstall_pending: bool = False) -> Any:
    from dataclasses import replace

    state = presence.DeviceState.from_orm(device)
    return replace(state, settings=dict(effective), uninstall_pending=bool(uninstall_pending))


def _snapshot(device: Device, effective: dict[str, Any]) -> dict[str, Any]:
    return presence.snapshot(_device_state(device, effective))


async def _require_device(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Device:
    token = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    if not token:
        raise AppError.auth("缺少设备令牌")
    digest = device_token_hash(token, request.app.state.settings.secret_key)
    device = (await session.execute(select(Device).where(Device.token_hash == digest))).scalars().first()
    if device is None:
        raise AppError.auth("设备令牌无效")
    if device.uninstalled_at_ms:
        raise AppError.gone("该设备已卸载")
    return device


async def _touch_http(
    request: Request,
    session: AsyncSession,
    device: Device,
    *,
    register: bool = True,
) -> AgentSession:
    """Return the one AgentSession backing the 0.2.x long-poll carrier.

    The registry holds the *same* class as the v3 carriers; the legacy adapter
    below only translates wire formats and owns no state the journal does not.
    """

    effective = await _effective(session, device)
    current = links.get(device.id)
    settings = request.app.state.settings
    # A live v3 session wins over a stray old-protocol heartbeat/poll. Replacing
    # it would flap presence back to protocol 2 in the middle of an upgrade.
    v3_live = (
        current is not None
        and int(getattr(current, "protocol", 0) or 0) >= 3
        and now_ms() - int(getattr(current, "last_seen_ms", 0) or 0) <= int(settings.poll_stale_ms)
    )
    agent_session = _HTTP_SESSIONS.get(device.id)
    if agent_session is None or agent_session.closed:
        agent_session = AgentSession(
            device_id=device.id,
            uid=device.uid,
            journal=_journal(request),
            settings=settings,
            carrier="poll",
            protocol=2,
            agent_config=device_safe_config(effective),
        )
        _HTTP_SESSIONS[device.id] = agent_session
        if not v3_live:
            # First contact after a restart: pending commands created while no
            # session existed are queued here, then delivered by the next drain.
            await agent_session.replay_pending()
    agent_session.touch()
    if register and not v3_live:
        if current is None or current is agent_session:
            await links.register(device.id, agent_session)
    return agent_session


async def _take_outbox_commands(agent_session: AgentSession) -> list[Any]:
    """Drain queued downlink commands into the old long-poll/heartbeat shape.

    The old protocol has no downlink ack, so envelopes are acked as soon as they
    are serialised. Rows stay ``pending`` until the agent reports a result, which
    is what the journal's at-least-once replay expects (C3).
    """

    commands: list[Any] = []
    for envelope in agent_session.outbox.pending():
        if envelope.t == "command":
            command_id = str(envelope.body.get("command_id") or envelope.id)
            record = await agent_session.journal.get(command_id)
            if record is None:
                log.warning("legacy outbox command %s vanished from journal", command_id)
            elif int(record.device_id) == int(agent_session.device_id):
                commands.append(record)
        agent_session.outbox.ack(envelope.id)
    return commands


async def _wait_http_commands(agent_session: AgentSession, hold_s: float) -> list[Any]:
    deadline = now_ms() + int(max(0.0, hold_s) * 1000)
    while True:
        commands = await _take_outbox_commands(agent_session)
        if commands or now_ms() >= deadline:
            return commands
        await asyncio.sleep(POLL_WAIT_STEP_S)


class LegacyWsWire:
    """Translate one old WebSocket connection to/from an ``AgentSession``.

    This object is deliberately *not* registered: it is a transport adapter, so
    the shared registry keeps exactly one session type (C1).
    """

    def __init__(self, websocket: WebSocket, session: AgentSession) -> None:
        self.websocket = websocket
        self.session = session

    async def send_json(self, payload: dict[str, Any]) -> bool:
        try:
            await self.websocket.send_json(payload)
            return True
        except Exception as exc:  # noqa: BLE001 - peer close is a normal legacy event
            log.info("legacy ws send failed uid=%s: %s", self.session.uid, exc)
            return False

    async def send_bytes(self, blob: bytes) -> bool:
        try:
            await self.websocket.send_bytes(blob)
            return True
        except Exception as exc:  # noqa: BLE001 - peer close is a normal legacy event
            log.info("legacy ws binary send failed uid=%s: %s", self.session.uid, exc)
            return False

    async def send_envelope(self, envelope: Any) -> bool:
        kind = envelope.t
        body = envelope.body if isinstance(envelope.body, dict) else {}
        if kind == "command":
            command = {
                "id": str(body.get("command_id") or envelope.id),
                "type": str(body.get("type") or body.get("kind") or ""),
                "payload": body.get("payload") or {},
                # 0.2.10's WS command object has no status/result fields; it reads
                # only id/type/payload and ignores timeout_ms, but the old server
                # always sent it, so keep the exact legacy shape.
                "timeout_ms": int(body.get("deadline_ms") or envelope.ts_ms or 90_000),
            }
            return await self.send_json({"t": "command", "command": command})
        if kind == "config":
            return await self.send_json({"t": "config", "config": body.get("config") or {}})
        if kind == "ping":
            return await self.send_json({"t": "ping", "ts": int(envelope.ts_ms or now_ms())})
        if kind == "control":
            return await self._send_control(body)
        if kind == "result":
            log.debug("legacy ws ignoring server result envelope for uid=%s", self.session.uid)
            return True
        log.warning("legacy ws ignoring unknown downlink kind=%r uid=%s", kind, self.session.uid)
        return True

    async def _send_control(self, body: dict[str, Any]) -> bool:
        action = str(body.get("action") or "")
        token = body.get("session")
        if action == "start":
            return await self.send_json(
                {
                    "t": "ctrl_start",
                    "session": token,
                    "fps": body.get("fps"),
                    "quality": body.get("quality"),
                    "ttl_s": body.get("ttl_s"),
                }
            )
        if action == "input":
            if isinstance(body.get("data_b64"), str):
                try:
                    blob = base64.b64decode(str(body["data_b64"]), validate=True)
                except (ValueError, TypeError) as exc:
                    log.warning("legacy control base64 decode failed uid=%s: %s", self.session.uid, exc)
                    return True
                return await self.send_bytes(blob)
            return await self.send_json({"t": "ctrl_in", "session": token, "ev": body.get("ev") or {}})
        if action == "stop":
            return await self.send_json({"t": "ctrl_stop", "session": token})
        log.warning("legacy ws unknown control action %r uid=%s", action, self.session.uid)
        return True

    async def pump(self) -> None:
        """Push queued envelopes to the old socket, translating each one."""

        while not self.session.closed:
            queued = self.session.outbox.pending()
            if queued:
                for envelope in queued:
                    if not await self.send_envelope(envelope):
                        return
                    self.session.outbox.ack(envelope.id)
                continue
            changed = await self.session.outbox.wait_for_change(POLL_HOLD_MAX_S)
            if not changed and self.session.closed:
                return


async def _update_offer(request: Request, session: AsyncSession, device: Device) -> dict[str, Any] | None:
    return await PackageService(request.app.state.settings).update_offer(session, device)


@router.post("/enroll")
async def enroll(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = request.app.state.settings
    ip = client_ip(request)
    limiter = _limiter(settings)
    if not limiter.allow(f"enroll:{ip}"):
        raise AppError.rate(f"注册过于频繁，请 {limiter.retry_after(f'enroll:{ip}')} 秒后再试")

    uid = str(payload.get("uid") or "").strip()
    if not uid or len(uid) > 60:
        raise AppError.bad_request("设备标识不合法")

    install_token = str(payload.get("install_token") or payload.get("token") or "")
    enroll_code = str(payload.get("code") or "")
    credential_mode = "legacy_code"
    if install_token:
        credential_mode = "token"
    elif not settings.enroll_code:
        raise AppError.auth("服务器未开启注册（未配置注册码且未使用安装令牌）")
    elif not constant_time_compare(enroll_code, settings.enroll_code):
        await audit.record(
            session,
            action=audit.ACTION_ENROLL,
            ip=ip,
            detail={"ok": False, "uid": uid, "mode": "legacy_code"},
        )
        raise AppError.auth("注册码不正确")

    token = new_device_token()
    digest = device_token_hash(token, settings.secret_key)
    if credential_mode == "token":
        # Validate before touching the devices row: an invalid token must not
        # create/rotate a device as a side effect (A18).
        await validate_install_token(session, install_token, settings=settings)
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        device = Device(uid=uid, enrolled_at_ms=now_ms())
        session.add(device)
    device.hostname = str(payload.get("hostname") or "")[:250]
    device.os_name = str(payload.get("os_name") or "")[:60]
    device.os_version = str(payload.get("os_version") or "")[:120]
    device.arch = str(payload.get("arch") or "")[:30]
    device.agent_version = str(payload.get("agent_version") or "")[:30]
    device.sysinfo = payload.get("sysinfo") if isinstance(payload.get("sysinfo"), dict) else {}
    device.token_hash = digest
    device.uninstalled_at_ms = None
    device.last_seen_ms = now_ms()
    device.protocol = 3 if credential_mode == "token" else 2
    boot_id = str(payload.get("boot_id") or "")
    if boot_id:
        device.boot_id = boot_id[:64]
    if not device.name:
        device.name = str(payload.get("hostname") or uid)[:120]
    await session.commit()
    await session.refresh(device)

    if credential_mode == "token":
        await consume_install_token(session, install_token, device=device, settings=settings)

    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device=device,
        ip=ip,
        detail={"ok": True, "version": device.agent_version, "mode": credential_mode},
    )
    await _touch_http(request, session, device)
    effective = await _effective(session, device)
    heartbeat = int(effective.get("heartbeat_interval_s", 20))
    return {
        "device_uid": device.uid,
        "token": token,
        "config": device_safe_config(effective),
        "base_urls": list(settings.base_urls),
        "server_version": resolve_version(),
        "server_time_ms": now_ms(),
        "heartbeat_interval_s": heartbeat,
    }


@router.post("/heartbeat")
async def heartbeat(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(_require_device),
) -> dict[str, Any]:
    settings = request.app.state.settings
    apply_agent_report(device, payload, protocol=3 if int(device.protocol or 0) == 3 else 2)
    _merge_report(device, sysinfo=payload.get("sysinfo"), state=payload.get("state"))
    await session.commit()

    agent_session = await _touch_http(request, session, device)
    effective = await _effective(session, device)
    commands = await _take_outbox_commands(agent_session)
    update = None
    if effective.get("auto_update", True):
        update = await _update_offer(request, session, device)
    return {
        "config": device_safe_config(effective),
        "server_time_ms": now_ms(),
        "heartbeat_interval_s": int(effective.get("heartbeat_interval_s", 20)),
        "poll_hold_s": min(POLL_HOLD_MAX_S, int(getattr(settings, "poll_hold_s", POLL_HOLD_MAX_S) or POLL_HOLD_MAX_S)),
        "update": update,
        "uninstall": any(getattr(record, "kind", "") == "uninstall" for record in commands),
        "commands": [record.to_legacy() for record in commands],
    }


@router.get("/commands")
async def poll_commands(
    request: Request,
    wait: int = 0,
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(_require_device),
) -> dict[str, Any]:
    settings = request.app.state.settings
    hold_max = min(POLL_HOLD_MAX_S, int(getattr(settings, "poll_hold_s", POLL_HOLD_MAX_S) or POLL_HOLD_MAX_S))
    hold = max(0, min(hold_max, int(wait)))
    agent_session = await _touch_http(request, session, device)
    commands = await _take_outbox_commands(agent_session)
    if not commands and hold:
        commands = await _wait_http_commands(agent_session, hold)

    device.last_seen_ms = now_ms()
    await session.commit()
    effective = await _effective(session, device)
    return {
        "commands": [record.to_legacy() for record in commands],
        "server_time_ms": now_ms(),
        "config": device_safe_config(effective),
        "update": await _update_offer(request, session, device),
    }


@router.post("/commands/{command_id}/result")
async def command_result(
    command_id: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(_require_device),
) -> dict[str, Any]:
    journal = _journal(request)
    record = await journal.get(command_id)
    if record is None or int(record.device_id) != device.id:
        raise AppError.gone("命令不存在")
    error_code = str(payload.get("error_code") or "")
    if error_code and error_code not in {str(code) for code in ErrorCode}:
        error_code = str(ErrorCode.INTERNAL)
    await journal.settle_result(
        command_id,
        ok=bool(payload.get("ok")),
        result=payload.get("result") if isinstance(payload.get("result"), dict) else None,
        error=str(payload.get("error"))[:2000] if payload.get("error") else None,
        error_code=error_code or None,
    )
    device.last_seen_ms = now_ms()
    await session.commit()
    return {"ok": True, "message": "ok", "server_time_ms": now_ms()}


@router.get("/state")
async def device_state(
    request: Request,
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(_require_device),
) -> dict[str, Any]:
    settings = request.app.state.settings
    await _touch_http(request, session, device)
    device.last_seen_ms = now_ms()
    await session.commit()
    effective = await _effective(session, device)
    return {
        "presence": _snapshot(device, effective),
        "server_version": resolve_version(),
        "server_time_ms": now_ms(),
        "base_urls": list(settings.base_urls),
        "config": device_safe_config(effective),
    }


@router.post("/uninstall/ack")
async def uninstall_ack(
    request: Request,
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(_require_device),
) -> dict[str, Any]:
    device.uninstalled_at_ms = now_ms()
    await session.commit()
    await links.unregister(device.id)
    _HTTP_SESSIONS.pop(device.id, None)
    await audit.record(
        session,
        action=audit.ACTION_DEVICE_UNINSTALL,
        device=device,
        ip=client_ip(request),
        detail={"stage": "acked", "protocol": 2},
    )
    return {"ok": True, "message": "已记录卸载", "server_time_ms": now_ms()}


# --------------------------------------------------------------------- ws ---
def _decode_frame(blob: bytes) -> tuple[dict[str, Any], bytes]:
    if len(blob) < 4:
        raise ValueError("frame too short")
    (header_len,) = struct.unpack(">I", blob[:4])
    if header_len <= 0 or 4 + header_len > len(blob):
        raise ValueError("invalid header length")
    header = json.loads(blob[4 : 4 + header_len].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("header is not an object")
    return header, blob[4 + header_len :]


@router.websocket("/ws")
async def agent_socket(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    token = websocket.headers.get(DEVICE_TOKEN_HEADER.lower(), "") or websocket.query_params.get("token", "")
    token = token.strip()
    async with websocket.app.state.db.sessionmaker() as session:
        device = None
        if token:
            digest = device_token_hash(token, settings.secret_key)
            device = (await session.execute(select(Device).where(Device.token_hash == digest))).scalars().first()
    if device is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="invalid device token")
        return
    if device.uninstalled_at_ms:
        await websocket.accept()
        await websocket.close(code=4002, reason="device uninstalled")
        return

    await websocket.accept()
    async with websocket.app.state.db.sessionmaker() as session:
        row = await session.get(Device, device.id)
        if row is None:
            await websocket.close(code=4002, reason="device gone")
            return
        row.last_seen_ms = now_ms()
        row.protocol = 3 if int(row.protocol or 0) == 3 else 2
        await session.commit()
        effective = await _effective(session, row)
        update = await PackageService(settings).update_offer(session, row)

    journal = _journal(websocket)
    agent_session = AgentSession(
        device_id=device.id,
        uid=device.uid,
        journal=journal,
        settings=settings,
        carrier="ws",
        protocol=2,
        agent_config=device_safe_config(effective),
    )
    wire = LegacyWsWire(websocket, agent_session)
    await links.register(device.id, agent_session)
    # A10/N2: commands that arrived while the old agent was offline are queued
    # before the pump starts, so welcome always precedes the first replay.
    await agent_session.replay_pending()

    pump = asyncio.create_task(wire.pump(), name=f"legacy-ws-pump-{device.id}")
    try:
        await wire.send_json(
            {
                "t": "welcome",
                "device_uid": device.uid,
                "server_version": resolve_version(),
                "server_time_ms": now_ms(),
                "heartbeat_interval_s": int(effective.get("heartbeat_interval_s", 20)),
                "config": device_safe_config(effective),
                "update": update,
                "poll_hold_s": min(
                    POLL_HOLD_MAX_S, int(getattr(settings, "poll_hold_s", POLL_HOLD_MAX_S) or POLL_HOLD_MAX_S)
                ),
            }
        )
        idle = max(30, int(getattr(settings, "ws_idle_timeout_s", 90) or 90))
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=idle)
            except asyncio.TimeoutError:
                if not await wire.send_json({"t": "ping", "ts": now_ms()}):
                    break
                continue
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            blob = message.get("bytes")
            if text is not None:
                await _handle_text(websocket, agent_session, wire, text)
            elif blob is not None:
                await _handle_binary(websocket, agent_session, blob)
            agent_session.touch()
            links.touch(agent_session.device_id)
    except WebSocketDisconnect as exc:
        log.debug("legacy ws disconnected uid=%s code=%s: %s", agent_session.uid, exc.code, exc)
    finally:
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            log.debug("legacy ws pump cancelled uid=%s", agent_session.uid)
        except Exception as exc:  # noqa: BLE001 - pump failure is observable, not fatal
            log.warning("legacy ws pump stopped uid=%s: %s", agent_session.uid, exc)
        await links.unregister(agent_session.device_id, agent_session)
        await agent_session.close(code=1001, reason="legacy carrier closed")
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is not None:
                row.last_seen_ms = now_ms()
                await session.commit()


async def _handle_text(
    websocket: WebSocket,
    agent_session: AgentSession,
    wire: LegacyWsWire,
    text: str,
) -> None:
    try:
        message = json.loads(text)
    except ValueError:
        log.warning("legacy agent %s sent invalid JSON", agent_session.uid)
        return
    if not isinstance(message, dict):
        return
    kind = message.get("t")
    journal = _journal(websocket)

    if kind == "hello":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, agent_session.device_id)
            if row is None:
                return
            apply_agent_report(row, message, protocol=3 if int(row.protocol or 0) == 3 else 2)
            _merge_report(row, sysinfo=message.get("sysinfo"), state=message.get("state"))
            await session.commit()
            config = device_safe_config(await _effective(session, row))
        await wire.send_json({"t": "config", "config": config, "server_time_ms": now_ms()})

    elif kind == "heartbeat":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, agent_session.device_id)
            if row is None:
                return
            apply_agent_report(row, message, protocol=3 if int(row.protocol or 0) == 3 else 2)
            _merge_report(row, sysinfo=message.get("sysinfo"), state=message.get("state"))
            await session.commit()
            config = await _effective(session, row)
            update = await PackageService(websocket.app.state.settings).update_offer(session, row)
        await wire.send_json(
            {
                "t": "pong",
                "server_time_ms": now_ms(),
                "config": device_safe_config(config),
                "update": update,
                "heartbeat_interval_s": int(config.get("heartbeat_interval_s", 20)),
            }
        )

    elif kind == "result":
        command_id = str(message.get("command_id") or message.get("id") or "")
        if not command_id:
            return
        error_code = str(message.get("error_code") or "")
        if error_code and error_code not in {str(code) for code in ErrorCode}:
            error_code = str(ErrorCode.INTERNAL)
        await journal.settle_result(
            command_id,
            ok=bool(message.get("ok")),
            result=message.get("result") if isinstance(message.get("result"), dict) else None,
            error=str(message["error"])[:2000] if message.get("error") else None,
            error_code=error_code or None,
        )

    elif kind == "event":
        name = str(message.get("event") or "agent_event")[:64]
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        async with websocket.app.state.db.sessionmaker() as session:
            await audit.record(
                session,
                action=f"agent.{name}"[:64],
                actor="agent",
                device_uid=agent_session.uid,
                detail=data,
            )

    elif kind == "uninstall_ack":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, agent_session.device_id)
            if row is not None:
                row.uninstalled_at_ms = now_ms()
                await session.commit()
        await links.unregister(agent_session.device_id, agent_session)

    elif kind == "pong":
        agent_session.touch()

    else:
        log.debug("legacy agent %s sent unknown message %r", agent_session.uid, kind)


async def _handle_binary(websocket: WebSocket, agent_session: AgentSession, blob: bytes) -> None:
    try:
        header, payload = _decode_frame(blob)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, struct.error) as exc:
        log.warning("legacy agent %s sent malformed binary frame: %s", agent_session.uid, exc)
        return

    kind = header.get("t")
    if kind == "ctrl":
        token = str(header.get("session") or "")
        if token:
            if not agent_session.push_control_frame(token, payload):
                log.warning("legacy control frame for unknown session %s uid=%s", token, agent_session.uid)
        else:
            log.warning("legacy control frame without session uid=%s", agent_session.uid)
        return
    if kind != "frame":
        log.debug("legacy agent %s sent unknown binary %r", agent_session.uid, kind)
        return

    try:
        from ..media import ingest as media_ingest
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体层未就绪，无法接收画面") from exc

    async with websocket.app.state.db.sessionmaker() as session:
        row = await session.get(Device, agent_session.device_id)
        if row is None:
            return
        await media_ingest.store_frame(
            session,
            row,
            payload,
            frame_id=header.get("frame_id") or None,
            device_ts_ms=int(header.get("ts") or 0) or None,
            session_id=int(header.get("session_id") or 0),
            kind=str(header.get("kind") or "screen"),
            settings=await store().load(session),
        )
        row.last_seen_ms = now_ms()
        row.protocol = 3 if int(row.protocol or 0) == 3 else 2
        await session.commit()


__all__ = [
    "LegacyWsWire",
    "agent_socket",
    "legacy_router",
    "router",
]