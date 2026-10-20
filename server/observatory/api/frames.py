"""Captured-frame endpoints: latest frame, thumbnails, full images, timeline.

Frames are immutable once written, so they are served with a long cache lifetime
and an ETag. The previous WebUI appended ``?_: Date.now()`` to every blob URL,
which defeated exactly this caching and made scrubbing a 60-second operation.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse

from ..deps import client_ip, get_session, require_admin
from ..models import Device, Frame, now_ms
from ..schemas import FrameListOut, FrameOut, LatestFrameOut, OkOut, TimelineOut
from ..services import audit, events, storage, timeline
from ..services.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["frames"], dependencies=[Depends(require_admin)])

CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


async def load_device(session: AsyncSession, uid: str) -> Device:
    result = await session.execute(select(Device).where(Device.uid == uid))
    device = result.scalars().first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device


def _frame_out(frame: Frame) -> FrameOut:
    return FrameOut(
        id=frame.id,
        ts_ms=frame.ts_ms,
        width=frame.width,
        height=frame.height,
        size_bytes=frame.size_bytes,
        has_thumb=bool(frame.thumb_path),
        kind=frame.kind,
    )


@router.get("/{uid}/frames/latest", response_model=LatestFrameOut)
async def latest_frame(
    uid: str,
    session: AsyncSession = Depends(get_session),
) -> LatestFrameOut:
    device = await load_device(session, uid)
    result = await session.execute(
        select(Frame).where(Frame.device_id == device.id).order_by(Frame.id.desc()).limit(1)
    )
    frame = result.scalars().first()
    return LatestFrameOut(frame=_frame_out(frame) if frame else None, server_time_ms=now_ms())


@router.get("/{uid}/frames", response_model=FrameListOut)
async def list_frames(
    uid: str,
    from_ms: int = Query(default=0, ge=0),
    to_ms: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> FrameListOut:
    device = await load_device(session, uid)
    statement = select(Frame).where(Frame.device_id == device.id)
    if from_ms:
        statement = statement.where(Frame.ts_ms >= from_ms)
    if to_ms:
        statement = statement.where(Frame.ts_ms < to_ms)
    statement = statement.order_by(Frame.ts_ms.desc()).limit(limit)
    rows = list((await session.execute(statement)).scalars().all())
    return FrameListOut(frames=[_frame_out(row) for row in rows], server_time_ms=now_ms())


@router.get("/{uid}/timeline", response_model=TimelineOut)
async def device_timeline(
    uid: str,
    from_ms: int = Query(..., ge=0),
    to_ms: int = Query(..., ge=0),
    bucket_ms: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_session),
) -> TimelineOut:
    device = await load_device(session, uid)
    try:
        payload = await timeline.build(
            session,
            device_id=device.id,
            from_ms=from_ms,
            to_ms=to_ms,
            bucket_ms=bucket_ms or None,
        )
    except timeline.TimelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload["server_time_ms"] = now_ms()
    return TimelineOut(**payload)


@router.get("/{uid}/frames/{frame_id}")
async def frame_content(
    uid: str,
    frame_id: int,
    variant: str = Query(default="thumb", pattern="^(thumb|full)$"),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> FileResponse:
    device = await load_device(session, uid)
    frame = await session.get(Frame, frame_id)
    if frame is None or frame.device_id != device.id:
        raise HTTPException(status_code=404, detail="画面不存在")

    relative = frame.thumb_path if variant == "thumb" else frame.path
    if not relative:
        relative = frame.path
    try:
        path = storage.absolute(runtime.settings, relative)
    except storage.StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=410, detail="画面文件已被清理")
    return FileResponse(
        path,
        media_type=storage.media_type_for(path),
        headers={**CACHE_HEADERS, "ETag": f'"{frame.sha256 or frame.id}"'},
    )


@router.delete("/{uid}/frames", response_model=OkOut)
async def purge_frames(
    uid: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> OkOut:
    from ..services import ingest

    device = await load_device(session, uid)
    result = await session.execute(delete(Frame).where(Frame.device_id == device.id))
    count = int(result.rowcount or 0)
    await session.commit()
    await ingest.delete_device_frames(runtime, device)
    await audit.record(
        session,
        action=audit.ACTION_DEVICE_DELETE,
        device=device,
        ip=client_ip(request),
        detail={"scope": "frames", "deleted": count},
    )
    await events.emit(runtime.console_hub, events.EVENT_DEVICE_UPDATED, device_uid=device.uid)
    return OkOut(message=f"已删除 {count} 张画面")


__all__ = ["router"]