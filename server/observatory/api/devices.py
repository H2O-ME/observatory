"""Console device endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_admin
from ..models import Command, Device, now_ms
from ..schemas import (
    CommandCreate,
    CommandListOut,
    CommandOut,
    DeviceEnvelope,
    DeviceListOut,
    DevicePatch,
    OkOut,
    UninstallAccepted,
    UninstallRequest,
)
from ..services import audit, commands as command_service, events, views
from ..services.runtime import Runtime, get_runtime
from ..services.settings_store import PER_DEVICE_KEYS, SettingsError, normalize

log = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(require_admin)])


async def load_device(session: AsyncSession, uid: str) -> Device:
    result = await session.execute(select(Device).where(Device.uid == uid))
    device = result.scalars().first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device


@router.get("", response_model=DeviceListOut)
async def list_devices(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> DeviceListOut:
    result = await session.execute(select(Device).order_by(Device.enrolled_at_ms.asc()))
    devices = list(result.scalars().all())
    payloads = await views.devices_out(runtime, session, devices)
    return DeviceListOut(
        devices=payloads,  # type: ignore[arg-type]
        server_time_ms=now_ms(),
        server_version=runtime.version,
    )


@router.get("/{uid}", response_model=DeviceEnvelope)
async def get_device(
    uid: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> DeviceEnvelope:
    device = await load_device(session, uid)
    pending = await views.uninstall_pending_ids(session, [device.id])
    frame_ids = await views.latest_frame_ids(session, [device.id])
    payload = await views.device_out(
        runtime,
        session,
        device,
        uninstall_pending=device.id in pending,
        latest_frame_id=frame_ids.get(device.id),
    )
    return DeviceEnvelope(device=payload, server_time_ms=now_ms())  # type: ignore[arg-type]


@router.patch("/{uid}", response_model=DeviceEnvelope)
async def patch_device(
    uid: str,
    payload: DevicePatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> DeviceEnvelope:
    device = await load_device(session, uid)
    changes: dict = {}

    if payload.name is not None:
        name = payload.name.strip()
        if len(name) > 120:
            raise HTTPException(status_code=400, detail="名称过长")
        device.name = name
        changes["name"] = name
    if payload.note is not None:
        device.note = payload.note.strip()[:250]
        changes["note"] = device.note
    if payload.settings is not None:
        overrides = dict(device.settings or {})
        for key, value in payload.settings.items():
            if key not in PER_DEVICE_KEYS:
                raise HTTPException(status_code=400, detail=f"该设置不能按设备覆盖: {key}")
            try:
                overrides[key] = normalize(key, value)
            except SettingsError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        device.settings = overrides
        changes["settings"] = overrides

    await session.commit()
    await audit.record(
        session, action=audit.ACTION_DEVICE_UPDATE, device=device, ip=client_ip(request), detail=changes
    )

    # Push the new config immediately so a connected agent does not wait for its
    # next poll to pick up the change.
    effective = await runtime.store.effective(session, device.settings or {})
    await runtime.agent_hub.send_json(device.id, {"t": "config", "config": effective})

    await events.emit(runtime.console_hub, events.EVENT_DEVICE_UPDATED, device_uid=device.uid)
    pending = await views.uninstall_pending_ids(session, [device.id])
    frame_ids = await views.latest_frame_ids(session, [device.id])
    out = await views.device_out(
        runtime,
        session,
        device,
        uninstall_pending=device.id in pending,
        latest_frame_id=frame_ids.get(device.id),
    )
    return DeviceEnvelope(device=out, server_time_ms=now_ms())  # type: ignore[arg-type]


@router.delete("/{uid}", response_model=OkOut)
async def delete_device(
    uid: str,
    request: Request,
    purge_remote: bool = Query(default=True, description="同时删除该设备在归档仓库中的文件"),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> OkOut:
    from ..services import ingest

    device = await load_device(session, uid)
    if runtime.registry.get(device.id) is not None:
        # Removing a live device leaves a machine that thinks it is enrolled but
        # can never authenticate again; ask the operator to uninstall first.
        raise HTTPException(status_code=409, detail="设备仍在线，请先远程卸载再删除记录")

    await audit.record(
        session,
        action=audit.ACTION_DEVICE_DELETE,
        device=device,
        ip=client_ip(request),
        detail={"purge_remote": purge_remote},
    )
    runtime.control.revoke_device(device.id)
    await ingest.delete_device_frames(runtime, device)

    if purge_remote:
        archiver = getattr(runtime, "archive", None)
        if archiver is not None:
            values = await runtime.store.load(session)
            if values.get("archive_enabled") and values.get("archive_repo"):
                try:
                    await archiver.delete_device(
                        device.uid,
                        repo=str(values.get("archive_repo") or ""),
                        token=str(values.get("archive_token") or ""),
                        branch=str(values.get("archive_branch") or "main"),
                        prefix=str(values.get("archive_path_prefix") or "frames"),
                    )
                except Exception as exc:  # noqa: BLE001 - archive is best effort
                    log.warning("remote archive purge failed for %s: %s", device.uid, exc)

    await session.delete(device)
    await session.commit()
    await events.emit(runtime.console_hub, events.EVENT_DEVICE_REMOVED, device_uid=uid)
    return OkOut(message="设备记录已删除")


@router.post("/{uid}/uninstall", response_model=UninstallAccepted)
async def uninstall_device(
    uid: str,
    payload: UninstallRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> UninstallAccepted:
    device = await load_device(session, uid)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需要确认才能真正卸载")
    if device.uninstalled_at_ms:
        raise HTTPException(status_code=409, detail="该设备已经卸载过")

    presence = await views.device_out(runtime, session, device)
    if not presence["presence"]["capabilities"]["uninstall"]:
        raise HTTPException(status_code=409, detail="设备当前不在线，无法下发卸载指令")

    await audit.record(
        session,
        action=audit.ACTION_DEVICE_UNINSTALL,
        device=device,
        ip=client_ip(request),
        detail={"reason": "console request"},
    )
    command = await command_service.create(
        session,
        device=device,
        command_type="uninstall",
        payload={},
        actor="console",
        bus=runtime.bus,
        agent_hub=runtime.agent_hub,
        console_hub=runtime.console_hub,
    )
    await events.emit(
        runtime.console_hub,
        events.EVENT_UNINSTALL_PROGRESS,
        device_uid=device.uid,
        data={"stage": "requested", "command_id": command.id},
    )
    return UninstallAccepted(
        command_id=command.id,
        status=command.status,  # type: ignore[arg-type]
        message="卸载指令已下发，受控端将以 SYSTEM 权限自行清理，不会弹 UAC",
    )


@router.post("/{uid}/commands", response_model=CommandOut)
async def create_command(
    uid: str,
    payload: CommandCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> CommandOut:
    device = await load_device(session, uid)
    if payload.type in command_service.CONFIRM_REQUIRED and not payload.confirm:
        raise HTTPException(status_code=400, detail=f"{payload.type} 需要显式确认")
    try:
        command = await command_service.create(
            session,
            device=device,
            command_type=payload.type,
            payload=payload.payload,
            actor="console",
            bus=runtime.bus,
            agent_hub=runtime.agent_hub,
            console_hub=runtime.console_hub,
        )
    except command_service.CommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.record(
        session,
        action=audit.ACTION_COMMAND_CREATE,
        device=device,
        ip=client_ip(request),
        detail={"type": payload.type, "command_id": command.id},
    )
    return CommandOut(**command_service.serialize(command))  # type: ignore[arg-type]


@router.get("/{uid}/commands", response_model=CommandListOut)
async def list_commands(
    uid: str,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> CommandListOut:
    device = await load_device(session, uid)
    result = await session.execute(
        select(Command)
        .where(Command.device_id == device.id)
        .order_by(Command.created_at_ms.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return CommandListOut(
        commands=[CommandOut(**command_service.serialize(row)) for row in rows],  # type: ignore[arg-type]
        server_time_ms=now_ms(),
    )


@router.get("/{uid}/commands/{command_id}", response_model=CommandOut)
async def get_command(
    uid: str,
    command_id: str,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    device = await load_device(session, uid)
    command = await session.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise HTTPException(status_code=404, detail="命令不存在")
    return CommandOut(**command_service.serialize(command))  # type: ignore[arg-type]