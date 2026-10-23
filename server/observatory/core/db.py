"""Async SQLite engine + versioned, idempotent migrations (spec §6/§6bis).

Migration contract:

* ``schema_version`` is the only source of truth for the applied version;
* migrations are an ordered list of functions run in one transaction;
* before touching an existing database the file is copied with the SQLite
  backup API to ``data/backups/pre-migrate-v<from>-<ts>.db``;
* if a migration fails the backup is restored, the engine is disposed and
  :class:`MigrationError` is raised, so the process exits non-zero instead of
  serving on a half-migrated database.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .clock import now_ms
from .config import Settings
from .errors import AppError
from .log import log_event

log = logging.getLogger(__name__)

#: Bump this and append a migration function when the schema changes.
SCHEMA_VERSION = 2

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=8000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-16000",
)

MigrationFn = Callable[[AsyncConnection], Awaitable[None]]


class MigrationError(RuntimeError):
    """Raised when a migration failed and the pre-migration backup was restored."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFn


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: tuple[int, ...] = field(default_factory=tuple)
    backup_path: Path | None = None
    legacy_detected: bool = False


async def _table_names(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {str(row[0]) for row in result.fetchall()}


async def _column_names(conn: AsyncConnection, table: str) -> set[str]:
    result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return {str(row[1]) for row in result.fetchall()}


async def _read_schema_version(conn: AsyncConnection) -> int:
    result = await conn.execute(text("SELECT MAX(version) FROM schema_version"))
    value = result.scalar()
    return int(value) if value is not None else 0


async def _ensure_column(conn: AsyncConnection, table: str, name: str, ddl: str) -> None:
    if name in await _column_names(conn, table):
        return
    await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))
    log.info("migrating: %s.%s added", table, name)


async def _migration_v1_baseline(conn: AsyncConnection) -> None:
    """0.2.10 baseline: the six historic tables. ``IF NOT EXISTS`` = idempotent."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid VARCHAR(64) NOT NULL,
            name VARCHAR(128) NOT NULL,
            hostname VARCHAR(255) NOT NULL,
            os_name VARCHAR(64) NOT NULL,
            os_version VARCHAR(128) NOT NULL,
            arch VARCHAR(32) NOT NULL,
            agent_version VARCHAR(32) NOT NULL,
            token_hash VARCHAR(128) NOT NULL,
            enrolled_at_ms BIGINT NOT NULL,
            last_seen_ms BIGINT NOT NULL,
            uninstalled_at_ms BIGINT,
            last_result_at_ms BIGINT,
            settings JSON NOT NULL,
            sysinfo JSON NOT NULL,
            note VARCHAR(255) NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_devices_uid ON devices (uid)",
        "CREATE INDEX IF NOT EXISTS ix_devices_last_seen_ms ON devices (last_seen_ms)",
        """
        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
            ts_ms BIGINT NOT NULL,
            session INTEGER NOT NULL,
            kind VARCHAR(16) NOT NULL,
            path VARCHAR(512) NOT NULL,
            thumb_path VARCHAR(512),
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_frames_device_id ON frames (device_id)",
        "CREATE INDEX IF NOT EXISTS ix_frames_ts_ms ON frames (ts_ms)",
        "CREATE INDEX IF NOT EXISTS ix_frames_device_ts ON frames (device_id, ts_ms)",
        "CREATE INDEX IF NOT EXISTS ix_frames_device_session ON frames (device_id, session)",
        """
        CREATE TABLE IF NOT EXISTS commands (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            device_id INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
            type VARCHAR(48) NOT NULL,
            payload JSON NOT NULL,
            status VARCHAR(16) NOT NULL,
            result JSON,
            error TEXT,
            actor VARCHAR(64) NOT NULL,
            created_at_ms BIGINT NOT NULL,
            dispatched_at_ms BIGINT,
            finished_at_ms BIGINT,
            expires_at_ms BIGINT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_commands_device_id ON commands (device_id)",
        "CREATE INDEX IF NOT EXISTS ix_commands_status ON commands (status)",
        "CREATE INDEX IF NOT EXISTS ix_commands_device_status ON commands (device_id, status)",
        """
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms BIGINT NOT NULL,
            actor VARCHAR(64) NOT NULL,
            action VARCHAR(64) NOT NULL,
            device_uid VARCHAR(64) NOT NULL,
            device_name VARCHAR(128) NOT NULL,
            ip VARCHAR(64) NOT NULL,
            detail JSON NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_audit_ts_ms ON audit (ts_ms)",
        "CREATE INDEX IF NOT EXISTS ix_audit_action ON audit (action)",
        "CREATE INDEX IF NOT EXISTS ix_audit_device_uid ON audit (device_uid)",
        """
        CREATE TABLE IF NOT EXISTS settings (
            key VARCHAR(64) NOT NULL PRIMARY KEY,
            value JSON NOT NULL,
            updated_at_ms BIGINT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(16) NOT NULL,
            version VARCHAR(32) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            notes VARCHAR(255) NOT NULL,
            created_at_ms BIGINT NOT NULL,
            CONSTRAINT uq_packages_kind_version UNIQUE (kind, version)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_packages_version ON packages (version)",
    )
    for statement in statements:
        await conn.execute(text(statement))


async def _migration_v2_link_v3(conn: AsyncConnection) -> None:
    """0.3.0 link/protocol columns and the two new state tables."""

    await _ensure_column(conn, "devices", "boot_id", "VARCHAR(64) NOT NULL DEFAULT ''")
    await _ensure_column(conn, "devices", "protocol", "INTEGER NOT NULL DEFAULT 2")

    await _ensure_column(conn, "frames", "device_ts_ms", "BIGINT")
    await _ensure_column(conn, "frames", "frame_id", "VARCHAR(64)")
    await _ensure_column(conn, "frames", "archived_at_ms", "BIGINT")
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_frames_device_frame "
            "ON frames (device_id, frame_id)"
        )
    )

    await _ensure_column(conn, "commands", "error_code", "VARCHAR(32)")
    await _ensure_column(conn, "commands", "attempts", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "commands", "deadline_ms", "INTEGER")

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS frame_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                frame_id VARCHAR(64),
                day VARCHAR(16) NOT NULL,
                remote_path VARCHAR(512) NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                size_bytes INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at_ms BIGINT NOT NULL,
                uploaded_at_ms BIGINT,
                error TEXT
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_frame_archives_device_frame "
            "ON frame_archives (device_id, frame_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_frame_archives_status_created "
            "ON frame_archives (status, created_at_ms)"
        )
    )

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS install_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash VARCHAR(128) NOT NULL,
                created_by VARCHAR(64) NOT NULL,
                created_at_ms BIGINT NOT NULL,
                expires_at_ms BIGINT NOT NULL,
                used_at_ms BIGINT,
                used_device_id INTEGER,
                max_uses INTEGER NOT NULL DEFAULT 1
            )
            """
        )
    )
    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ux_install_tokens_hash ON install_tokens (token_hash)")
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline_0_2_10", _migration_v1_baseline),
    Migration(2, "link_v3_0_3_0", _migration_v2_link_v3),
)


def _sqlite_backup(source: Path, target: Path) -> None:
    """Copy *source* to *target* through the SQLite online backup API."""

    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source))
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        with suppress(FileNotFoundError):
            # Reason: a stale WAL/SHM of the failed migration must not be replayed.
            path.with_name(path.name + suffix).unlink()


def _path_display(path: Path) -> str:
    return str(path)


class Database:
    """Owns the async engine, the session factory and the migration runner."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.path: Path = settings.db_path
        self.url = f"sqlite+aiosqlite:///{self.path.as_posix()}"
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            echo=False,
            pool_pre_ping=True,
            connect_args={"timeout": 15},
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        event.listen(self.engine.sync_engine, "connect", _on_connect)
        self._migrated = False

    async def migrate(self) -> MigrationReport:
        """Bring the file to :data:`SCHEMA_VERSION`, backing up first if needed."""

        settings = self.settings
        existing = self.path.exists() and self.path.stat().st_size > 0
        from_version = 0
        legacy_detected = False
        tables: set[str] = set()
        if existing:
            async with self.engine.connect() as conn:
                tables = await _table_names(conn)
                if "schema_version" in tables:
                    from_version = await _read_schema_version(conn)
                elif "devices" in tables:
                    # 0.2.10 database: stamp it as v1 (spec §6bis).
                    from_version = 1
                    legacy_detected = True

        pending = [migration for migration in MIGRATIONS if migration.version > from_version]
        backup_path: Path | None = None
        if pending and existing and tables:
            backup_path = await self._backup(from_version)

        try:
            applied = await self._apply(pending, stamp_version=1 if legacy_detected else None)
        except Exception as exc:
            log.exception("database migration failed; restoring pre-migration backup")
            if backup_path is not None:
                await self._restore(backup_path)
            raise MigrationError(
                "数据库迁移失败，已尝试还原备份。"
                f"备份: {_path_display(backup_path) if backup_path else '（新建库，无备份）'}；"
                f"手工恢复: copy \"{backup_path}\" \"{self.path}\""
                if backup_path
                else "数据库迁移失败，且新建库没有可还原的备份。"
            ) from exc

        self._migrated = True
        report = MigrationReport(
            from_version=from_version,
            to_version=SCHEMA_VERSION,
            applied=tuple(applied),
            backup_path=backup_path,
            legacy_detected=legacy_detected,
        )
        log_event(
            log,
            logging.INFO,
            "db.migrated",
            from_version=report.from_version,
            to_version=report.to_version,
            applied=report.applied,
            backup=str(report.backup_path) if report.backup_path else None,
            legacy=report.legacy_detected,
        )
        return report

    async def _apply(self, pending: list[Migration], *, stamp_version: int | None) -> list[int]:
        applied: list[int] = []
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_version ("
                    "version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL DEFAULT 0)"
                )
            )
            if stamp_version is not None:
                await conn.execute(
                    text("INSERT OR REPLACE INTO schema_version (version, applied_at_ms) VALUES (:v, :ts)"),
                    {"v": stamp_version, "ts": now_ms()},
                )
            for migration in pending:
                await migration.apply(conn)
                await conn.execute(
                    text("INSERT OR REPLACE INTO schema_version (version, applied_at_ms) VALUES (:v, :ts)"),
                    {"v": migration.version, "ts": now_ms()},
                )
                applied.append(migration.version)
        return applied

    async def _backup(self, from_version: int) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        target = self.settings.backups_dir / f"pre-migrate-v{from_version}-{stamp}.db"
        await asyncio.to_thread(_sqlite_backup, self.path, target)
        log_event(log, logging.INFO, "db.backup_created", path=str(target), from_version=from_version)
        return target

    async def _restore(self, backup_path: Path) -> None:
        await self.engine.dispose()
        _remove_sqlite_sidecars(self.path)
        await asyncio.to_thread(shutil.copy2, backup_path, self.path)
        log_event(log, logging.WARNING, "db.backup_restored", path=str(backup_path))

    async def schema_version(self) -> int:
        async with self.engine.connect() as conn:
            return await _read_schema_version(conn)

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()


def _on_connect(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one AsyncSession per request."""

    database: Database | None = getattr(request.app.state, "db", None)
    if database is None:
        raise AppError.internal("数据库尚未就绪")
    async with database.sessionmaker() as session:
        yield session


__all__ = [
    "Database",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "MigrationReport",
    "SCHEMA_VERSION",
    "get_session",
]