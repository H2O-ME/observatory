"""v10 迁移：设备清理任务带上意图（``purge_reason`` / ``purge_remote``）。

为什么值得单独测：S5 的修复依赖这两列表达"这次清理是谁排的队、远端要不要删"。
迁移必须(a) 给 v9 老库补列、(b) 给已有 ``purge_pending_at_ms`` 的行回填可确定的
意图（v9 只有卸载后的自动清理会盖章）、(c) 幂等——重复执行不报错、不覆盖人工
改过的值。用一份合成的 v9 库直接跑迁移，不经过 app，< 3 秒。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_V9_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL);
INSERT INTO schema_version (version, applied_at_ms) VALUES (9, 1700000000000);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL DEFAULT '',
    uninstalled_at_ms BIGINT,
    purge_pending_at_ms BIGINT
);

INSERT INTO devices (uid, uninstalled_at_ms, purge_pending_at_ms)
VALUES ('OBS-V10-QUEUED', 1700000000001, 1700000000002),
       ('OBS-V10-LIVE', NULL, NULL);
"""


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_v9_to_v10_adds_purge_intent_and_backfills(tmp_dir: Path) -> None:
    from observatory.core.config import Settings
    from observatory.core.db import SCHEMA_VERSION, Database

    database_path = tmp_dir / "observatory.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_V9_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    settings = Settings(
        data_dir=tmp_dir,
        secret_key="test-secret-key-0123456789abcdef0123456789",
        admin_password="test-admin-password",
    )
    database = Database(settings)

    async def run():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    report = asyncio.run(run())
    assert report.from_version == 9
    assert report.to_version == SCHEMA_VERSION == 10
    assert report.applied == (10,)
    assert report.backup_path is not None and report.backup_path.exists(), "跨版本迁移必须先备份"

    connection = sqlite3.connect(database_path)
    try:
        assert {"purge_reason", "purge_remote"} <= _columns(connection, "devices")
        queued = connection.execute(
            "SELECT purge_reason, purge_remote FROM devices WHERE uid='OBS-V10-QUEUED'"
        ).fetchone()
        assert queued == ("auto_after_uninstall", 1), f"老标记必须回填可确定的意图，实际 {queued}"
        live = connection.execute(
            "SELECT purge_reason, purge_remote FROM devices WHERE uid='OBS-V10-LIVE'"
        ).fetchone()
        assert live == (None, None), "没有待清理标记的设备不得凭空冒出意图"
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
    finally:
        connection.close()

    # 幂等：再跑一次完整迁移不会重复应用
    async def rerun():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    again = asyncio.run(rerun())
    assert again.applied == ()

    # 迁移函数本身的幂等：人工改过的意图不能被回填覆盖
    async def apply_twice() -> None:
        from sqlalchemy import text

        from observatory.core.db import _migration_v10_purge_intent

        async with database.engine.begin() as conn:
            await conn.execute(text("UPDATE devices SET purge_reason='manual' WHERE uid='OBS-V10-QUEUED'"))
            await _migration_v10_purge_intent(conn)
            await _migration_v10_purge_intent(conn)
        await database.engine.dispose()

    asyncio.run(apply_twice())

    connection = sqlite3.connect(database_path)
    try:
        reason = connection.execute(
            "SELECT purge_reason FROM devices WHERE uid='OBS-V10-QUEUED'"
        ).fetchone()[0]
        assert reason == "manual", "重复执行不得覆盖已有的清理意图"
    finally:
        connection.close()
