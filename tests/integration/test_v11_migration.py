"""v11 迁移：服务端暂存取回表（``file_transfers``）。

为什么值得单独测：文件取回从"浏览器直连分片读"改成"服务端后台拉回 + 24h
暂存"，新增的元数据表是这条链路的唯一状态来源。迁移必须(a) 给 v10 老库建表、
(b) 建好清理热路径用的 ``(status, expires_at_ms)`` 索引、(c) 幂等——重复执行
不报错。用一份合成的 v10 库直接跑迁移，不经过 app。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_V10_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL);
INSERT INTO schema_version (version, applied_at_ms) VALUES (10, 1700000000000);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL DEFAULT ''
);

INSERT INTO devices (uid) VALUES ('OBS-V11-EXISTING');
"""

_EXPECTED_COLUMNS = {
    "id",
    "device_uid",
    "device_name",
    "remote_path",
    "filename",
    "expected_bytes",
    "received_bytes",
    "status",
    "error",
    "sha256",
    "storage_name",
    "actor",
    "created_at_ms",
    "completed_at_ms",
    "expires_at_ms",
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute("SELECT * FROM pragma_table_info(?)", (table,))}


def _indexes(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute("SELECT * FROM pragma_index_list(?)", (table,))}


def test_v10_to_v11_creates_file_transfers(tmp_dir: Path) -> None:
    from observatory.core.config import Settings
    from observatory.core.db import SCHEMA_VERSION, Database

    from tests.helpers import TEST_ADMIN_PASSWORD, TEST_SECRET_KEY

    database_path = tmp_dir / "observatory.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_V10_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    settings = Settings(
        data_dir=tmp_dir,
        secret_key=TEST_SECRET_KEY,
        admin_password=TEST_ADMIN_PASSWORD,
    )
    database = Database(settings)

    async def run():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    report = asyncio.run(run())
    assert report.from_version == 10
    assert report.to_version == SCHEMA_VERSION == 11
    assert report.applied == (11,)
    assert report.backup_path is not None and report.backup_path.exists(), "跨版本迁移必须先备份"

    connection = sqlite3.connect(database_path)
    try:
        assert _columns(connection, "file_transfers") == _EXPECTED_COLUMNS
        assert "ix_file_transfers_status_expires" in _indexes(connection, "file_transfers")
        assert "ix_file_transfers_device_uid" in _indexes(connection, "file_transfers")
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
        # 老数据（设备表）不能被迁移影响
        assert connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
    finally:
        connection.close()

    # 幂等：再跑一次完整迁移不会重复应用
    async def rerun():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    assert asyncio.run(rerun()).applied == ()

    # 迁移函数本身的幂等：建表/建索引都带 IF NOT EXISTS，可重复执行
    async def apply_twice() -> None:
        from observatory.core.db import _migration_v11_file_transfers

        async with database.engine.begin() as conn:
            await _migration_v11_file_transfers(conn)
            await _migration_v11_file_transfers(conn)
        await database.engine.dispose()

    asyncio.run(apply_twice())
