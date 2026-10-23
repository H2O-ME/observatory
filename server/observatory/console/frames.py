"""Read-only frame and timeline API for the console.

Frame data belongs to the media layer; these endpoints only query the ``frames``
table and stream files through ``media.storage`` (``Frame.path`` is relative to
``data_dir``).  The DTOs keep the 0.2.10 shape and add ``device_time_ms`` while
retaining ``ts_ms`` as the one-version alias required by C-6.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device, Frame
from . import audit
from .deps import client_ip, require_admin
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["frames"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

MIN_BUCKET_MS = 5_000
MAX_BUCKETS = 900
MAX_SPAN_MS = 31 * 24 * 3600 * 1000
CACHE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


async def _load_device(session: AsyncSession, uid: str) -> Device:
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        raise AppError.gone("设备不存在")
    return device


def _frame_out(frame: Frame) -> dict[str, Any]:
    return {
        "id": frame.id,
        "ts_ms": frame.ts_ms,
        "device_time_ms": frame.device_ts_ms,
        "width": frame.width,
        "height": frame.height,
        "size_bytes": frame.size_bytes,
        "has_thumb": bool(frame.thumb_path),
        "kind": frame.kind,
        "session": frame.session,
    }


async def _media_settings(session: AsyncSession) -> Any:
    try:
        from ..media import settings as media_settings
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体配置未就绪") from exc
    return await media_settings.load(session)


def choose_bucket(from_ms: int, to_ms: int, suggested_ms: int | None = None) -> int:
    span = max(1, to_ms - from_ms)
    bucket = max(MIN_BUCKET_MS, int(suggested_ms or 60_000))
    if span / bucket > MAX_BUCKETS:
        bucket = span // MAX_BUCKETS
        bucket = max(MIN_BUCKET_MS, (bucket // MIN_BUCKET_MS) * MIN_BUCKET_MS)
    return int(bucket)


@router.get("/{uid}/frames/latest")
async def latest_frame(uid: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    device = await _load_device(session, uid)
    frame = (
        await session.execute(
            select(Frame).where(Frame.device_id == device.id).order_by(Frame.ts_ms.desc(), Frame.id.desc()).limit(1)
        )
    ).scalars().first()
    return stamp({"frame": _frame_out(frame) if frame else None})


@router.get("/{uid}/frames")
async def list_frames(
    uid: str,
    from_ms: int = Query(default=0, ge=0),
    to_ms: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await _load_device(session, uid)
    statement = select(Frame).where(Frame.device_id == device.id)
    counter = select(func.count(Frame.id)).where(Frame.device_id == device.id)
    if from_ms:
        statement = statement.where(Frame.ts_ms >= from_ms)
        counter = counter.where(Frame.ts_ms >= from_ms)
    if to_ms:
        statement = statement.where(Frame.ts_ms < to_ms)
        counter = counter.where(Frame.ts_ms < to_ms)
    total = int((await session.execute(counter)).scalar() or 0)
    rows = list(
        (
            await session.execute(
                statement.order_by(Frame.ts_ms.desc(), Frame.id.desc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return stamp(
        {
            "frames": [_frame_out(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/{uid}/timeline")
async def device_timeline(
    uid: str,
    from_ms: int = Query(..., ge=0),
    to_ms: int = Query(..., ge=0),
    bucket_ms: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await _load_device(session, uid)
    if to_ms <= from_ms:
        raise AppError.bad_request("结束时间必须晚于开始时间")
    if to_ms - from_ms > MAX_SPAN_MS:
        raise AppError.bad_request("时间线跨度不能超过 31 天")
    bucket = choose_bucket(from_ms, to_ms, bucket_ms)
    bucket_index = (Frame.ts_ms / bucket).label("bucket_index")
    result = await session.execute(
        select(
            bucket_index,
            func.count(Frame.id).label("frame_count"),
            func.min(Frame.id).label("frame_id"),
            func.min(Frame.ts_ms).label("first_ts"),
            func.min(Frame.session).label("session"),
        )
        .where(Frame.device_id == device.id, Frame.ts_ms >= from_ms, Frame.ts_ms < to_ms)
        .group_by(bucket_index)
        .order_by(bucket_index)
    )
    buckets = [
        {
            "ts_ms": int(row.first_ts or 0),
            "device_time_ms": int(row.first_ts or 0),
            "frame_id": int(row.frame_id) if row.frame_id is not None else None,
            "frame_count": int(row.frame_count),
            "session": int(row.session or 0),
        }
        for row in result.fetchall()
    ]
    return stamp(
        {
            "from_ms": from_ms,
            "to_ms": to_ms,
            "bucket_ms": bucket,
            "buckets": buckets,
            "total_frames": sum(item["frame_count"] for item in buckets),
        }
    )


@router.get("/{uid}/frames/{frame_id}")
async def frame_content(
    uid: str,
    frame_id: int,
    request: Request,
    variant: str = Query(default="thumb", pattern="^(thumb|full)$"),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    device = await _load_device(session, uid)
    frame = await session.get(Frame, frame_id)
    if frame is None or int(frame.device_id) != int(device.id):
        raise AppError.gone("画面不存在")
    relative = frame.thumb_path if variant == "thumb" else frame.path
    if not relative:
        relative = frame.path
    try:
        from ..media import settings as media_settings, storage
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体存储层未就绪") from exc
    settings = await media_settings.load(session)
    try:
        path = storage.absolute(settings, relative)
    except storage.StorageError as exc:
        raise AppError.bad_request(str(exc)) from exc
    if not path.exists():
        raise AppError.gone("画面文件已被清理")
    return FileResponse(
        path,
        media_type=storage.media_type_for(path),
        headers={**CACHE_HEADERS, "ETag": f'"{frame.sha256 or frame.id}"'},
    )


@router.delete("/{uid}/frames")
async def purge_frames(
    uid: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Delete one device's frame rows and local files, observably (C6).

    Media owns storage primitives; this handler only orchestrates the two calls
    and never deletes a device or archive row.
    """

    device = await _load_device(session, uid)
    rows = list((await session.execute(select(Frame).where(Frame.device_id == device.id))).scalars().all())
    try:
        from ..media import settings as media_settings, storage
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体存储层未就绪") from exc
    settings = await media_settings.load(session)
    removed_files = 0
    for frame in rows:
        for relative in (frame.path, frame.thumb_path):
            if not relative:
                continue
            try:
                removed_files += storage.delete_path(storage.absolute(settings, relative))
            except storage.StorageError as exc:
                log.warning("skip invalid frame path uid=%s frame=%s: %s", uid, frame.id, exc)
    for frame in rows:
        await session.delete(frame)
    await session.commit()
    await audit.record(
        session,
        action=audit.ACTION_FILE_OP,
        device=device,
        ip=client_ip(request),
        detail={"scope": "frames", "deleted": len(rows), "files": removed_files},
    )
    return stamp({"ok": True, "message": f"已删除 {len(rows)} 张画面", "deleted": len(rows)})


__all__ = ["choose_bucket", "router"]