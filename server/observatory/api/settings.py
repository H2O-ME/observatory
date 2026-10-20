"""Console settings, audit log and system status."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_admin
from ..models import AuditEntry, Device, now_ms
from ..schemas import AuditListOut, AuditOut, SettingsOut, SettingsPatch
from ..services import audit, events, installer, storage
from ..services.runtime import Runtime, get_runtime
from ..services.settings_store import SECRET_KEYS, SettingsError, describe, public_view

log = logging.getLogger(__name__)
router = APIRouter(tags=["settings"], dependencies=[Depends(require_admin)])


@router.get("/settings", response_model=SettingsOut)
async def get_settings_payload(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> SettingsOut:
    values = await runtime.store.load(session)
    return SettingsOut(
        values=public_view(values),
        specs=describe(),
        server_version=runtime.version,
        server_time_ms=now_ms(),
    )


@router.patch("/settings", response_model=SettingsOut)
async def patch_settings(
    payload: SettingsPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> SettingsOut:
    incoming = dict(payload.values)
    for key in list(incoming):
        if key in SECRET_KEYS and incoming[key] in {"", "••••••••"}:
            # Unchanged masked secret: never overwrite a stored token with dots.
            incoming.pop(key)
    try:
        changed = await runtime.store.patch(session, incoming)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.record(
        session,
        action=audit.ACTION_SETTINGS_UPDATE,
        ip=client_ip(request),
        detail={"keys": sorted(incoming.keys())},
    )
    values = await runtime.store.load(session)
    # Push the new configuration to every connected agent immediately.
    await runtime.agent_hub.broadcast_json({"t": "config", "config": values})
    await events.emit(runtime.console_hub, events.EVENT_NOTICE, data={"message": "设置已更新", "keys": list(changed)})
    return SettingsOut(
        values=public_view(values),
        specs=describe(),
        server_version=runtime.version,
        server_time_ms=now_ms(),
    )


@router.get("/audit", response_model=AuditListOut)
async def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str = Query(default=""),
    device_uid: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
) -> AuditListOut:
    statement = select(AuditEntry)
    counter = select(func.count(AuditEntry.id))
    if action:
        statement = statement.where(AuditEntry.action == action)
        counter = counter.where(AuditEntry.action == action)
    if device_uid:
        statement = statement.where(AuditEntry.device_uid == device_uid)
        counter = counter.where(AuditEntry.device_uid == device_uid)

    total = int((await session.execute(counter)).scalar() or 0)
    rows = list(
        (
            await session.execute(statement.order_by(AuditEntry.ts_ms.desc()).offset(offset).limit(limit))
        )
        .scalars()
        .all()
    )
    return AuditListOut(
        entries=[
            AuditOut(
                id=row.id,
                ts_ms=row.ts_ms,
                actor=row.actor,
                action=row.action,
                device_uid=row.device_uid,
                device_name=row.device_name,
                ip=row.ip,
                detail=row.detail or {},
            )
            for row in rows
        ],
        total=total,
        server_time_ms=now_ms(),
    )


@router.get("/system")
async def system_status(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    devices = int((await session.execute(select(func.count(Device.id)))).scalar() or 0)
    frames_bytes = storage.directory_size_bytes(Path(runtime.settings.frames_dir))
    thumbs_bytes = storage.directory_size_bytes(Path(runtime.settings.thumbs_dir))
    try:
        db_bytes = Path(runtime.settings.db_path).stat().st_size
    except OSError:
        db_bytes = 0
    disk = shutil.disk_usage(runtime.settings.data_dir)

    return {
        "version": runtime.version,
        "uptime_s": runtime.uptime_s(),
        "server_time_ms": now_ms(),
        "devices": devices,
        "channels": runtime.registry.counts(),
        "ws_clients": runtime.console_hub.client_count,
        "pending_commands": runtime.bus.open_count(),
        "control_sessions": runtime.control.active_count(),
        "storage": {
            "frames_bytes": frames_bytes,
            "thumbs_bytes": thumbs_bytes,
            "db_bytes": db_bytes,
            "disk_total": disk.total,
            "disk_free": disk.free,
        },
        "base_urls": runtime.settings.base_urls,
        "console_host": runtime.settings.console_hosts[0] if runtime.settings.console_hosts else "",
        # Everything the console needs to offer a one-click download, including a
        # ready-made file with the enrollment code already inside.
        "install": installer.install_info(runtime),
    }


__all__ = ["router"]