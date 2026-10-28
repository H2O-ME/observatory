"""帧摄取：全系统**唯一一条**帧入库路径。

C2 的落地方式：所有帧上传（v3 ``POST /api/v1/agent/frames``）最终都只调
:func:`store_frame`。没有第二个落盘/转码/入库实现，也没有"WS 二进制帧"分支。

本模块负责的四件事：

1. **幂等**：以 ``frame_id`` 去重。先查已有行（命中直接返回、**不重复转码**），
   唯一索引兜住并发；插行失败（``IntegrityError``）时删掉刚落盘的孤儿文件并返回既有行。
2. **落盘**：唯一临时名 + ``os.replace``，文件名带内容摘要，同毫秒两帧不会互相覆盖。
3. **时间语义**：``ts_ms`` 是**服务端接收时间**（本模块取 ``core.clock``），设备上报的
   采集时间只进 ``device_ts_ms``；两者绝不混用。
4. **归档登记**：帧提交后再登记归档；登记失败不回滚已保存的帧（归档是第二副本，
   不能反过来决定首副本的存亡），但失败必须留日志。

设备在线时间（``Device.last_seen_ms``）**不在这里刷新**——那是 presence 的唯一职责，
帧上传顺手改会让在线判定出现第二个写入点。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from observatory.core.clock import now_ms as _now_ms
from observatory.core.errors import AppError
from observatory.core.models import Device, Frame

from . import archive, retention, storage, transcode
from .settings import MediaSettings, load as load_settings, resolve

log = logging.getLogger(__name__)

_SAFE_KIND = re.compile(r"[^A-Za-z0-9_-]")
_MAX_FRAME_ID_LEN = 128

_thumbnailer = transcode.Thumbnailer()
_listeners: list[Callable[[Device, Frame], Any]] = []


# --------------------------------------------------------------------------
# 装配钩子（避免媒体层反向依赖 console/events）
# --------------------------------------------------------------------------
def configure_thumbnailer(thumbnailer: transcode.Thumbnailer) -> None:
    global _thumbnailer
    _thumbnailer = thumbnailer


def add_frame_listener(listener: Callable[[Device, Frame], Any]) -> None:
    """注册"帧已入库"回调（console 用它推实时事件）。"""

    if listener not in _listeners:
        _listeners.append(listener)


def clear_frame_listeners() -> None:
    _listeners.clear()


# --------------------------------------------------------------------------
# 归一辅助
# --------------------------------------------------------------------------
def normalise_frame_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:_MAX_FRAME_ID_LEN]


def normalise_kind(value: Any) -> str:
    cleaned = _SAFE_KIND.sub("", str(value or "screen"))[:32]
    return cleaned or "screen"


async def _resolve_settings(session: AsyncSession, settings: Any) -> MediaSettings:
    if isinstance(settings, MediaSettings):
        return settings
    if settings is None:
        return await load_settings(session)
    return resolve(settings, base=await load_settings(session))


async def _find_existing(session: AsyncSession, device_id: int, frame_id: str) -> Frame | None:
    return (
        await session.execute(
            select(Frame)
            .where(Frame.device_id == int(device_id), Frame.frame_id == frame_id)
            .order_by(Frame.id.asc())
            .limit(1)
        )
    ).scalars().first()


def _remove_files(paths: list[Any]) -> int:
    return sum(storage.delete_path(path) for path in paths)


async def _notify(device: Device, frame: Frame) -> None:
    for listener in tuple(_listeners):
        try:
            result = listener(device, frame)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 事件订阅方失败不能影响已完成的入库
            log.warning("帧事件回调失败 device=%s frame=%s: %s", getattr(device, "uid", "?"), frame.id, exc)


# --------------------------------------------------------------------------
# 唯一摄取入口
# --------------------------------------------------------------------------
async def store_frame(
    session: AsyncSession,
    device: Device,
    data: bytes,
    *,
    frame_id: str | None = None,
    device_ts_ms: int | None = None,
    session_id: int = 0,
    kind: str = "screen",
    settings: Any = None,
    now_ms: int | None = None,
) -> Frame | None:
    """归一、落盘、入库一帧；返回入库行，丢弃/失败时返回 ``None``。

    ``now_ms`` 仅用于测试注入；生产路径必须留空，让 ``ts_ms`` 使用服务端时钟。
    """

    media_settings = await _resolve_settings(session, settings)
    # 先快照主键与 uid：commit/rollback 会令 ORM 对象过期，之后在异步上下文里读属性
    # 会触发"隐式懒加载"（MissingGreenlet），并发重复上传的异常分支最容易踩到。
    device_id = int(device.id)
    device_uid = str(device.uid)
    if not data:
        log.debug("丢弃空帧 device=%s", device_uid)
        return None
    if len(data) > media_settings.max_frame_bytes:
        log.warning(
            "丢弃超限帧 device=%s bytes=%d limit=%d",
            device_uid,
            len(data),
            media_settings.max_frame_bytes,
        )
        return None

    clean_id = normalise_frame_id(frame_id)
    clean_kind = normalise_kind(kind)
    if clean_id:
        existing = await _find_existing(session, device_id, clean_id)
        if existing is not None:
            # 幂等命中：不再解码、不再编码、不再落盘（重复上传最贵的成本就在这里）。
            log.debug("重复帧 frame_id=%s 已存在（id=%s），跳过转码", clean_id, existing.id)
            return existing

    moment = _now_ms() if now_ms is None else int(now_ms)

    try:
        encoded, width, height = await _thumbnailer.normalize(
            data, media_settings.max_width, media_settings.webp_quality
        )
        thumb, _, _ = await _thumbnailer.thumbnail(
            encoded, media_settings.thumb_width, media_settings.thumb_quality
        )
    except transcode.ImageError as exc:
        log.warning("丢弃无法解码的帧 device=%s: %s", device_uid, exc)
        return None

    digest = storage.sha256_of(encoded)
    relative = storage.relative_frame_path(
        device_uid, moment, ext="webp", kind=clean_kind, digest=digest
    )
    relative_thumb = storage.relative_thumb_path(
        device_uid, moment, ext="webp", kind=clean_kind, digest=digest
    )
    try:
        frame_path = storage.absolute(media_settings, relative)
        thumb_path = storage.absolute(media_settings, relative_thumb)
    except storage.StorageError as exc:
        log.error("帧路径非法 device=%s: %s", device_uid, exc)
        return None

    try:
        await asyncio.to_thread(storage.write_atomic, frame_path, encoded)
        await asyncio.to_thread(storage.write_atomic, thumb_path, thumb)
    except OSError as exc:
        # F24: a full disk / IO hiccup is transient — answer 5xx (the agent
        # retries with backoff) instead of 400, which made the agent discard
        # the frame permanently as if it were undecodable.
        log.error("帧落盘失败 device=%s: %s", device_uid, exc)
        await asyncio.to_thread(_remove_files, [frame_path, thumb_path])
        raise AppError.internal("帧落盘失败（存储暂不可用），请稍后重试") from exc

    frame = Frame(
        device_id=device_id,
        ts_ms=moment,
        device_ts_ms=None if device_ts_ms is None else int(device_ts_ms),
        session=int(session_id or 0),
        kind=clean_kind,
        frame_id=clean_id,
        path=relative,
        thumb_path=relative_thumb,
        width=int(width),
        height=int(height),
        size_bytes=len(encoded),
        sha256=digest,
        archived_at_ms=None,
    )
    session.add(frame)
    try:
        await session.commit()
    except IntegrityError as exc:
        # 并发重复上传：唯一索引挡在这里。若既有行与本次写的文件**不同路径**才清理；
        # 同毫秒同内容的两次请求可能算出同一路径，此时删文件会误删赢家的数据。
        await session.rollback()
        if clean_id:
            existing = await _find_existing(session, device_id, clean_id)
            if existing is not None:
                if existing.path != relative and existing.thumb_path != relative_thumb:
                    await asyncio.to_thread(_remove_files, [frame_path, thumb_path])
                log.info("并发重复帧 frame_id=%s 命中既有行 id=%s", clean_id, existing.id)
                return existing
        await asyncio.to_thread(_remove_files, [frame_path, thumb_path])
        log.error("帧入库唯一约束冲突且找不到既有行 device=%s: %s", device_uid, exc)
        return None
    except SQLAlchemyError as exc:
        await session.rollback()
        await asyncio.to_thread(_remove_files, [frame_path, thumb_path])
        # F24: same as the OSError path — transient DB trouble must be retryable.
        log.error("帧入库失败 device=%s: %s", device_uid, exc)
        raise AppError.internal("帧入库失败（数据库暂不可用），请稍后重试") from exc

    await session.refresh(frame)
    await _notify(device, frame)

    try:
        await archive.schedule(session=session, device=device, frame=frame, settings=media_settings)
    except (SQLAlchemyError, RuntimeError, ValueError) as exc:
        await session.rollback()
        # 帧已提交，归档只是第二副本：登记失败必须可观测，但不能让上传失败。
        log.warning("归档登记失败（帧已保存）frame_id=%s: %s", frame.id, exc)
    return frame


# --------------------------------------------------------------------------
# 删除设备
# --------------------------------------------------------------------------
def _remove_device_dirs(settings: MediaSettings, uid: str) -> tuple[int, int]:
    files = 0
    freed = 0
    for root in (
        storage.device_frame_dir(settings, uid),
        storage.device_thumb_dir(settings, uid),
    ):
        removed, size = storage.remove_tree(root)
        files += removed
        freed += size
    return files, freed


async def delete_device_data(
    session: AsyncSession,
    device: Device,
    settings: Any = None,
    *,
    purge_remote: bool = True,
) -> dict[str, int | str]:
    """删除一台设备的全部媒体数据：本地树 → 远端归档 → 数据库行。

    顺序刻意如此：先删本地（不可逆、无远端依赖），再删远端（失败可观测、状态行保留），
    最后删数据库行（一旦删行就无法再定位远端路径）。

    远端删除失败时抛 :class:`archive.ArchiveError`（S3）：数据库行与归档账目都
    保持原样，由调用方（``console.device_purge``）标记 ``purge_pending_at_ms``
    并在后台重试。调用方因此绝不能把"异常"当成"清理完成"。

    P1（2026-10 业务审查）：``purge_remote=False`` 此前是死参数——接收、写
    审计、发事件，但这里无条件本地+远端全清。现在 False 时跳过远端删除，
    且 ``frame_archives`` 状态行**保留**（它们是远端副本仅存的账目，删了
    就再也无法定位/清理远端）；Frame 行照删（本地帧已随目录删除）。
    """

    media_settings = await _resolve_settings(session, settings)
    files, freed = await asyncio.to_thread(_remove_device_dirs, media_settings, str(device.uid))

    remote_removed = 0
    #: 保留该键只为兼容调用方（console/devices.py 读它来决定是否告警）；
    #: S3 起远端失败不再降级成这个字段，而是抛 ArchiveError，走到这里时
    #: 远端删除必然已经成功（或本来就没有远端任务），所以恒为空串。
    archive_error = ""
    frames = 0
    if not purge_remote:
        from ..core.models import Frame as FrameRow

        result = await session.execute(delete(FrameRow).where(FrameRow.device_id == int(device.id)))
        frames = int(result.rowcount or 0)
        await session.commit()
    else:
        # S3（2026-10 独立审计）：这里此前把远端删除失败吞成 archive_error 后继续
        # 删帧行/归档账目，调用方随后删掉设备记录并报告成功——远端副本还在、账目
        # 却没了，既无法重试也无法定位。现在让 :class:`archive.ArchiveError`
        # 直穿到 console.device_purge（它的宽捕获会标记 purge_pending_at_ms 并返回
        # ok=False），Frame 与 FrameArchive 行都保留给下一拍重试。
        remote_removed = await archive.purge_device(session, device, settings=media_settings)
        frames = await retention.purge_device(session, int(device.id))
    log.info(
        "设备媒体数据已删除 device=%s 文件=%d 释放=%d 帧行=%d 远端条目=%d",
        device.uid,
        files,
        freed,
        frames,
        remote_removed,
    )
    return {
        "frames": frames,
        "local_files": files,
        "local_bytes": freed,
        "remote_removed": remote_removed,
        "archive_error": archive_error,
    }


__all__ = [
    "add_frame_listener",
    "clear_frame_listeners",
    "configure_thumbnailer",
    "delete_device_data",
    "normalise_frame_id",
    "normalise_kind",
    "store_frame",
]