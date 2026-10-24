"""0.2.10 数据库 schema 契约（N3 迁移的证据保护）。"""

from __future__ import annotations

import sqlite3

import pytest

from tests.contract.fixtures import legacy_db_0_2_10 as legacy_db

pytestmark = pytest.mark.contract


def test_schema_has_exactly_the_six_legacy_tables() -> None:
    assert set(legacy_db.LEGACY_DB_TABLES) == {"audit", "commands", "devices", "frames", "packages", "settings"}
    table_ddl = [statement for statement in legacy_db.DDL if statement.startswith("CREATE TABLE")]
    assert len(table_ddl) == 6
    for table in legacy_db.LEGACY_DB_TABLES:
        assert any(f"CREATE TABLE {table} " in statement for statement in table_ddl), table


def test_schema_is_replayable_on_empty_database() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        legacy_db.create_legacy_schema(connection)
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table' order by name")
        }
        assert tables == set(legacy_db.LEGACY_DB_TABLES)
    finally:
        connection.close()


def test_0_3_0_expected_columns_and_tables_are_documented() -> None:
    """spec §6 的新增列/表必须被迁移测试覆盖（W3）。"""

    assert set(legacy_db.EXPECTED_NEW_COLUMNS) == {"commands", "devices", "frames"}
    assert "attempts" in legacy_db.EXPECTED_NEW_COLUMNS["commands"]
    assert "error_code" in legacy_db.EXPECTED_NEW_COLUMNS["commands"]
    assert "boot_id" in legacy_db.EXPECTED_NEW_COLUMNS["devices"]
    assert "protocol" in legacy_db.EXPECTED_NEW_COLUMNS["devices"]
    assert "frame_id" in legacy_db.EXPECTED_NEW_COLUMNS["frames"]
    assert set(legacy_db.EXPECTED_NEW_TABLES) == {"schema_version", "frame_archives", "install_tokens"}


def test_json_columns_are_declared_for_every_table_that_needs_them() -> None:
    assert set(legacy_db.JSON_COLUMNS) <= set(legacy_db.LEGACY_DB_TABLES)
    assert "detail" in legacy_db.JSON_COLUMNS["audit"]
    assert "payload" in legacy_db.JSON_COLUMNS["commands"]
    assert "settings" in legacy_db.JSON_COLUMNS["devices"]


def _seed_legacy_database(path, *, devices: int = 500, frames: int = 20_000) -> None:
    """构造 0.2.10 形状的合成历史库（500 设备 / 20000 帧）。"""

    connection = sqlite3.connect(path)
    try:
        legacy_db.create_legacy_schema(connection)
        connection.executemany(
            "INSERT INTO devices (id, uid, name, hostname, os_name, os_version, arch, agent_version, "
            "token_hash, enrolled_at_ms, last_seen_ms, uninstalled_at_ms, last_result_at_ms, settings, "
            "sysinfo, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    index,
                    f"OBS-LEGACY-{index:05d}",
                    f"PC-{index:05d}",
                    f"PC-{index:05d}",
                    "Windows",
                    "11 23H2",
                    "AMD64",
                    "0.2.10",
                    f"{index:064x}",
                    1_700_000_000_000 + index,
                    1_700_000_100_000 + index,
                    None,
                    None,
                    "{}",
                    "{}",
                    "",
                )
                for index in range(1, devices + 1)
            ],
        )
        connection.executemany(
            "INSERT INTO frames (id, device_id, ts_ms, session, kind, path, thumb_path, width, height, "
            "size_bytes, sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    index,
                    (index % devices) + 1,
                    1_690_000_000_000 + index * 1_000,
                    1,
                    "screen",
                    f"frames/OBS-LEGACY-{((index % devices) + 1):05d}/2023-07-15/10/frame-{index}.webp",
                    None,
                    1600,
                    900,
                    20_000 + index,
                    f"{index:064x}",
                )
                for index in range(1, frames + 1)
            ],
        )
        connection.executemany(
            "INSERT INTO commands (id, device_id, type, payload, status, result, error, actor, "
            "created_at_ms, dispatched_at_ms, finished_at_ms, expires_at_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"cmd-{index:05d}",
                    (index % devices) + 1,
                    "sysinfo",
                    "{}",
                    "ok",
                    "{}",
                    None,
                    "console",
                    1_700_000_000_000 + index,
                    1_700_000_000_100 + index,
                    1_700_000_000_200 + index,
                    1_700_000_100_000 + index,
                )
                for index in range(1, 51)
            ],
        )
        connection.execute(
            'INSERT INTO settings ("key", value, updated_at_ms) VALUES (?,?,?)',
            ("jpeg_quality", "72", 1_700_000_000_000),
        )
        connection.execute(
            "INSERT INTO audit (id, ts_ms, actor, action, device_uid, device_name, ip, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (1, 1_700_000_000_000, "admin", "device.delete", "OBS-LEGACY-00001", "PC-00001", "127.0.0.1", "{}"),
        )
        connection.commit()
    finally:
        connection.close()


def test_n3_migrates_legacy_schema_with_backup(tmp_dir) -> None:
    """N3：合成 500 设备/20000 帧旧库 → 备份 → 六表保留 → 新列/新表/索引 → 幂等。"""

    import asyncio

    from observatory.core.config import Settings
    from observatory.core.db import SCHEMA_VERSION, Database

    database_path = tmp_dir / "observatory.db"
    _seed_legacy_database(database_path)
    settings = Settings(
        data_dir=tmp_dir,
        secret_key="test-secret-key-0123456789abcdef0123456789",
        admin_password="test-admin-password",
    )
    database = Database(settings)

    async def migrate_twice():
        first = await database.migrate()
        second = await database.migrate()
        await database.engine.dispose()
        return first, second

    first, second = asyncio.run(migrate_twice())
    assert first.from_version == 1, "0.2.10 库应被识别为 v1"
    assert first.legacy_detected is True
    assert first.to_version == SCHEMA_VERSION
    assert first.applied == tuple(range(2, SCHEMA_VERSION + 1))
    assert first.backup_path is not None and first.backup_path.exists(), "迁移前必须备份"
    assert first.backup_path.stat().st_size > 0

    # 幂等：第二次迁移不再执行/不再备份
    assert second.from_version == SCHEMA_VERSION
    assert second.applied == ()
    assert second.backup_path is None

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table' order by name")
        }
        assert set(legacy_db.LEGACY_DB_TABLES) <= tables
        assert set(legacy_db.EXPECTED_NEW_TABLES) <= tables

        for table, columns in legacy_db.EXPECTED_NEW_COLUMNS.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = set(columns) - existing
            assert not missing, f"{table} 缺少新列 {missing}"
        frame_columns = {row[1] for row in connection.execute("PRAGMA table_info(frames)")}
        assert "device_ts_ms" in frame_columns and "frame_id" in frame_columns

        assert connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 500
        assert connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 20_000
        oldest = connection.execute("SELECT MIN(ts_ms), path, sha256 FROM frames WHERE id=1").fetchone()
        assert oldest[0] == 1_690_000_001_000, "旧画面 ts_ms 必须原样保留"
        assert oldest[1].endswith("frame-1.webp")
        assert len(oldest[2]) == 64

        indexes = {row[0] for row in connection.execute("select name from sqlite_master where type='index'")}
        assert "ux_devices_token_hash" in indexes, "v3 迁移必须建 token_hash partial unique 索引"
        assert "ix_frames_device_ts" in indexes
        assert "ux_frame_archives_device_remote_path" in indexes, "v4 归档唯一索引（F26）"
        assert "ix_audit_actor" in indexes, "v4 审计 actor 索引（F27）"
        tombstone_columns = {row[1] for row in connection.execute("PRAGMA table_info(deleted_devices)")}
        assert {"uid", "token_hash", "deleted_at_ms"} <= tombstone_columns, "v4 删除墓碑表（F9）"
    finally:
        connection.close()

def test_migration_failure_restores_backup(tmp_dir) -> None:
    """C6 补测（0.3.1 F63）：迁移失败必须还原备份并以 MigrationError 拒绝启动。

    0.3.0 的 docstring 承诺了这条失败恢复路径，但没有任何测试执行过它——
    "看起来测了其实没测"的典型。注入一个必然抛错的迁移，断言：备份生成、
    异常类型正确、库内容回到迁移前。
    """

    import asyncio

    import pytest as _pytest

    from observatory.core.config import Settings
    from observatory.core.db import Database, Migration, MigrationError

    database_path = tmp_dir / "observatory.db"
    _seed_legacy_database(database_path, devices=3, frames=5)
    settings = Settings(
        data_dir=tmp_dir,
        secret_key="test-secret-key-0123456789abcdef0123456789",
        admin_password="test-admin-password",
    )

    async def broken_migration(_conn) -> None:
        raise RuntimeError("injected migration failure")

    async def run() -> None:
        database = Database(settings)
        original = list(database.__class__.__mro__)  # keep reference shape
        del original
        import observatory.core.db as db_module

        saved = db_module.MIGRATIONS
        db_module.MIGRATIONS = (
            Migration(2, "injected_broken", broken_migration),
        )
        try:
            with _pytest.raises(MigrationError):
                await database.migrate()
        finally:
            db_module.MIGRATIONS = saved
        await database.engine.dispose()

    asyncio.run(run())

    connection = sqlite3.connect(database_path)
    try:
        # 还原后：0.2.10 形状原样保留（没有 schema_version 表、没有 v2 列）
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        assert set(legacy_db.LEGACY_DB_TABLES) <= tables, "还原后旧表必须原样存在"
        assert "schema_version" not in tables, "迁移失败后不得留下半迁移状态"
        device_columns = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
        assert "boot_id" not in device_columns, "还原后不应有 v2 新列"
    finally:
        connection.close()

    # 迁移前备份存在且非空
    backups = list((tmp_dir / "backups").glob("pre-migrate-v*.db"))
    assert backups and all(item.stat().st_size > 0 for item in backups), "失败路径也必须留下备份"
