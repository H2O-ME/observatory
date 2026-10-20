"""Frame ingest: one code path for HTTP upload and the agent WebSocket."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Device, Frame, now_ms
from . import events, storage
from .runtime import Runtime

log = logging.getLogger(__name__)


async def store_frame(
    runtime: Runtime,
    session: AsyncSession,
    device: Device,
    data: bytes,
    *,
    ts_ms: int,
    width: int = 0,
    height: int = 0,
    session_id: int = 0,
    kind: str = "screen",
    settings_values: dict | None = None,
    emit: bool = True,
) -> Frame | None:
    """Normalise, persist (original + thumbnail) and register one frame."""

    if not data:
        return None
    if len(data) > runtime.settings.max_frame_bytes:
        log.warning("dropping oversized frame from %s: %d bytes", device.uid, len(data))
        return None

    values = settings_values or {}
    max_width = int(values.get("max_width", 1600))
    quality = int(values.get("jpeg_quality", 72))
    thumb_width = int(values.get("thumb_width", 320))

    try:
        encoded, width, height = await runtime.thumbnailer.normalize(data, max_width, quality)
        thumb_bytes, _, _ = await runtime.thumbnailer.thumbnail(encoded, thumb_width)
    except Exception as exc:  # noqa: BLE001 - a corrupt frame must not kill the request
        log.warning("frame decode failed for %s: %s", device.uid, exc)
        return None

    moment = ts_ms or now_ms()
    rel_path = storage.relative_frame_path(device.uid, moment, ext="webp", kind=kind)
    rel_thumb = storage.relative_thumb_path(device.uid, moment, ext="webp", kind=kind)

    try:
        frame_path = storage.absolute(runtime.settings, rel_path)
        thumb_path = storage.absolute(runtime.settings, rel_thumb)
        await asyncio.to_thread(storage.write_atomic, frame_path, encoded)
        await asyncio.to_thread(storage.write_atomic, thumb_path, thumb_bytes)
    except OSError as exc:
        log.error("frame write failed for %s: %s", device.uid, exc)
        return None

    frame = Frame(
        device_id=device.id,
        ts_ms=moment,
        session=int(session_id or 0),
        kind=kind,
        path=rel_path,
        thumb_path=rel_thumb,
        width=width,
        height=height,
        size_bytes=len(encoded),
        sha256=storage.sha256_of(encoded),
    )
    session.add(frame)
    device.last_seen_ms = now_ms()
    await session.commit()
    await session.refresh(frame)

    if emit:
        await events.emit(
            runtime.console_hub,
            events.EVENT_FRAME_ADDED,
            device_uid=device.uid,
            data={
                "frame": {
                    "id": frame.id,
                    "ts_ms": frame.ts_ms,
                    "width": frame.width,
                    "height": frame.height,
                    "kind": frame.kind,
                }
            },
        )

    archive = getattr(runtime, "archive", None)
    if archive is not None:
        archive.schedule(
            enabled=bool(values.get("archive_enabled")),
            repo=str(values.get("archive_repo") or ""),
            token=str(values.get("archive_token") or ""),
            branch=str(values.get("archive_branch") or "main"),
            prefix=str(values.get("archive_path_prefix") or "frames"),
            device_uid=device.uid,
            frame=frame,
            data=encoded,
        )
    return frame


async def delete_device_frames(runtime: Runtime, device: Device) -> None:
    """Remove a device's frame and thumbnail trees from disk."""

    for root in (storage.device_frame_dir(runtime.settings, device.uid), storage.device_thumb_dir(runtime.settings, device.uid)):
        await asyncio.to_thread(_remove_tree, root)


def _remove_tree(root) -> None:
    import shutil

    try:
        shutil.rmtree(root, ignore_errors=True)
    except OSError:
        pass


__all__ = ["delete_device_frames", "store_frame"]