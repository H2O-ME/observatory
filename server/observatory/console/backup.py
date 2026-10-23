"""Backup/archive console API (frontend contract C-11/C-12).

The media layer owns the archive state machine and every remote operation; this
module is a thin HTTP/audit wrapper:

* list   -- ``GET /backup`` and ``GET /backup/entries`` (``archive.list_page``)
* detail -- ``GET /backup/entries/{entry_id}``
* fetch  -- ``GET /backup/entries/{entry_id}/file`` when a local copy exists,
            ``POST /backup/entries/{entry_id}/retrieve`` otherwise (``archive.restore``)
* delete -- ``DELETE /backup/entries/{entry_id}`` (``archive.delete_entry``,
            remote first; the status only becomes 「已删除」 after it succeeds)

Local frame paths are relative to ``settings.data_dir`` (``frames/<uid>/...``),
so files are resolved with media's ``storage.absolute`` instead of gluing
``frames_dir`` to the stored path.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device, Frame, FrameArchive
from . import audit
from .deps import client_ip, require_admin
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(tags=["backup"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

MAX_PAGE = 200


async def _media_archive() -> Any:
    try:
        from ..media import archive as media_archive
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体归档层未就绪") from exc
    return media_archive


async def _media_settings(session: AsyncSession) -> Any:
    try:
        from ..media import settings as media_settings
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体配置未就绪") from exc
    return await media_settings.load(session)


def _map_archive_error(exc: Exception) -> AppError:
    message = str(exc) or "归档远端操作失败"
    if "404" in message or "不存在" in message:
        return AppError.gone(message)
    if "未启用" in message or "未配置" in message:
        return AppError.unsupported(message)
    return AppError.internal(message, retryable=True)


async def _load_entry(session: AsyncSession, entry_id: int) -> FrameArchive:
    row = await session.get(FrameArchive, entry_id)
    if row is None:
        raise AppError.gone("归档记录不存在")
    return row


async def _local_frame(session: AsyncSession, row: FrameArchive) -> Frame | None:
    if not row.frame_id:
        return None
    return (
        await session.execute(
            select(Frame)
            .where(Frame.device_id == int(row.device_id), Frame.frame_id == row.frame_id)
            .order_by(Frame.id.asc())
            .limit(1)
        )
    ).scalars().first()


async def _local_frame_for(session: AsyncSession, item: dict[str, Any]) -> Frame | None:
    frame_id = item.get("frame_id")
    device_id = item.get("device_id")
    if not frame_id or not device_id:
        return None
    return (
        await session.execute(
            select(Frame)
            .where(Frame.device_id == int(device_id), Frame.frame_id == str(frame_id))
            .order_by(Frame.id.asc())
            .limit(1)
        )
    ).scalars().first()


async def _frame_path(session: AsyncSession, frame: Frame) -> Any:
    try:
        from ..media import settings as media_settings, storage
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体存储层未就绪") from exc
    media_settings_value = await media_settings.load(session)
    return storage.absolute(media_settings_value, frame.path)


@router.get("/backup")
async def backup_overview(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    media_archive = await _media_archive()
    cfg = await _media_settings(session)
    summary = await media_archive.summary(session)
    failures = await media_archive.recent_failures(session, limit=10)
    return stamp(
        {
            "configured": bool(getattr(cfg, "archive_ready", False)),
            "archive_enabled": bool(getattr(cfg, "archive_enabled", False)),
            "repo": getattr(cfg, "archive_repo", ""),
            "branch": getattr(cfg, "archive_branch", ""),
            "summary": summary,
            "recent_failures": failures,
            "statuses": list(media_archive.STATUSES),
        }
    )


@router.get("/backup/entries")
async def list_backup_entries(
    session: AsyncSession = Depends(get_session),
    status: str = Query(default=""),
    device_uid: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    media_archive = await _media_archive()
    if status and status not in media_archive.STATUSES:
        raise AppError.bad_request(f"未知归档状态: {status}")
    page = await media_archive.list_page(
        session,
        status=status or None,
        device_uid=device_uid or None,
        limit=limit,
        offset=offset,
    )
    items = []
    for item in page.get("items", []):
        entry = dict(item)
        frame = await _local_frame_for(session, item)
        entry["local_available"] = frame is not None
        entry["device_name"] = ""
        entry["server_time_ms"] = now_ms()
        items.append(entry)
    return stamp(
        {
            "entries": items,
            "total": int(page.get("total") or 0),
            "limit": int(page.get("limit") or limit),
            "offset": int(page.get("offset") or offset),
            "statuses": list(media_archive.STATUSES),
        }
    )


@router.get("/backup/entries/{entry_id}")
async def backup_entry_detail(entry_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _load_entry(session, entry_id)
    device = await session.get(Device, int(row.device_id))
    frame = await _local_frame(session, row)
    payload = {
        "id": row.id,
        "device_id": row.device_id,
        "device_uid": device.uid if device else "",
        "device_name": device.display_name if device else "",
        "frame_id": row.frame_id,
        "day": row.day,
        "remote_path": row.remote_path,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
        "local_available": frame is not None,
        "created_at_ms": row.created_at_ms,
        "uploaded_at_ms": row.uploaded_at_ms,
        "server_time_ms": now_ms(),
    }
    if frame is not None:
        payload["frame"] = {
            "id": frame.id,
            "ts_ms": frame.ts_ms,
            "device_time_ms": frame.device_ts_ms,
            "width": frame.width,
            "height": frame.height,
            "size_bytes": frame.size_bytes,
            "sha256": frame.sha256,
        }
    return stamp({"entry": payload})


@router.get("/backup/entries/{entry_id}/file")
async def backup_entry_file(
    entry_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    row = await _load_entry(session, entry_id)
    frame = await _local_frame(session, row)
    if frame is None:
        raise AppError.unsupported("该归档只有远端副本，请先调用取回接口")
    path = await _frame_path(session, frame)
    if not path.exists():
        raise AppError.gone("本地归档文件已丢失")
    await audit.record(
        session,
        action=audit.ACTION_BACKUP_RETRIEVE,
        device=await session.get(Device, int(row.device_id)),
        ip=client_ip(request),
        detail={"entry_id": entry_id, "mode": "local"},
    )
    return FileResponse(
        path,
        media_type="image/webp",
        filename=f"{row.frame_id or row.id}.webp",
        headers={"X-Sha256": row.sha256, "Cache-Control": "no-store"},
    )


@router.post("/backup/entries/{entry_id}/retrieve")
async def retrieve_backup_entry(
    entry_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load_entry(session, entry_id)
    local = await _local_frame(session, row)
    if local is not None:
        raise AppError.bad_request("本地副本仍然存在，直接下载即可")
    media_archive = await _media_archive()
    try:
        frame = await media_archive.restore(session, row)
    except Exception as exc:  # noqa: BLE001 - mapped to the uniform error body
        error_cls = getattr(media_archive, "ArchiveError", None)
        if error_cls is not None and isinstance(exc, error_cls):
            raise _map_archive_error(exc) from exc
        raise
    await audit.record(
        session,
        action=audit.ACTION_BACKUP_RETRIEVE,
        device=await session.get(Device, int(row.device_id)),
        ip=client_ip(request),
        detail={"entry_id": entry_id, "mode": "remote"},
    )
    return stamp(
        {
            "ok": True,
            "message": "已取回归档",
            "frame_id": frame.id,
            "device_time_ms": frame.device_ts_ms,
            "ts_ms": frame.ts_ms,
        }
    )


@router.delete("/backup/entries/{entry_id}")
async def delete_backup_entry(
    entry_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load_entry(session, entry_id)
    media_archive = await _media_archive()
    try:
        removed = await media_archive.delete_entry(session, row)
    except Exception as exc:  # noqa: BLE001 - mapped to the uniform error body
        error_cls = getattr(media_archive, "ArchiveError", None)
        if error_cls is not None and isinstance(exc, error_cls):
            raise _map_archive_error(exc) from exc
        raise
    if not removed:
        raise AppError.internal("远端归档删除未确认，未修改状态（A14）")
    await audit.record(
        session,
        action=audit.ACTION_BACKUP_DELETE,
        device=await session.get(Device, int(row.device_id)),
        ip=client_ip(request),
        detail={"entry_id": entry_id, "remote_path": row.remote_path},
    )
    return stamp({"ok": True, "message": "远端归档已删除，本地帧不受影响", "entry_id": entry_id})


__all__ = ["router"]