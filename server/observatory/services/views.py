"""Response builders shared by every endpoint that describes a device.

Centralising this is what keeps the console honest: the wall, the device header,
the file manager and the control page literally cannot disagree, because there is
one function that decides what state a device is in.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Command, Device, Frame, now_ms
from .presence import snapshot
from .runtime import Runtime


async def latest_frame_ids(session: AsyncSession, device_ids: list[int]) -> dict[int, int]:
    if not device_ids:
        return {}
    result = await session.execute(
        select(Frame.device_id, func.max(Frame.id)).where(Frame.device_id.in_(device_ids)).group_by(Frame.device_id)
    )
    return {int(row[0]): int(row[1]) for row in result.fetchall() if row[1] is not None}


async def uninstall_pending_ids(session: AsyncSession, device_ids: list[int] | None = None) -> set[int]:
    statement = select(Command.device_id).where(
        Command.type == "uninstall", Command.status.in_(("pending", "dispatched"))
    )
    if device_ids:
        statement = statement.where(Command.device_id.in_(device_ids))
    result = await session.execute(statement)
    return {int(row[0]) for row in result.fetchall()}


async def device_out(
    runtime: Runtime,
    session: AsyncSession,
    device: Device,
    *,
    uninstall_pending: bool = False,
    latest_frame_id: int | None = None,
) -> dict:
    effective = await runtime.store.effective(session, device.settings or {})
    presence = snapshot(device, runtime.registry, uninstall_pending=uninstall_pending)
    return {
        "uid": device.uid,
        "name": device.display_name,
        "hostname": device.hostname,
        "os_name": device.os_name,
        "os_version": device.os_version,
        "arch": device.arch,
        "agent_version": device.agent_version,
        "enrolled_at_ms": device.enrolled_at_ms,
        "last_seen_ms": device.last_seen_ms,
        "note": device.note,
        "presence": presence,
        "sysinfo": device.sysinfo or {},
        "settings": effective,
        "latest_frame_id": latest_frame_id,
        "server_time_ms": now_ms(),
    }


async def devices_out(runtime: Runtime, session: AsyncSession, devices: list[Device]) -> list[dict]:
    ids = [device.id for device in devices]
    frame_ids = await latest_frame_ids(session, ids)
    pending = await uninstall_pending_ids(session, ids)
    return [
        await device_out(
            runtime,
            session,
            device,
            uninstall_pending=device.id in pending,
            latest_frame_id=frame_ids.get(device.id),
        )
        for device in devices
    ]


__all__ = ["device_out", "devices_out", "latest_frame_ids", "uninstall_pending_ids"]