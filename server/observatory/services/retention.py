"""Retention: the only place that deletes data.

Invariant (the previous code violated it): **delete rows and commit first, then
unlink files.** A crash between the two leaves orphaned files, which the quota
sweep reclaims. The inverse order leaves database rows pointing at files that no
longer exist, which is what produced 404/410 responses in the old timeline viewer.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEntry, Command, Frame, now_ms
from . import storage
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

PURGE_BATCH = 500


class RetentionService:
    def __init__(self, settings, store: SettingsStore) -> None:
        self.settings = settings
        self.store = store

    async def run_once(self, session: AsyncSession) -> dict[str, int]:
        values = await self.store.load(session)
        stats: dict[str, int] = {}

        frame_cutoff = now_ms() - int(values["frame_retention_days"]) * 86_400_000
        stats["frames"] = await self._purge_frames(session, frame_cutoff)

        audit_cutoff = now_ms() - int(values["audit_retention_days"]) * 86_400_000
        result = await session.execute(delete(AuditEntry).where(AuditEntry.ts_ms < audit_cutoff))
        stats["audit"] = int(result.rowcount or 0)

        command_cutoff = now_ms() - int(values["command_retention_days"]) * 86_400_000
        result = await session.execute(
            delete(Command).where(
                Command.created_at_ms < command_cutoff,
                Command.status.in_(("ok", "error", "timeout", "cancelled")),
            )
        )
        stats["commands"] = int(result.rowcount or 0)
        await session.commit()

        quota_bytes = int(values["storage_quota_mb"]) * 1024 * 1024
        target_bytes = int(quota_bytes * int(values["quota_target"]) / 100)
        stats["quota_reclaimed"] = await self._enforce_quota(session, target_bytes)

        if any(stats.values()):
            log.info("retention pass: %s", stats)
        return stats

    async def _purge_frames(self, session: AsyncSession, cutoff_ms: int) -> int:
        removed = 0
        while True:
            result = await session.execute(
                select(Frame).where(Frame.ts_ms < cutoff_ms).order_by(Frame.ts_ms.asc()).limit(PURGE_BATCH)
            )
            rows = list(result.scalars().all())
            if not rows:
                break
            paths = self._collect_paths(rows)
            for row in rows:
                await session.delete(row)
            await session.commit()
            await asyncio.to_thread(self._unlink_all, paths)
            removed += len(rows)
            if len(rows) < PURGE_BATCH:
                break
        return removed

    async def _enforce_quota(self, session: AsyncSession, target_bytes: int) -> int:
        if target_bytes <= 0:
            return 0
        used = await asyncio.to_thread(storage.directory_size_bytes, Path(self.settings.frames_dir))
        used += await asyncio.to_thread(storage.directory_size_bytes, Path(self.settings.thumbs_dir))
        if used <= target_bytes:
            return 0

        reclaimed = 0
        while used > target_bytes:
            result = await session.execute(select(Frame).order_by(Frame.ts_ms.asc()).limit(PURGE_BATCH))
            rows = list(result.scalars().all())
            if not rows:
                break
            paths = self._collect_paths(rows)
            for row in rows:
                await session.delete(row)
            await session.commit()
            freed = await asyncio.to_thread(self._unlink_all, paths)
            reclaimed += freed
            used -= freed
            if len(rows) < PURGE_BATCH:
                break
        return reclaimed

    def _collect_paths(self, rows: list[Frame]) -> list[Path]:
        paths: list[Path] = []
        for row in rows:
            for relative in (row.path, row.thumb_path):
                if not relative:
                    continue
                try:
                    paths.append(storage.absolute(self.settings, relative))
                except storage.StorageError:
                    log.warning("skipping out-of-tree frame path %r", relative)
        return paths

    @staticmethod
    def _unlink_all(paths: list[Path]) -> int:
        freed = 0
        for path in paths:
            freed += storage.delete_path(path)
        return freed

    async def purge_device(self, session: AsyncSession, device_id: int) -> int:
        """Delete every frame row for a device (files are removed by the caller)."""

        result = await session.execute(delete(Frame).where(Frame.device_id == device_id))
        await session.commit()
        return int(result.rowcount or 0)


__all__ = ["PURGE_BATCH", "RetentionService"]