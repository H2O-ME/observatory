"""Device-facing HTTP endpoints (enroll, heartbeat, poll, frame upload).

These are served on **both** domains. Everything here is authenticated with the
``X-Device-Token`` header except ``/enroll``, which is protected by the shared
enrollment code and a rate limiter.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_device
from ..models import Command, Device, now_ms
from ..schemas import (
    CommandResultIn,
    EnrollIn,
    EnrollOut,
    FrameUploadOut,
    HeartbeatIn,
    HeartbeatOut,
    OkOut,
    UpdateInfoOut,
)
from ..security import device_token_hash, new_device_token
from ..services import audit, commands as command_service, events, ingest
from ..services.presence import snapshot
from ..services.runtime import Runtime, get_runtime
from ..version import is_newer

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/enroll", response_model=EnrollOut)
async def enroll(
    payload: EnrollIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> EnrollOut:
    ip = client_ip(request)
    key = f"enroll:{ip}"
    if runtime.enroll_limiter and not runtime.enroll_limiter.allow(key):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")

    expected = runtime.settings.enroll_code
    if not expected:
        raise HTTPException(status_code=403, detail="服务器未开启注册（OBS_ENROLL_CODE 为空）")
    if not hmac.compare_digest(payload.code.strip(), expected):
        await audit.record(session, action=audit.ACTION_ENROLL, ip=ip, detail={"ok": False, "uid": payload.uid})
        raise HTTPException(status_code=401, detail="注册码不正确")
    if not payload.uid or len(payload.uid) > 60:
        raise HTTPException(status_code=400, detail="设备标识不合法")

    token = new_device_token()
    digest = device_token_hash(token, runtime.settings.secret_key)

    result = await session.execute(select(Device).where(Device.uid == payload.uid))
    device = result.scalars().first()
    if device is None:
        device = Device(uid=payload.uid, enrolled_at_ms=now_ms())
        session.add(device)

    device.hostname = payload.hostname[:250]
    device.os_name = payload.os_name[:60]
    device.os_version = payload.os_version[:120]
    device.arch = payload.arch[:30]
    device.agent_version = payload.agent_version[:30]
    device.sysinfo = payload.sysinfo or {}
    device.token_hash = digest
    device.uninstalled_at_ms = None
    device.last_seen_ms = now_ms()
    if not device.name:
        device.name = payload.hostname[:120] or payload.uid
    await session.commit()
    await session.refresh(device)

    runtime.registry.attach(device.id, "poll", ip)
    effective = await runtime.store.effective(session, device.settings or {})
    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device=device,
        ip=ip,
        detail={"ok": True, "version": payload.agent_version},
    )
    await events.emit(runtime.console_hub, events.EVENT_DEVICE_UPDATED, device_uid=device.uid)
    log.info("device enrolled uid=%s ip=%s version=%s", device.uid, ip, payload.agent_version)

    return EnrollOut(
        device_uid=device.uid,
        token=token,
        config=effective,
        base_urls=runtime.settings.base_urls,
        server_version=runtime.version,
        server_time_ms=now_ms(),
        heartbeat_interval_s=int(effective.get("heartbeat_interval_s", 20)),
    )


async def _update_info(runtime: Runtime, session: AsyncSession, device: Device) -> UpdateInfoOut | None:
    if runtime.packages is None:
        return None
    package = await runtime.packages.latest(session, "agent")
    if package is None:
        return None
    if not is_newer(package.version, device.agent_version or "0.0.0"):
        return None
    return UpdateInfoOut(
        version=package.version,
        url=runtime.packages.url_for(package),
        sha256=package.sha256,
        size_bytes=package.size_bytes,
    )


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    payload: HeartbeatIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> HeartbeatOut:
    ip = client_ip(request)
    device.last_seen_ms = now_ms()
    if payload.agent_version:
        device.agent_version = payload.agent_version[:30]
    if payload.sysinfo:
        # Heartbeats carry a cheap subset; merge so a light payload never erases
        # hostname/os/model collected at enrolment.
        device.sysinfo = {**(device.sysinfo or {}), **payload.sysinfo}
    runtime.registry.attach(device.id, "poll", ip)
    await session.commit()

    effective = await runtime.store.effective(session, device.settings or {})
    update = await _update_info(runtime, session, device) if effective.get("auto_update", True) else None
    pending = await command_service.claim_pending(session, device.id, limit=10)

    return HeartbeatOut(
        config=effective,
        server_time_ms=now_ms(),
        heartbeat_interval_s=int(effective.get("heartbeat_interval_s", 20)),
        poll_hold_s=runtime.settings.poll_hold_s,
        update=update,
        uninstall=any(command.type == "uninstall" for command in pending),
        commands=[command_service.serialize(command) for command in pending],  # type: ignore[list-item]
    )


@router.get("/commands")
async def poll_commands(
    request: Request,
    wait: int = 0,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> dict:
    """Long-poll for commands.

    Returns immediately when something is queued; otherwise waits up to ``wait``
    seconds for the notifier. The wait is capped well below any proxy timeout.
    """

    hold = max(0, min(runtime.settings.poll_hold_s, wait))
    commands = await command_service.claim_pending(session, device.id, limit=10)
    if not commands and hold:
        runtime.registry.attach(device.id, "poll", client_ip(request))
        if await runtime.notifier.wait(device.id, hold):
            commands = await command_service.claim_pending(session, device.id, limit=10)

    device.last_seen_ms = now_ms()
    await session.commit()
    return {
        "commands": [command_service.serialize(command) for command in commands],
        "server_time_ms": now_ms(),
        "config": await runtime.store.effective(session, device.settings or {}),
        "update": await _update_info(runtime, session, device),
    }


@router.post("/commands/{command_id}/result", response_model=OkOut)
async def command_result(
    command_id: str,
    payload: CommandResultIn,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> OkOut:
    command = await session.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise HTTPException(status_code=404, detail="命令不存在")

    await command_service.record_result(
        session,
        command_id=command_id,
        ok=payload.ok,
        result=payload.result,
        error=payload.error,
        bus=runtime.bus,
        console_hub=runtime.console_hub,
        device_uid=device.uid,
    )
    device.last_seen_ms = now_ms()
    await session.commit()
    return OkOut(message="ok")


@router.post("/frames", response_model=FrameUploadOut)
async def upload_frame(
    request: Request,
    ts_ms: int = 0,
    session_id: int = 0,
    kind: str = "screen",
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> FrameUploadOut:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空图像")
    if len(body) > runtime.settings.max_frame_bytes:
        raise HTTPException(status_code=413, detail="图像过大")

    runtime.registry.attach(device.id, "poll", client_ip(request))
    effective = await runtime.store.effective(session, device.settings or {})
    frame = await ingest.store_frame(
        runtime,
        session,
        device,
        body,
        ts_ms=ts_ms or now_ms(),
        session_id=session_id,
        kind=kind if kind in {"screen", "camera"} else "screen",
        settings_values=effective,
    )
    if frame is None:
        raise HTTPException(status_code=422, detail="图像无法解码")
    return FrameUploadOut(frame_id=frame.id, ts_ms=frame.ts_ms, stored=True, server_time_ms=now_ms())


@router.post("/uninstall/ack", response_model=OkOut)
async def uninstall_ack(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> OkOut:
    device.uninstalled_at_ms = now_ms()
    await session.commit()
    runtime.registry.detach(device.id)
    await runtime.agent_hub.close_device(device.id, code=4002, reason="uninstalled")
    await events.emit(
        runtime.console_hub, events.EVENT_UNINSTALL_PROGRESS, device_uid=device.uid, data={"stage": "acked"}
    )
    return OkOut(message="已记录卸载")


@router.get("/state")
async def device_state(
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    device: Device = Depends(require_device),
) -> dict:
    """The device's own view of its presence, for the agent's diagnostics log."""

    runtime.registry.attach(device.id, "poll", client_ip(request))
    device.last_seen_ms = now_ms()
    await session.commit()
    return {
        "presence": snapshot(device, runtime.registry),
        "server_version": runtime.version,
        "server_time_ms": now_ms(),
        "base_urls": runtime.settings.base_urls,
        "config": await runtime.store.effective(session, device.settings or {}),
    }


__all__ = ["router"]