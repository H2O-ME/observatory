"""P3：v8 迁移与"删除设备后命令历史保留"。

只测两件在生产上真正会出问题的事：
1. v7 老库能升到 v8，且 commands 的行、错误码、重试次数、预算都不丢；
2. 删除 devices 行后 commands 仍在（SET NULL 而非 CASCADE）。

用同步 sqlite3 建 v7 壳库 + 真实 Database.migrate()，约 1 秒。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_V7_SCHEMA = """
CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(120) NOT NULL DEFAULT '',
    note VARCHAR(250) NOT NULL DEFAULT '',
    os_name VARCHAR(64) NOT NULL DEFAULT '',
    agent_version VARCHAR(32) NOT NULL DEFAULT '',
    token_hash VARCHAR(64) NOT NULL DEFAULT '',
    settings JSON NOT NULL DEFAULT '{}',
    enrolled_at_ms BIGINT NOT NULL DEFAULT 0,
    last_seen_ms BIGINT NOT NULL DEFAULT 0,
    uninstalled_at_ms BIGINT,
    last_result_at_ms BIGINT
);
CREATE TABLE commands (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
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
    deadline_ms INTEGER
);
CREATE INDEX ix_commands_device_id ON commands (device_id);
CREATE INDEX ix_commands_status ON commands (status);
CREATE INDEX ix_commands_device_status ON commands (device_id, status);
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at_ms BIGINT NOT NULL DEFAULT 0);
INSERT INTO schema_version (version, applied_at_ms) VALUES (7, 0);
INSERT INTO devices (uid, token_hash) VALUES ('OBS-V8-OLD', 'hash-old');
INSERT INTO commands
    (id, device_id, type, payload, status, actor, created_at_ms, expires_at_ms,
     error_code, attempts, deadline_ms)
VALUES
    ('cmd-v8-uninstall', 1, 'uninstall', '{}', 'ok', 'console', 111, 222, 'TIMEOUT', 3, 5000),
    ('cmd-v8-pending', 1, 'snapshot', '{"scope":"session"}', 'pending', 'console', 333, 444, NULL, 0, 15000);
"""


def _make_v7_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_V7_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def test_v7_to_v8_migration_preserves_command_history(tmp_path: Path) -> None:
    from observatory.core.config import Settings  # noqa: PLC0415
    from observatory.core.db import Database, SCHEMA_VERSION  # noqa: PLC0415

    path = tmp_path / "observatory.db"
    _make_v7_db(path)
    settings = Settings(data_dir=tmp_path, secret_key="v8-migration-test-secret-key-0001")

    async def run() -> None:
        database = Database(settings)
        try:
            report = await database.migrate()
            assert report.from_version == 7
            assert 8 in report.applied, "v8（命令历史保留）必须被执行"
            assert report.to_version == SCHEMA_VERSION
            assert report.backup_path is not None, "跨版本迁移必须先备份"
            async with database.sessionmaker() as session:
                from sqlalchemy import text  # noqa: PLC0415

                rows = (
                    await session.execute(
                        text(
                            "SELECT id, device_uid, status, error_code, attempts, deadline_ms "
                            "FROM commands ORDER BY id"
                        )
                    )
                ).fetchall()
                # 行数没丢，device_uid 从 devices 回填
                assert rows == [
                    ("cmd-v8-pending", "OBS-V8-OLD", "pending", None, 0, 15000),
                    ("cmd-v8-uninstall", "OBS-V8-OLD", "ok", "TIMEOUT", 3, 5000),
                ], rows
                # 部分唯一索引：一台设备只允许一条未终结卸载
                indexes = {
                    row[0]
                    for row in (
                        await session.execute(
                            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='commands'")
                        )
                    ).fetchall()
                }
                assert "ux_commands_one_open_uninstall" in indexes
                assert "ix_commands_device_uid" in indexes
        finally:
            await database.engine.dispose()

    asyncio.run(run())


def test_device_delete_keeps_commands_with_null_device_id(tmp_path: Path) -> None:
    """CASCADE 换成 SET NULL：卸载证据不能随设备记录一起消失。"""

    from observatory.core.config import Settings  # noqa: PLC0415
    from observatory.core.db import Database  # noqa: PLC0415

    path = tmp_path / "observatory.db"
    _make_v7_db(path)
    settings = Settings(data_dir=tmp_path, secret_key="v8-migration-test-secret-key-0002")

    async def run() -> None:
        database = Database(settings)
        try:
            await database.migrate()
            async with database.sessionmaker() as session:
                from sqlalchemy import text  # noqa: PLC0415

                assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
                await session.execute(text("DELETE FROM devices WHERE uid='OBS-V8-OLD'"))
                await session.commit()
                rows = (
                    await session.execute(
                        text("SELECT id, device_id, device_uid FROM commands ORDER BY id")
                    )
                ).fetchall()
                assert [(row[0], row[1], row[2]) for row in rows] == [
                    ("cmd-v8-pending", None, "OBS-V8-OLD"),
                    ("cmd-v8-uninstall", None, "OBS-V8-OLD"),
                ], rows
        finally:
            await database.engine.dispose()

    asyncio.run(run())
