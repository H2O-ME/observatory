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
import json
import logging
import struct
from dataclasses import dataclass, field
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
from ..console.dto import budget_ms, device_safe_config
from ..console.install import consume_install_token, validate_install_token
from ..console.updates import PackageService
from ..link.envelope import Result
from ..link.registry import registry as links

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-legacy"])
legacy_router = router

POLL_HOLD_MAX_S = 20

_ENROLL_LIMITER: RateLimiter | None = None
_HTTP_SESSIONS: dict[int, "LegacyHttpSession"] = {}


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


async def _touch_http(request: Request, session: AsyncSession, device: Device) -> "LegacyHttpSession":
    """Register/refresh the duck-typed HTTP carrier so presence sees it online."""

    sess = _HTTP_SESSIONS.get(device.id)
    if sess is None:
        sess = LegacyHttpSession(device_id=device.id, uid=device.uid)
        _HTTP_SESSIONS[device.id] = sess
    sess.journal = _journal(request)
    sess.touch()
    await links.register_legacy(device.id, sess)
    return sess


@dataclass
class LegacyControlStream:
    token: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=4))
    closed: bool = False


class LegacyCarrier:
    """Duck-typed ``AgentSession`` for the 0.2.x protocol."""

    protocol = 2

    def __init__(self, *, device_id: int, uid: str, carrier: str) -> None:
        self.device_id = device_id
        self.uid = uid
        self.carrier = carrier
        self.last_seen_ms = now_ms()
        self.journal: Any = None
        self._streams: dict[str, LegacyControlStream] = {}

    def touch(self) -> None:
        self.last_seen_ms = now_ms()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.last_seen_ms = 0

    # -- downlink ----------------------------------------------------------
    async def _send_json(self, payload: dict[str, Any]) -> bool:
        raise NotImplementedError

    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int = 0) -> str:
        raise NotImplementedError

    # -- request/response --------------------------------------------------
    async def request(self, kind: str, body: dict[str, Any], *, timeout_s: float) -> Result:
        if self.journal is None:
            raise RuntimeError("legacy carrier has no journal bound")
        deadline = self.journal.deadline_for(kind)
        started = now_ms()
        record = await self.journal.create(self.device_id, kind, body, actor="console", timeout_ms=deadline)
        async with self.journal.expect(record.id) as slot:
            envelope = record.to_envelope_body(deadline_ms=deadline)
            sent = await self.send("command", envelope, deadline_ms=deadline)
            if not sent:
                return Result(
                    ok=False,
                    status="error",
                    result=None,
                    error="兼容链路不可用",
                    error_code=str(ErrorCode.UNSUPPORTED),
                    retryable=True,
                    command_id=record.id,
                    deadline_ms=deadline,
                    server_time_ms=now_ms(),
                    latency_ms=0,
                )
            settled = await slot.wait(timeout_s)
        if settled is not None:
            return settled
        await self.journal.mark_timeout(record.id)
        return Result.timed_out(
            command_id=record.id,
            error=f"受控端在 {timeout_s:.0f} 秒内没有响应",
            deadline_ms=deadline,
            server_time_ms=now_ms(),
            latency_ms=max(0, now_ms() - started),
        )

    # -- control streams ---------------------------------------------------
    def open_control(self, token: str) -> LegacyControlStream:
        stream = self._streams.get(token)
        if stream is None:
            stream = LegacyControlStream(token=token)
            self._streams[token] = stream
        return stream

    def close_control(self, token: str) -> None:
        stream = self._streams.pop(token, None)
        if stream is not None:
            stream.closed = True

    def push_control(self, token: str, payload: bytes) -> bool:
        stream = self._streams.get(token)
        if stream is None or stream.closed:
            log.warning("legacy control frame for unknown session %s", token)
            return False
        while stream.queue.full():
            try:
                stream.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            stream.queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            log.warning("legacy control queue full for session %s: %s", token, exc)
            return False
        return True


class LegacyHttpSession(LegacyCarrier):
    carrier = "poll"

    def __init__(self, *, device_id: int, uid: str) -> None:
        super().__init__(device_id=device_id, uid=uid, carrier="poll")

    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int = 0) -> str:
        if kind == "command":
            # The long-poll carrier cannot push: the command stays pending and is
            # claimed by the next GET /agent/commands. Returning "" makes the
            # caller leave the journal row deliverable instead of marking it sent.
            return ""
        # Config/control/ping have no 0.2.x long-poll counterpart.  The next
        # heartbeat returns the config, and control requires a WebSocket.
        log.info("legacy http carrier ignoring %s downlink device=%s", kind, self.uid)
        return ""


class LegacyWsSession(LegacyCarrier):
    carrier = "ws"

    def __init__(self, *, device_id: int, uid: str, websocket: WebSocket) -> None:
        super().__init__(device_id=device_id, uid=uid, carrier="ws")
        self.websocket = websocket

    async def _send_json(self, payload: dict[str, Any]) -> bool:
        try:
            await self.websocket.send_json(payload)
            return True
        except Exception as exc:  # noqa: BLE001 - socket closed by the peer is normal
            log.info("legacy ws send failed uid=%s: %s", self.uid, exc)
            return False

    async def forward_binary(self, blob: bytes) -> bool:
        """Forward a browser control frame to the 0.2.x agent unchanged (C7)."""

        try:
            await self.websocket.send_bytes(blob)
            return True
        except Exception as exc:  # noqa: BLE001 - socket closed by the peer is normal
            log.info("legacy ws binary forward failed uid=%s: %s", self.uid, exc)
            return False

    async def _mark_command_dispatched(self, command_id: str) -> None:
        if self.journal is None:
            return
        claimed = await self.journal.claim_pending(self.device_id, 20)
        if not any(record.id == command_id for record in claimed):
            log.debug("command %s was not pending when marking dispatched", command_id)

    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int = 0) -> str:
        if kind == "command":
            command_id = str(body.get("command_id") or body.get("id") or "")
            command = {
                "id": command_id,
                "type": str(body.get("type") or body.get("kind") or ""),
                "payload": body.get("payload") or {},
                "status": "dispatched",
                "result": None,
                "error": None,
                "created_at_ms": now_ms(),
                "dispatched_at_ms": now_ms(),
                "finished_at_ms": None,
            }
            ok = await self._send_json({"t": "command", "command": command})
            if ok:
                await self._mark_command_dispatched(command_id)
            return command_id if ok else ""
        if kind == "config":
            await self._send_json({"t": "config", "config": body.get("config") or {}})
            return ""
        if kind == "ping":
            await self._send_json({"t": "ping", "ts": now_ms()})
            return ""
        if kind == "control":
            action = str(body.get("action") or "")
            if action == "start":
                await self._send_json(
                    {
                        "t": "ctrl_start",
                        "session": body.get("session"),
                        "fps": body.get("fps"),
                        "quality": body.get("quality"),
                        "ttl_s": body.get("ttl_s"),
                    }
                )
            elif action == "input":
                await self._send_json(
                    {"t": "ctrl_in", "session": body.get("session"), "ev": body.get("ev") or {}}
                )
            elif action == "stop":
                await self._send_json({"t": "ctrl_stop", "session": body.get("session")})
            else:
                log.warning("legacy ws unknown control action %r uid=%s", action, self.uid)
            return ""
        log.warning("legacy ws ignoring unknown downlink kind=%r uid=%s", kind, self.uid)
        return ""


async def _claim(journal: Any, device_id: int) -> list[Any]:
    return list(await journal.claim_pending(device_id, 10))


async def _poll_for_commands(journal: Any, device_id: int, hold_s: float) -> list[Any]:
    """Wait up to ``hold_s`` for a command; the legacy long-poll equivalent."""

    deadline = now_ms() + int(hold_s * 1000)
    while True:
        commands = await _claim(journal, device_id)
        if commands:
            return commands
        if now_ms() >= deadline:
            return []
        await asyncio.sleep(0.25)


def _legacy_commands(commands: list[Any]) -> list[dict[str, Any]]:
    return [record.to_legacy() for record in commands]


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
    device.last_seen_ms = now_ms()
    device.protocol = 3 if int(device.protocol or 0) == 3 else 2
    if payload.get("agent_version"):
        device.agent_version = str(payload["agent_version"])[:30]
    if payload.get("boot_id"):
        device.boot_id = str(payload["boot_id"])[:64]
    _merge_report(device, sysinfo=payload.get("sysinfo"), state=payload.get("state"))
    await session.commit()

    await _touch_http(request, session, device)
    effective = await _effective(session, device)
    journal = _journal(request)
    commands = await _claim(journal, device.id)
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
        "commands": _legacy_commands(commands),
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
    journal = _journal(request)
    commands = await _claim(journal, device.id)
    if not commands and hold:
        await _touch_http(request, session, device)
        commands = await _poll_for_commands(journal, device.id, hold)

    device.last_seen_ms = now_ms()
    await session.commit()
    effective = await _effective(session, device)
    return {
        "commands": _legacy_commands(commands),
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


async def _send_pending(ws_session: LegacyWsSession, journal: Any) -> None:
    for record in await _claim(journal, ws_session.device_id):
        await ws_session.send("command", record.to_envelope_body(deadline_ms=budget_ms(record.kind)))


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
    ws_session = LegacyWsSession(device_id=device.id, uid=device.uid, websocket=websocket)
    await links.register_legacy(device.id, ws_session)
    journal = _journal(websocket)
    ws_session.journal = journal

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

    await websocket.send_json(
        {
            "t": "welcome",
            "device_uid": device.uid,
            "server_version": resolve_version(),
            "server_time_ms": now_ms(),
            "heartbeat_interval_s": int(effective.get("heartbeat_interval_s", 20)),
            "config": device_safe_config(effective),
            "update": update,
            "poll_hold_s": min(POLL_HOLD_MAX_S, int(getattr(settings, "poll_hold_s", POLL_HOLD_MAX_S) or POLL_HOLD_MAX_S)),
        }
    )
    # A10/N2: commands that arrived while the old agent was offline are drained
    # on reconnect instead of waiting for an HTTP heartbeat that never comes.
    await _send_pending(ws_session, journal)

    idle = max(30, int(getattr(settings, "ws_idle_timeout_s", 90) or 90))
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=idle)
            except asyncio.TimeoutError:
                if not await ws_session._send_json({"t": "ping", "ts": now_ms()}):
                    break
                continue
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            blob = message.get("bytes")
            if text is not None:
                await _handle_text(websocket, ws_session, text)
            elif blob is not None:
                await _handle_binary(websocket, ws_session, blob)
            ws_session.touch()
            links.touch(ws_session.device_id)
    except WebSocketDisconnect:
        pass
    finally:
        await links.unregister(ws_session.device_id, ws_session)
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, device.id)
            if row is not None:
                row.last_seen_ms = now_ms()
                await session.commit()


async def _handle_text(websocket: WebSocket, ws_session: LegacyWsSession, text: str) -> None:
    try:
        message = json.loads(text)
    except ValueError:
        log.warning("legacy agent %s sent invalid JSON", ws_session.uid)
        return
    if not isinstance(message, dict):
        return
    kind = message.get("t")
    journal = _journal(websocket)

    if kind == "hello":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, ws_session.device_id)
            if row is None:
                return
            row.agent_version = str(message.get("agent_version", row.agent_version))[:30]
            row.last_seen_ms = now_ms()
            row.protocol = 3 if int(row.protocol or 0) == 3 else 2
            if message.get("boot_id"):
                row.boot_id = str(message["boot_id"])[:64]
            _merge_report(row, sysinfo=message.get("sysinfo"), state=message.get("state"))
            await session.commit()
            config = device_safe_config(await _effective(session, row))
        await ws_session._send_json({"t": "config", "config": config, "server_time_ms": now_ms()})

    elif kind == "heartbeat":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, ws_session.device_id)
            if row is None:
                return
            row.last_seen_ms = now_ms()
            row.protocol = 3 if int(row.protocol or 0) == 3 else 2
            if message.get("agent_version"):
                row.agent_version = str(message["agent_version"])[:30]
            if message.get("boot_id"):
                row.boot_id = str(message["boot_id"])[:64]
            _merge_report(row, sysinfo=message.get("sysinfo"), state=message.get("state"))
            await session.commit()
            config = await _effective(session, row)
            update = await PackageService(websocket.app.state.settings).update_offer(session, row)
        await ws_session._send_json(
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
                device_uid=ws_session.uid,
                detail=data,
            )

    elif kind == "uninstall_ack":
        async with websocket.app.state.db.sessionmaker() as session:
            row = await session.get(Device, ws_session.device_id)
            if row is not None:
                row.uninstalled_at_ms = now_ms()
                await session.commit()
        await links.unregister(ws_session.device_id, ws_session)

    elif kind == "pong":
        ws_session.touch()

    else:
        log.debug("legacy agent %s sent unknown message %r", ws_session.uid, kind)


async def _handle_binary(websocket: WebSocket, ws_session: LegacyWsSession, blob: bytes) -> None:
    try:
        header, payload = _decode_frame(blob)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, struct.error) as exc:
        log.warning("legacy agent %s sent malformed binary frame: %s", ws_session.uid, exc)
        return

    kind = header.get("t")
    if kind == "ctrl":
        token = str(header.get("session") or "")
        if token:
            ws_session.push_control(token, payload)
        else:
            log.warning("legacy control frame without session uid=%s", ws_session.uid)
        return
    if kind != "frame":
        log.debug("legacy agent %s sent unknown binary %r", ws_session.uid, kind)
        return

    try:
        from ..media import ingest as media_ingest
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体层未就绪，无法接收画面") from exc

    async with websocket.app.state.db.sessionmaker() as session:
        row = await session.get(Device, ws_session.device_id)
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
    "LegacyCarrier",
    "LegacyControlStream",
    "LegacyHttpSession",
    "LegacyWsSession",
    "agent_socket",
    "legacy_router",
    "router",
]