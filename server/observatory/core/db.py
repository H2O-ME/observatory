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
import json
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
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .clock import now_ms
from .config import Settings
from .errors import AppError
from .log import log_event

log = logging.getLogger(__name__)

#: Bump this and append a migration function when the schema changes.
SCHEMA_VERSION = 9

#: 每个连接都执行的 PRAGMA（W7/F56：journal_mode 需要短暂拿写锁且只需设一次，
#: 移到迁移路径；NullPool 下每会话新建连接，这里只留真正 per-connection 的项）。
PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=8000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-16000",
    # WAL 峰值治理：批量删除/归档并发写时 WAL 可膨胀数百 MB，之后的
    # autocheckpoint 会造成秒级写停顿；64MB 封顶让 checkpoint 平摊。
    "PRAGMA journal_size_limit=67108864",
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


async def _migration_v3_device_token_index(conn: AsyncConnection) -> None:
    """Unique token index: a re-enroll overwrite makes the previous token unusable.

    The partial predicate keeps legacy rows with an empty ``token_hash`` legal
    while guaranteeing that two devices can never share a real token.
    """

    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_token_hash "
            "ON devices (token_hash) WHERE token_hash <> ''"
        )
    )


async def _migration_v4_tombstones_and_indexes(conn: AsyncConnection) -> None:
    """0.3.1: delete tombstones (F9) + archive/audit indexes (F26/F27).

    The archive unique index first removes duplicate ``(device_id, remote_path)``
    rows (concurrent registration could create them before the constraint
    existed), keeping the oldest row of each group.
    """

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS deleted_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid VARCHAR(64) NOT NULL,
                token_hash VARCHAR(128) NOT NULL,
                deleted_at_ms BIGINT NOT NULL
            )
            """
        )
    )
    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ux_deleted_devices_token ON deleted_devices (token_hash)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_deleted_devices_uid ON deleted_devices (uid)")
    )

    await conn.execute(
        text(
            """
            DELETE FROM frame_archives
            WHERE id NOT IN (
                SELECT MIN(id) FROM frame_archives GROUP BY device_id, remote_path
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_frame_archives_device_remote_path "
            "ON frame_archives (device_id, remote_path)"
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit (actor)"))




async def _migration_v5_archive_frame_ts(conn: AsyncConnection) -> None:
    """frame_archives.frame_ts_ms（归档行的原始采集时间）+ 热查询复合索引。

    P1/P2（2026-10 审查）：restore 重建 Frame 此前用 created_at_ms（归档登记
    时刻），取回的帧在时间线上排错位置、还被保留策略当作新帧；audit/commands
    的过滤+排序热查询只有单列索引。
    """

    await conn.execute(text("ALTER TABLE frame_archives ADD COLUMN frame_ts_ms BIGINT NULL"))
    # 老行没有原始 ts：沿用旧语义，restore 行为与 v4 一致。
    await conn.execute(text("UPDATE frame_archives SET frame_ts_ms = created_at_ms WHERE frame_ts_ms IS NULL"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_action_ts ON audit (action, ts_ms)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_device_ts ON audit (device_uid, ts_ms)"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_commands_device_created ON commands (device_id, created_at_ms)")
    )


async def _migration_v6_install_tickets(conn: AsyncConnection) -> None:
    """部署票据改为"长期有效 + 可绑定多台设备"（2026-10-27）。

    旧模型把 ``used_at_ms`` 当"一次性"标志：管理员重装同一台机器时，受控端
    因为 config.json 里已有设备令牌而跳过注册，票据永远显示"未使用"，而撤销
    又会悄悄掐掉这台机器唯一的重认证凭据。新模型用 ``install_token_uses``
    记录"票据 → 设备"的去重绑定关系，``used_count`` 是已绑定台数，
    ``max_uses = 0`` 表示不限、``expires_at_ms = 0`` 表示长期有效。
    """

    await _ensure_column(conn, "install_tokens", "used_count", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "install_tokens", "last_used_at_ms", "BIGINT NULL")
    await _ensure_column(conn, "install_tokens", "revoked_at_ms", "BIGINT NULL")
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS install_token_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id INTEGER NOT NULL,
                device_id INTEGER NOT NULL,
                used_at_ms BIGINT NOT NULL,
                ip VARCHAR(64) NOT NULL DEFAULT '',
                hostname VARCHAR(128) NOT NULL DEFAULT ''
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_install_token_uses_pair "
            "ON install_token_uses (token_id, device_id)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_install_token_uses_token ON install_token_uses (token_id)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_install_token_uses_device ON install_token_uses (device_id)")
    )
    # 旧行回填：曾经"用过一次"的票据视为绑定过 1 台设备。
    await conn.execute(
        text(
            "UPDATE install_tokens SET used_count = 1, last_used_at_ms = used_at_ms "
            "WHERE used_at_ms IS NOT NULL AND used_count = 0"
        )
    )
    await conn.execute(
        text(
            "INSERT OR IGNORE INTO install_token_uses (token_id, device_id, used_at_ms) "
            "SELECT id, used_device_id, COALESCE(used_at_ms, created_at_ms) "
            "FROM install_tokens WHERE used_device_id IS NOT NULL"
        )
    )


async def _migration_v7_device_token_history(conn: AsyncConnection) -> None:
    """P1: superseded device tokens with a grace window (``device_token_history``).

    Why it exists: an enrolment can commit and still lose its response, after
    which the agent retries with the token it already had. Previously that retry
    was rejected as unknown, so the device could not recover its own record. The
    table keeps the *previous* hash for ``OBS_TOKEN_ROTATE_GRACE_S`` so the retry
    is accepted, and it is what makes concurrent rotations safe (INV-3: every
    token handed out during a burst stays usable inside the window).

    ``ON DELETE CASCADE`` is load-bearing: deleting a device must also forget its
    superseded tokens, otherwise an old token would keep authenticating against a
    device that no longer exists — i.e. an accidental permanent credential, which
    is exactly the "ban/lockout" shape this release removes.
    """

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS device_token_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
                token_hash VARCHAR(128) NOT NULL,
                superseded_at_ms BIGINT NOT NULL
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_device_token_history_hash "
            "ON device_token_history (token_hash)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_device_token_history_device "
            "ON device_token_history (device_id)"
        )
    )


async def _migration_v8_command_history(conn: AsyncConnection) -> None:
    """P3: keep command history after the device row is gone.

    Enrolment/cleanup now delete the ``devices`` row when a confirmed uninstall
    removes the activity record. With the old ``ON DELETE CASCADE`` on
    ``commands.device_id`` that delete silently took the uninstall command — the
    very evidence that the purge happened — with it.

    So this rebuilds ``commands``::

        device_id  → nullable, ON DELETE SET NULL
        device_uid → new NOT NULL column, backfilled from devices

    plus ``devices.purge_pending_at_ms`` to mark "uninstalled, record not yet
    removed" so a failed media cleanup can be retried by housekeeping instead of
    being reported as a clean delete.

    SQLite cannot alter a foreign key in place, so the table is rebuilt. There is
    no child table referencing ``commands``, and every existing row joined a live
    ``devices`` row before this migration (CASCADE guaranteed it), so the
    ``device_uid`` backfill cannot leave empty values behind.
    """

    await _ensure_column(conn, "devices", "purge_pending_at_ms", "BIGINT NULL")

    columns = await _column_names(conn, "commands")
    if "device_uid" in columns:
        # Already rebuilt (idempotent re-run after a partial deploy).
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_commands_device_uid ON commands (device_uid)"
            )
        )
        return

    await conn.execute(
        text(
            """
            CREATE TABLE commands_v8 (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                device_id INTEGER NULL REFERENCES devices (id) ON DELETE SET NULL,
                device_uid VARCHAR(64) NOT NULL DEFAULT '',
                type VARCHAR(48) NOT NULL,
                payload JSON NOT NULL,
                status VARCHAR(16) NOT NULL,
                result JSON,
                error TEXT,
                error_code VARCHAR(32),
                attempts INTEGER NOT NULL DEFAULT 0,
                actor VARCHAR(64) NOT NULL,
                created_at_ms BIGINT NOT NULL,
                dispatched_at_ms BIGINT,
                finished_at_ms BIGINT,
                expires_at_ms BIGINT NOT NULL,
                deadline_ms BIGINT
            )
            """
        )
    )
    backfill = (
        """
        INSERT INTO commands_v8 (
            id, device_id, device_uid, type, payload, status, result, error,
            error_code, attempts, actor, created_at_ms, dispatched_at_ms,
            finished_at_ms, expires_at_ms, deadline_ms
        )
        SELECT
            c.id,
            c.device_id,
            COALESCE((SELECT d.uid FROM devices d WHERE d.id = c.device_id), ''),
            c.type, c.payload, c.status, c.result, c.error,
            {error_code}, {attempts}, c.actor, c.created_at_ms,
            c.dispatched_at_ms, c.finished_at_ms, c.expires_at_ms, {deadline_ms}
        FROM commands c
        """
    ).format(
        error_code="c.error_code" if "error_code" in columns else "NULL",
        attempts="COALESCE(c.attempts, 0)" if "attempts" in columns else "0",
        deadline_ms="c.deadline_ms" if "deadline_ms" in columns else "NULL",
    )
    await conn.execute(text(backfill))
    await conn.execute(text("DROP TABLE commands"))
    await conn.execute(text("ALTER TABLE commands_v8 RENAME TO commands"))
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_commands_device_id ON commands (device_id)",
        "CREATE INDEX IF NOT EXISTS ix_commands_status ON commands (status)",
        "CREATE INDEX IF NOT EXISTS ix_commands_device_status ON commands (device_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_commands_device_uid ON commands (device_uid)",
        # P2 invariant, enforced by the database rather than only by the handler:
        # at most one open uninstall per device. Partial index = SQLite-friendly.
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_commands_one_open_uninstall "
        "ON commands (device_id) WHERE type = 'uninstall' "
        "AND status IN ('pending', 'dispatched', 'running')",
    ):
        await conn.execute(text(statement))


async def _migration_v9_drop_tickets_and_tombstones(conn: AsyncConnection) -> None:
    """P4: delete the two legacy admission mechanisms.

    Registration is open (no per-machine approval, no tickets, no bans), so the
    tables that implemented the old model carry no authority any more:

    ``install_tokens`` / ``install_token_uses``
        deployment tickets and their device bindings (A18);
    ``deleted_devices``
        delete tombstones (F9) — deleting a device is "forget the record", not a
        ban, and a reinstall must be able to register again.

    Dropping them outright would lose the operational record of *who was let in
    and what was deleted*, so every row is copied into ``audit`` first (one entry
    per row, hashes only — tickets never stored plaintext). ``audit`` is the
    table that outlives devices and is already the compliance trail.

    The migration is idempotent: absent tables are simply skipped.
    """

    tables = await _table_names(conn)
    stamp = now_ms()
    for table, action, columns in (
        (
            "install_tokens",
            "legacy.ticket_archive",
            "id, created_by, created_at_ms, expires_at_ms, max_uses, used_count, "
            "last_used_at_ms, revoked_at_ms",
        ),
        (
            "install_token_uses",
            "legacy.ticket_binding_archive",
            "id, token_id, device_id, used_at_ms, ip, hostname",
        ),
        (
            "deleted_devices",
            "legacy.tombstone_archive",
            "id, uid, deleted_at_ms",
        ),
    ):
        if table not in tables:
            continue
        rows = (await conn.execute(text(f"SELECT {columns} FROM {table}"))).fetchall()
        names = [str(item).strip() for item in columns.split(",")]
        for row in rows:
            detail = {name: value for name, value in zip(names, row, strict=True)}
            uid = str(detail.pop("uid", "") or "")
            await conn.execute(
                text(
                    "INSERT INTO audit (ts_ms, actor, action, device_uid, device_name, ip, detail) "
                    "VALUES (:ts_ms, :actor, :action, :uid, '', :ip, :detail)"
                ),
                {
                    "ts_ms": stamp,
                    "actor": "migration",
                    "action": action,
                    "uid": uid,
                    "ip": str(detail.get("ip") or ""),
                    "detail": json.dumps(detail, ensure_ascii=False),
                },
            )
        await conn.execute(text(f"DROP TABLE {table}"))
        log.info("migrating: %s archived (%d rows) and dropped", table, len(rows))


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline_0_2_10", _migration_v1_baseline),
    Migration(2, "link_v3_0_3_0", _migration_v2_link_v3),
    Migration(3, "device_token_index", _migration_v3_device_token_index),
    Migration(4, "tombstones_and_indexes", _migration_v4_tombstones_and_indexes),
    Migration(5, "archive_frame_ts_and_indexes", _migration_v5_archive_frame_ts),
    Migration(6, "install_tickets_multi_device", _migration_v6_install_tickets),
    Migration(7, "device_token_history", _migration_v7_device_token_history),
    Migration(8, "command_history_preserved", _migration_v8_command_history),
    Migration(9, "drop_tickets_and_tombstones", _migration_v9_drop_tickets_and_tombstones),
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
        # NullPool：不复用池连接。QueuePool 在 dispose 时要走 aiosqlite 的
        # terminate 优雅关闭，Python 3.14 上该路径会被取消并永久挂起（测试里
        # 表现为 portal 线程 join 不返回）；SQLite 本地打开成本极低，不值得冒险。
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            echo=False,
            poolclass=NullPool,
            connect_args={"timeout": 15},
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        event.listen(self.engine.sync_engine, "connect", _on_connect)
        self._migrated = False

    async def migrate(self) -> MigrationReport:
        """Bring the file to :data:`SCHEMA_VERSION`, backing up first if needed."""

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
            # W7/F56: WAL 只在迁移路径设置一次（需要短暂写锁）；已有 WAL 库
            # 该语句是幂等 no-op。
            await conn.execute(text("PRAGMA journal_mode=WAL"))
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