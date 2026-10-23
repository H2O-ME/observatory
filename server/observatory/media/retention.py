"""保留策略：全系统**唯一**删除画面数据的地方。

0.2.10 的致命口径错误：配额 ``used`` 按磁盘目录大小算，删除却只删数据库行并按行
数的"释放字节"回减。一旦有孤儿文件（崩溃在"删行"与"删文件"之间），删光全部历史
行也永远达不到配额目标，清扫循环空转。这里改成**同源口径**：

* ``used`` = ``SUM(frames.size_bytes)``（与删除动作针对同一批行）；
* 删除顺序先删行、提交、再删文件；崩在中间只留下孤儿，由孤儿扫描回收；
* 孤儿扫描反方向兜底：磁盘上没有数据库引用的文件按 mtime 宽限期回收，
  数据库引用但磁盘缺失的行直接删行（避免"幽灵占用"）。

另外两条 0.3.0 的新语义：

* **归档感知**：归档启用时，未上传成功的帧在 "保留天数 + 宽限天数" 之前不删，
  宽限到期后照删并告警——既保证"上传成功才删本地"，又不会让归档长期失败把
  磁盘占满。
* **时间注入**：公开函数都可传 ``now_ms``，纯决策函数（:func:`archive_gate_open`、
  :func:`select_quota_victims`、:func:`split_orphans`）不碰时钟，可直接单测。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observatory.core.clock import now_ms as _now_ms
from observatory.core.models import AuditEntry, Command, Frame, FrameArchive

from . import archive, storage
from .settings import DAY_MS, MediaSettings, load as load_settings, resolve

log = logging.getLogger(__name__)

PURGE_BATCH = 500
DANGLING_CHECK_LIMIT = 200
ARCHIVE_ROW_RETENTION_DAYS = 30

# 命令终结态（与 0.2.10 的 commands 状态集合一致，只有这些能被保留策略回收）
_FINISHED_COMMAND_STATUSES = ("ok", "error", "timeout", "cancelled")


@dataclass(frozen=True, slots=True)
class RetentionStats:
    frames: int = 0
    frames_bytes: int = 0
    dangling: int = 0
    quota_reclaimed: int = 0
    orphans: int = 0
    orphans_bytes: int = 0
    audit: int = 0
    commands: int = 0
    archives: int = 0
    archive_enqueued: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "frames": self.frames,
            "frames_bytes": self.frames_bytes,
            "dangling": self.dangling,
            "quota_reclaimed": self.quota_reclaimed,
            "orphans": self.orphans,
            "orphans_bytes": self.orphans_bytes,
            "audit": self.audit,
            "commands": self.commands,
            "archives": self.archives,
            "archive_enqueued": self.archive_enqueued,
        }


# --------------------------------------------------------------------------
# 纯决策函数（无 IO、无隐式时钟）
# --------------------------------------------------------------------------
def cutoff_ms(retention_days: int, now_ms: int) -> int:
    return int(now_ms) - max(0, int(retention_days)) * DAY_MS


def archive_gate_open(
    archive_status: str | None,
    frame_ts_ms: int,
    settings: MediaSettings,
    *,
    now_ms: int,
) -> bool:
    """本地文件是否允许删除。

    * 归档未启用 / 关闭门控 → 允许（与 0.2.10 行为一致）；
    * 已上传 / 已删除 → 允许；
    * 待上传、失败或根本没有归档行 → 等 "保留天数 + 宽限天数" 之后才允许。
    """

    if not settings.archive_enabled or not settings.archive_gate_retention:
        return True
    if archive_status in (archive.STATUS_UPLOADED, archive.STATUS_DELETED):
        return True
    grace_total = (int(settings.frame_retention_days) + int(settings.archive_gate_grace_days)) * DAY_MS
    return int(now_ms) - int(frame_ts_ms) >= grace_total


def select_quota_victims(
    pairs: Sequence[tuple[Any, str | None]],
    *,
    used_bytes: int,
    target_bytes: int,
    settings: MediaSettings,
    now_ms: int,
) -> list[Any]:
    """按"最旧优先"挑选要回收的行，直到预计用量落到目标以下。

    返回帧行对象列表；被归档门控拦下的帧跳过（但不会因此无限循环——调用方在
    "挑不出任何行"时结束本轮）。
    """

    if used_bytes <= target_bytes:
        return []
    chosen: list[Any] = []
    projected = int(used_bytes)
    for frame, status in pairs:
        if projected <= target_bytes:
            break
        if not archive_gate_open(status, int(frame.ts_ms), settings, now_ms=now_ms):
            continue
        chosen.append(frame)
        projected -= int(frame.size_bytes or 0)
    return chosen


def orphan_is_expired(mtime_ms: int, *, now_ms: int, grace_ms: int) -> bool:
    """宽限期内的文件可能正在"写完文件、还没来得及插行"，必须放过。"""

    return int(now_ms) - int(mtime_ms) >= max(0, int(grace_ms))


def split_orphans(
    disk_files: Iterable[storage.MediaFile],
    referenced: set[str],
    *,
    now_ms: int,
    grace_ms: int,
) -> list[storage.MediaFile]:
    return [
        item
        for item in disk_files
        if item.relative not in referenced and orphan_is_expired(item.mtime_ms, now_ms=now_ms, grace_ms=grace_ms)
    ]


def frame_storage_paths(settings: MediaSettings, frames: Iterable[Any]) -> list[Path]:
    """把帧行折算成待删文件路径；越界路径跳过并告警。"""

    paths: list[Path] = []
    for frame in frames:
        for relative in (getattr(frame, "path", ""), getattr(frame, "thumb_path", "")):
            if not relative:
                continue
            try:
                paths.append(storage.absolute(settings, str(relative)))
            except storage.StorageError as exc:
                log.warning("跳过越界帧路径 %r: %s", relative, exc)
    return paths


def _unlink_all(paths: Sequence[Path]) -> int:
    return sum(storage.delete_path(path) for path in paths)


def _archive_remote_paths(settings: MediaSettings, frames: Iterable[Any]) -> list[str]:
    """帧被本地回收后，其归档状态行必须一起收敛，否则 sweep 会永远重试已不存在的文件。"""

    if not settings.archive_ready:
        return []
    paths: list[str] = []
    for frame in frames:
        try:
            paths.append(archive.remote_path_for(settings.archive_path_prefix, str(frame.path)))
        except storage.StorageError as exc:
            log.warning("计算归档路径失败，跳过状态收敛 frame=%s: %s", getattr(frame, "id", "?"), exc)
    return paths


async def _converge_archives_on_purge(
    session: AsyncSession, settings: MediaSettings, frames: Sequence[Any]
) -> int:
    """本地帧被回收时收敛其归档状态行。

    * 已上传的行保持"已上传"——远端副本还在，备份页仍可取回（C-12）；
    * 待上传/失败的行无法再上传了，置"失败"并把 attempts 推到上限，停止无意义重试。
    """

    if not settings.archive_ready:
        return 0
    remote_paths = _archive_remote_paths(settings, frames)
    if not remote_paths:
        return 0
    result = await session.execute(
        update(FrameArchive)
        .where(
            FrameArchive.remote_path.in_(remote_paths),
            FrameArchive.status.in_((archive.STATUS_PENDING, archive.STATUS_FAILED)),
        )
        .values(
            status=archive.STATUS_FAILED,
            attempts=max(1, int(settings.archive_max_attempts)),
            error="本地帧已被保留策略回收，归档中止",
        )
    )
    return int(result.rowcount or 0)


def _missing_files(settings: MediaSettings, rows: Sequence[Any]) -> list[Any]:
    missing: list[Any] = []
    for frame in rows:
        present = False
        for relative in (getattr(frame, "path", ""), getattr(frame, "thumb_path", "")):
            if not relative:
                continue
            try:
                if storage.absolute(settings, str(relative)).exists():
                    present = True
                    break
            except storage.StorageError as exc:
                log.warning("校验帧文件存在性时跳过越界路径 %r: %s", relative, exc)
        if not present:
            missing.append(frame)
    return missing


# --------------------------------------------------------------------------
# 周期任务
# --------------------------------------------------------------------------
async def run_once(
    session: AsyncSession,
    *,
    settings: MediaSettings | Any | None = None,
    now_ms: int | None = None,
    archive_service: archive.ArchiveService | None = None,
) -> dict[str, int]:
    """跑一轮保留：按天回收 → 配额回收 → 孤儿扫描 → 归档重试 → 审计/命令回收。"""

    if isinstance(settings, MediaSettings):
        media_settings: MediaSettings = settings
    elif settings is None:
        media_settings = await load_settings(session)
    else:
        media_settings = resolve(settings, base=await load_settings(session))
    moment = _now_ms() if now_ms is None else int(now_ms)
    stats = RetentionStats()

    frame_cutoff = cutoff_ms(media_settings.frame_retention_days, moment)
    removed, freed = await _purge_by_age(session, media_settings, frame_cutoff, moment)
    stats = RetentionStats(
        frames=removed,
        frames_bytes=freed,
        dangling=(await _purge_dangling(session, media_settings)),
        orphans=0,
        orphans_bytes=0,
        audit=await _purge_audit(session, media_settings, moment),
        commands=await _purge_commands(session, media_settings, moment),
        archives=await _purge_archive_rows(session, media_settings, moment),
    )

    reclaimed = await _enforce_quota(session, media_settings, moment)
    orphans, orphan_bytes = await _reclaim_orphans(session, media_settings, moment)
    service = archive_service or archive.get_service()
    enqueued = await service.sweep(session, settings=media_settings, limit=64)

    result = RetentionStats(
        frames=stats.frames,
        frames_bytes=stats.frames_bytes,
        dangling=stats.dangling,
        quota_reclaimed=reclaimed,
        orphans=orphans,
        orphans_bytes=orphan_bytes,
        audit=stats.audit,
        commands=stats.commands,
        archives=stats.archives,
        archive_enqueued=enqueued,
    ).as_dict()
    if any(result.values()):
        log.info("保留批次完成: %s", result)
    return result


async def _purge_by_age(
    session: AsyncSession, settings: MediaSettings, cutoff: int, now_ms: int
) -> tuple[int, int]:
    removed = 0
    freed = 0
    while True:
        rows = (
            await session.execute(
                select(Frame, FrameArchive.status)
                .outerjoin(
                    FrameArchive,
                    and_(
                        FrameArchive.device_id == Frame.device_id,
                        FrameArchive.frame_id == Frame.frame_id,
                    ),
                )
                .where(Frame.ts_ms < cutoff)
                .order_by(Frame.ts_ms.asc())
                .limit(PURGE_BATCH)
            )
        ).all()
        if not rows:
            break
        victims = [
            frame
            for frame, status in rows
            if archive_gate_open(status, int(frame.ts_ms), settings, now_ms=now_ms)
        ]
        if not victims:
            # 最旧的都被归档门控拦下 → 更新的同样在宽限期内，本轮到此为止。
            log.info("保留：%d 帧因归档未完成暂缓删除（宽限 %d 天）", len(rows), settings.archive_gate_grace_days)
            break
        paths = frame_storage_paths(settings, victims)
        for frame in victims:
            await session.delete(frame)
        await _converge_archives_on_purge(session, settings, victims)
        await session.commit()
        freed += await asyncio.to_thread(_unlink_all, paths)
        removed += len(victims)
        if len(rows) < PURGE_BATCH:
            break
    return removed, freed


async def _enforce_quota(session: AsyncSession, settings: MediaSettings, now_ms: int) -> int:
    target = settings.quota_target_bytes
    if target <= 0:
        return 0
    used = int((await session.execute(select(func.coalesce(func.sum(Frame.size_bytes), 0)))).scalar() or 0)
    if used <= target:
        return 0

    reclaimed = 0
    while used > target:
        rows = (
            await session.execute(
                select(Frame, FrameArchive.status)
                .outerjoin(
                    FrameArchive,
                    and_(
                        FrameArchive.device_id == Frame.device_id,
                        FrameArchive.frame_id == Frame.frame_id,
                    ),
                )
                .order_by(Frame.ts_ms.asc())
                .limit(PURGE_BATCH)
            )
        ).all()
        if not rows:
            break
        victims = select_quota_victims(
            rows, used_bytes=used, target_bytes=target, settings=settings, now_ms=now_ms
        )
        if not victims:
            log.warning(
                "配额回收停滞：已用 %d 字节 > 目标 %d 字节，但剩余帧均被归档门控拦下", used, target
            )
            break
        paths = frame_storage_paths(settings, victims)
        freed = sum(int(frame.size_bytes or 0) for frame in victims)
        for frame in victims:
            await session.delete(frame)
        await _converge_archives_on_purge(session, settings, victims)
        await session.commit()
        await asyncio.to_thread(_unlink_all, paths)
        reclaimed += freed
        used = max(0, used - freed)
        if len(rows) < PURGE_BATCH:
            break
    return reclaimed


async def _purge_dangling(session: AsyncSession, settings: MediaSettings) -> int:
    """处理"行在、文件没了"的幽灵占用；只扫最旧的一小批，成本可控。"""

    rows = (await session.execute(select(Frame).order_by(Frame.ts_ms.asc()).limit(DANGLING_CHECK_LIMIT))).scalars().all()
    if not rows:
        return 0
    missing = await asyncio.to_thread(_missing_files, settings, rows)
    if not missing:
        return 0
    for frame in missing:
        await session.delete(frame)
    await session.commit()
    log.info("保留：清理 %d 条指向缺失文件的帧行", len(missing))
    return len(missing)


async def _reclaim_orphans(session: AsyncSession, settings: MediaSettings, now_ms: int) -> tuple[int, int]:
    """磁盘有、数据库无引用的文件；只回收超过宽限期的。"""

    referenced: set[str] = set()
    for path, thumb in (await session.execute(select(Frame.path, Frame.thumb_path))).all():
        referenced |= storage.reconcile_paths((path, thumb))
    disk_files = await asyncio.to_thread(storage.scan_media_files, settings)
    orphans = split_orphans(
        disk_files, referenced, now_ms=now_ms, grace_ms=settings.orphan_grace_ms
    )
    if not orphans:
        return 0, 0
    freed = await asyncio.to_thread(_unlink_paths, settings, orphans)
    log.info("保留：回收孤儿文件 %d 个，释放 %d 字节", len(orphans), freed)
    return len(orphans), freed


def _unlink_paths(settings: MediaSettings, orphans: Iterable[storage.MediaFile]) -> int:
    freed = 0
    for item in orphans:
        try:
            freed += storage.delete_path(storage.absolute(settings, item.relative))
        except storage.StorageError as exc:
            log.warning("跳过越界孤儿文件 %r: %s", item.relative, exc)
    return freed


async def _purge_audit(session: AsyncSession, settings: MediaSettings, now_ms: int) -> int:
    cutoff = cutoff_ms(settings.audit_retention_days, now_ms)
    result = await session.execute(delete(AuditEntry).where(AuditEntry.ts_ms < cutoff))
    await session.commit()
    return int(result.rowcount or 0)


async def _purge_commands(session: AsyncSession, settings: MediaSettings, now_ms: int) -> int:
    cutoff = cutoff_ms(settings.command_retention_days, now_ms)
    result = await session.execute(
        delete(Command).where(
            Command.created_at_ms < cutoff,
            Command.status.in_(_FINISHED_COMMAND_STATUSES),
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def _purge_archive_rows(session: AsyncSession, settings: MediaSettings, now_ms: int) -> int:
    """已删除的归档状态行保留 30 天后清理，避免状态表无限增长。"""

    cutoff = cutoff_ms(ARCHIVE_ROW_RETENTION_DAYS, now_ms)
    result = await session.execute(
        delete(FrameArchive).where(
            FrameArchive.status == archive.STATUS_DELETED,
            FrameArchive.created_at_ms < cutoff,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def purge_device(session: AsyncSession, device_id: int) -> int:
    """删设备时清理该设备的帧行与归档状态行（文件由 ingest.delete_device_data 删）。"""

    deleted = await session.execute(delete(Frame).where(Frame.device_id == int(device_id)))
    await session.execute(delete(FrameArchive).where(FrameArchive.device_id == int(device_id)))
    await session.commit()
    return int(deleted.rowcount or 0)


__all__ = [
    "ARCHIVE_ROW_RETENTION_DAYS",
    "DANGLING_CHECK_LIMIT",
    "PURGE_BATCH",
    "RetentionStats",
    "archive_gate_open",
    "cutoff_ms",
    "frame_storage_paths",
    "orphan_is_expired",
    "purge_device",
    "run_once",
    "select_quota_victims",
    "split_orphans",
]